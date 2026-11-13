# -*- coding: utf-8 -*-
"""beacon.logview — read what happened, as it happens.

Three views, all from files in the run directory (nothing is sent anywhere):

  beacon log          each time Claude Code tried to stop: what it said, what beacon
                      decided, and the exact reply beacon sent back (or why it let it stop)
  beacon transcript   Claude Code's own full log of a session — your prompts, Claude's
                      text, every tool call and result, notices, the end summary. Works
                      for interactive sessions (the transcript file Claude Code writes,
                      whose path every hook receives and beacon records in sessions.jsonl)
                      and for `beacon watch` sessions (the raw stream in <run>/sessions/).
  --follow            on either: keep printing new entries until Ctrl-C.
"""
import json
import os
import time

_MAX = 600


def _jsonl(path, start=0):
    """-> (entries, end_offset). Reads complete lines only."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], start
    end = data.rfind(b"\n") + 1
    out = []
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line.decode("utf-8", "replace")))
        except ValueError:
            continue
    return out, start + end


def _clip(text, width):
    """Keep the start AND the end: the end of an output is where errors, tracebacks and final results are."""
    text = (text or "").strip()
    if width is None or len(text) <= width:
        return text
    if width < 20:                       # too small to show both ends (and text[-0:] would be the whole text)
        return text[:max(0, width)] + " …"
    head = (width * 3 + 4) // 5
    return f"{text[:head]} …[{len(text) - width} chars cut]… {text[-(width - head):]}"


def _indent(text, pad="    "):
    return pad + text.replace("\n", "\n" + pad)


# ------------------------------------------------------------------ beacon log
def stop_rows(run_dir):
    outs, _ = _jsonl(os.path.join(run_dir, "outputs.jsonl"))
    decs, _ = _jsonl(os.path.join(run_dir, "decisions.jsonl"))
    rows, j = [], 0
    for i, o in enumerate(outs):
        nxt = outs[i + 1].get("ts", float("inf")) if i + 1 < len(outs) else float("inf")
        while j < len(decs) and decs[j].get("ts", 0) < o.get("ts", 0):
            j += 1
        d = None
        # the decision for this stop is logged after it and before the next stop's message
        if j < len(decs) and decs[j].get("ts", 0) < nxt and decs[j].get("session") == o.get("session"):
            d = decs[j]
            j += 1
        rows.append((o, d))
    return rows


def render_stop(o, d, width=_MAX):
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(o.get("ts", 0)))
    sess = (o.get("session") or "-")[:8]
    lines = [f"━━ [{when}] session {sess}",
             "Claude Code said:", _indent(_clip(o.get("text"), width) or "(no text)")]
    if d is None:
        lines.append("beacon: (no decision logged — see hook-errors.log)")
        return "\n".join(lines)
    verdict = f"{d.get('action')}:{d.get('why')}"
    lh = d.get("lighthouse") or {}
    if lh:
        lines.append(f"lighthouse: {lh.get('status')}" + (f" — queued {', '.join(lh.get('added_desc') or lh.get('added') or [])}"
                                                         if lh.get("added") else ""))
    c = dict(d.get("classified") or {})
    fu = c.pop("followups", None)
    if isinstance(fu, dict):
        if fu.get("error"):
            lines.append(f"unfinished-work list: not read by Opus ({_clip(str(fu['error']), 200)}); "
                         "the worker is reminded to record it")
        else:
            lines.append("unfinished-work list read by Opus: "
                         + ", ".join(f"{len(fu.get(k) or [])} {k}" for k in ("work", "human", "optional"))
                         + (f", {len(fu['supervisor'])} about beacon (to the report)" if fu.get("supervisor") else ""))
    if c.get("kind") or c.get("source"):
        lines.append(f"question read by {c.get('source')}: {c.get('kind')}"
                     + (" — approved by standing rule" if c.get("approved") else "")
                     + (f" — chose: {c.get('choice')}" if c.get("choice") else "")
                     + (f" — note: {c.get('note')}" if c.get("note") else ""))
    if d.get("action") == "block":
        lines += [f"beacon replied ({verdict}):", _indent(_clip(d.get("reason"), None if width is None else width * 3)
                                                      or "(reason not recorded)")]
    else:
        lines.append(f"beacon let it stop ({verdict})" + (":" if d.get("report") else ""))
        if d.get("report"):
            lines.append(_indent(_clip(d.get("report"), width)))
    return "\n".join(lines)


def stop_log(run_dir, last=10, width=_MAX):
    rows = stop_rows(run_dir)[-last:] if last else stop_rows(run_dir)
    return "\n\n".join(render_stop(o, d, width) for o, d in rows) or "(no stops recorded yet)"


# ------------------------------------------------------------------ transcripts
def sessions(run_dir):
    """Known sessions, oldest first: {"session", "path", "kind", "ts"}.
    kind "transcript" = Claude Code's own file; "stream" = a `beacon watch` stream."""
    found = {}
    entries, _ = _jsonl(os.path.join(run_dir, "sessions.jsonl"))
    for e in entries:
        p, sid = e.get("transcript_path"), e.get("session")
        if p and sid and sid not in found:
            found[sid] = {"session": sid, "path": os.path.expanduser(p), "kind": "transcript", "ts": e.get("ts", 0)}
    sdir = os.path.join(run_dir, "sessions")
    try:
        names = sorted(n for n in os.listdir(sdir) if n.endswith(".jsonl"))
    except OSError:
        names = []
    for n in names:
        path = os.path.join(sdir, n)
        sid = n
        evs, _ = _jsonl(path)
        for ev in evs:
            if ev.get("session_id"):
                sid = ev["session_id"]
                break
        if sid in found and found[sid]["kind"] == "transcript" and os.path.exists(found[sid]["path"]):
            continue          # Claude Code's own file has everything the stream has
        if sid in found:      # a resumed session: one stream file per `claude -p --resume`
            found[sid]["paths"].append(path)
            continue
        found[sid] = {"session": sid, "path": path, "paths": [path], "kind": "stream",
                      "ts": os.path.getmtime(path)}
    for s in found.values():
        s.setdefault("paths", [s["path"]])
    return sorted(found.values(), key=lambda s: s["ts"])


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def _short_json(obj, width):
    s = json.dumps(obj, ensure_ascii=False)
    return _clip(s, width)


def _result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, dict):
            parts.append(f"[{b.get('type')}]")
    return "\n".join(parts)


def render_entry(e, width=_MAX):
    """One transcript/stream entry -> list of display lines (possibly empty)."""
    t = e.get("type")
    out = []
    if t in ("user", "assistant"):
        msg = e.get("message") or {}
        for b in _blocks(msg.get("content")):
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                who = "You / harness" if t == "user" else "Claude"
                out.append(f"{who}: " + _clip(b["text"], width).replace("\n", "\n    "))
            elif bt == "tool_use":
                out.append(f"  → {b.get('name')}({_short_json(b.get('input', {}), None if width is None else 600)})")
            elif bt == "tool_result":
                mark = " (error)" if b.get("is_error") else ""
                out.append(f"  ← result{mark}: " + _clip(_result_text(b.get("content")), width).replace("\n", "\n      "))
            elif bt == "thinking":
                out.append("  (thinking)")
    elif t == "system":
        st = e.get("subtype")
        if st == "informational":
            out.append("[notice] " + _clip(e.get("content", ""), width))
        elif st == "api_retry":
            out.append(f"[api retry] {e.get('error')} attempt {e.get('attempt')}/{e.get('max_retries')}")
        elif st == "permission_denied":
            why = e.get("decision_reason") or e.get("message") or e.get("decision_reason_type") or ""
            out.append(f"[denied] {e.get('tool_name')}: " + _clip(why, width))
        elif st == "init":
            out.append(f"[session start] model {e.get('model')}, permission mode {e.get('permissionMode')}")
        elif st == "compact_boundary":
            out.append("[context compacted]")
        elif "hook" in str(st).lower() and e.get("content"):
            body = e["content"] if isinstance(e["content"], str) else json.dumps(e["content"], ensure_ascii=False)
            out.append(f"[{st}] " + _clip(body, width))
    elif t == "result":
        out.append(f"[end] {e.get('subtype')}, {e.get('num_turns')} turns, ~${e.get('total_cost_usd') or 0:.4f}"
                   + (" (error)" if e.get("is_error") else ""))
    elif t == "attachment":
        a = e.get("attachment") or {}
        if "hook" in str(a.get("type", "")).lower():
            body = a.get("content") or a.get("stdout") or a.get("stderr") or e.get("rendered") or ""
            out.append(f"[hook {a.get('type')}] " + _clip(body if isinstance(body, str) else json.dumps(body), width))
    return out


def render_transcript(path, last=None, width=_MAX):
    """`path`: one file, or a list of files read in order (a resumed `beacon watch` session)."""
    lines = []
    for p in ([path] if isinstance(path, str) else path):
        entries, _ = _jsonl(p)
        for e in entries:
            lines.extend(render_entry(e, width))
    if last:
        lines = lines[-last:]
    return "\n".join(lines) or "(empty transcript)"


def pick_session(run_dir, session=None):
    known = sessions(run_dir)
    if not known:
        return None
    if session:
        for s in known:
            if s["session"].startswith(session):
                return s
        return None
    return known[-1]


# ------------------------------------------------------------------ follow
def follow_log(run_dir, width=_MAX, interval=2.0, out=print, stop=None):
    """Print new stops as they are decided. `stop` (a callable) ends the loop (tests)."""
    rows = stop_rows(run_dir)
    # count only complete stops: one whose decision is still being written gets printed when it lands
    shown = sum(1 for k, r in enumerate(rows) if r[1] is not None or k < len(rows) - 1)
    while not (stop and stop()):
        rows = stop_rows(run_dir)
        # a stop is complete once its decision is logged (or a newer stop exists)
        ready = [r for k, r in enumerate(rows) if r[1] is not None or k < len(rows) - 1]
        for o, d in ready[shown:]:
            out(render_stop(o, d, width) + "\n")
        shown = max(shown, len(ready))
        time.sleep(interval)


def follow_transcript(path, width=_MAX, interval=1.0, out=print, stop=None, start_at_end=False):
    offset = 0
    if start_at_end:
        _, offset = _jsonl(path)
    while not (stop and stop()):
        entries, offset = _jsonl(path, offset)
        for e in entries:
            for line in render_entry(e, width):
                out(line)
        time.sleep(interval)
