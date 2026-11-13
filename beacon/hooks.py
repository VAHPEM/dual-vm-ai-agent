# -*- coding: utf-8 -*-
"""beacon.hooks — the Claude Code hook entry points.

    python3 -m beacon.hooks stop            (Stop)
    python3 -m beacon.hooks session-start   (SessionStart)
    python3 -m beacon.hooks pre-tool-use    (PreToolUse)
    python3 -m beacon.hooks post-tool-use   (PostToolUse: activity hash only)
    python3 -m beacon.hooks stop-failure    (StopFailure, observability only)
    python3 -m beacon.hooks user-prompt-submit  (UserPromptSubmit: `beacon:` commands, answers in chat)

A session is supervised only if it was launched with BEACON_RUN=<run dir> in
its environment (hooks inherit Claude Code's environment). Every other session
in the same project passes straight through — this is the session isolation the
ralph-loop plugin lacked.

FAIL-OPEN: any exception inside a hook is logged to <run>/hook-errors.log and the
hook exits 0 with no output, i.e. the stop is allowed / the tool runs. A broken
supervisor must never turn into an infinite, money-burning loop.
"""
import json
import os
import re
import sys
import time
import traceback

from .board import refresh_if_open
from .ledger import Ledger, run_dir

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
TASK_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate")
# Tools that make the session submit a prompt to itself later. Such a prompt would reach the
# UserPromptSubmit hook as if the human had typed it (and `beacon:` there acts as the human).
SCHEDULE_TOOLS = ("CronCreate", "ScheduleWakeup")
# A second Claude Code started from Bash inherits BEACON_RUN: its prompts would also look human.
NESTED_CLAUDE = re.compile(r"(?:^|[;&|(`]\s*|\$\(\s*|\b(?:exec|nohup|xargs|npx|sudo)\s+|\benv\s+(?:\S+=\S*\s+)*"
                           r"|\btimeout\s+\S+\s+)(?:\S*/)?claude(?=\s|$|[;&|)`])")
OWN_TOOL_PREFIX = "mcp__beacon__"
# The CLI's human-side commands. The worker could otherwise verify its own candidate, answer its
# own questions, or set notify_cmd (a shell command the hooks run) through Bash.
HUMAN_CLI = re.compile(r"(?:-m\s+beacon(?:\.cli)?|\bbeacon(?:\.cli)?(?:\s|$)|/beacon\s)\s*"
                       r"(?:--run\s+\S+\s+)?(answer|verify|reopen|config|lighthouse|init|setup-project)\b")
WEB_TOOLS = ("WebSearch", "WebFetch")
PROTECTED_NAMES = ("settings.json", "settings.local.json")


def _append_jsonl(run, name, obj):
    try:
        with open(os.path.join(run_dir(run), name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _notify(L, event, report_path=""):
    cmd = (L.config().get("notify_cmd") or "").strip()
    if not cmd:
        return
    env = dict(os.environ, BEACON_EVENT=event, BEACON_REPORT=report_path, BEACON_RUN_DIR=L.dir)
    try:
        from .shellrun import run
        run(cmd, env=env, timeout=20)                          # sh, or Git Bash on Windows (as Claude Code)
    except Exception:  # noqa: BLE001  -- a notification must never break the hook
        pass


# ---------------------------------------------------------------- Stop
def stop(run, inp):
    from .classify import classify
    from .followups import extract
    from .gate import on_stop, stop_lock
    from .lighthouse import run_lighthouse
    from .report import write_report

    L = Ledger.open(run)
    try:
        _append_jsonl(run, "outputs.jsonl", {
            "ts": time.time(), "session": inp.get("session_id"),
            "stop_hook_active": inp.get("stop_hook_active"), "transcript_path": inp.get("transcript_path"),
            "text": inp.get("last_assistant_message") or ""})
        _remember_session(run, inp)
        t0 = time.monotonic()
        with stop_lock(L.dir) as locked:
            if not locked:
                L.record("stop_lock_timeout", {"session": inp.get("session_id")})
            d = on_stop(L, inp, lighthouse=run_lighthouse, classifier=classify, followup=extract,
                        rotate=os.environ.get("BEACON_WATCHDOG") == "1", started=t0)
        record = {"ts": time.time(), "session": inp.get("session_id"), "action": d.action, "why": d.why,
                  "reason": d.reason, "report": d.report, "classified": d.classified or None,
                  "lighthouse": {k: d.lighthouse.get(k) for k in ("status", "added", "added_desc", "detail", "review")}
                  if d.lighthouse else None}
        g = L.gate_state()
        if d.action == "block":
            _append_jsonl(run, "decisions.jsonl", record)
            refresh_if_open(L)
            nxt = L.next_node()
            msg = (f"beacon ▸ kept the session going (stop {g['stops']}, forced {g['forced']}/"
                   f"{L.config()['hard_forced']})" + (f"; next: {nxt['id']} {nxt['title']}" if nxt else ""))
            if d.lighthouse.get("added"):
                msg += (f"; lighthouse{' (you asked)' if d.lighthouse.get('requested_by') else ''} queued "
                        f"{len(d.lighthouse['added'])} idea(s)")
            fu = (d.classified or {}).get("followups") or {}
            if fu.get("work"):
                msg += f"; Claude listed unfinished work -> {len(fu['work'])} node(s): {', '.join(fu['work'][:6])}"
            if d.why == "leftovers_unrecorded":
                msg += "; Claude listed unfinished work (no model to read it): told it to do or record each item"
            told = " ".join((d.reason or "").split())
            msg += "\n→ told Claude: " + (told[:420] + " …" if len(told) > 420 else told)
            return {"decision": "block", "reason": d.reason, "systemMessage": msg[:700]}
        integrity = L.verify_integrity()
        path = write_report(L, d, integrity)
        record["report_path"] = path
        _append_jsonl(run, "decisions.jsonl", record)
        refresh_if_open(L)
        # never run a command taken from a ledger that failed its check; no notice for a stop the
        # human chose (pause, plan mode) or a pause for background work
        if integrity[0] and d.why not in ("paused", "plan_mode", "background_wait"):
            _notify(L, "stop:" + d.why, path)
        msg = f"beacon ▸ let the session stop: {d.why}. Report: {path}"
        qs = L.questions("open")
        pend = [p for p in L.problems() if p["status"] == "pending_verification"]
        prop = [p for p in L.problems() if p["status"] == "proposed"]
        if qs or pend or prop:
            from .chat import _questions
            msg += "\nWaiting on you:"
            for p in pend[:5]:
                msg += (f"\n{p['id']} candidate: {(p.get('candidate') or '')[:200]}  -> `beacon: verify {p['id']}` "
                        f"or `beacon: verify {p['id']} reject <why>`")
            for p in prop[:5]:
                msg += (f"\n{p['id']} proposed by Claude: {p['title'][:160]}  -> `beacon: accept {p['id']}` "
                        f"or `beacon: reject {p['id']} <why>`")
            if qs:
                msg += ("\n" + _questions(L)[:1200] + "\nReply with `#N <answer>`"
                        + (" (or just reply: there is one question)" if len(qs) == 1 else ""))
            msg += "\n(`beacon: help` lists the chat commands.)"
        elif d.report:
            msg += "\n" + d.report[:400]
        return {"systemMessage": msg}
    finally:
        L.close()


def _remember_session(run, inp):
    """Record where Claude Code keeps this session's transcript (every hook input carries it)."""
    sid, path = inp.get("session_id"), inp.get("transcript_path")
    if not sid or not path:
        return
    try:
        with open(os.path.join(run_dir(run), "sessions.jsonl"), encoding="utf-8") as fh:
            if any(json.loads(x).get("session") == sid for x in fh if x.strip()):
                return
    except (OSError, ValueError):
        pass
    _append_jsonl(run, "sessions.jsonl", {"ts": time.time(), "session": sid, "transcript_path": path,
                                          "source": inp.get("source") or inp.get("hook_event_name")})


# ---------------------------------------------------------------- SessionStart
def session_start(run, inp):
    _remember_session(run, inp)
    L = Ledger.open(run)
    try:
        ok, why = L.verify_integrity()
        src = inp.get("source", "")
        if not ok:
            ctx = (f"A beacon run is configured for this session, but its ledger failed the integrity "
                   f"check ({why}). Tell the human and do not write to the ledger.")
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
        L.record("session_start", {"session": inp.get("session_id"), "source": src})
        answered = [q for q in L.questions("answered") if not q["delivered"]]
        parts = [
            f"A beacon run \"{L.title()}\" supervises this session (session start reason: {src or 'startup'}).",
            "The ledger below is the source of truth for what is done and what is left. "
            "The Stop gate keeps the session going while workable nodes remain.",
            "Tools (beacon MCP server): ledger_view, add_node, start_node, attach_evidence, set_status, "
            "propose_done, claim_near_done, ask, mark_waiting, vm_check, propose_problem.",
            "", L.render_tree(max_evidence=2)[:6000],
        ]
        for q in answered:
            parts.append(f"The human answered question #{q['id']} ({q['kind']}): {q['answer']}")
        if answered:
            L.mark_delivered([q["id"] for q in answered])
        nxt = L.next_node()
        if nxt is not None:
            parts.append(f"Next node by the ledger's ordering: {nxt['id']} \"{nxt['title']}\".")
        open_n, total = len(L.open_problems()), len(L.problems())
        msg = f"beacon ▸ run \"{L.title()}\" supervises this session: {open_n}/{total} problem(s) open."
        if os.path.exists(os.path.join(inp.get("cwd") or os.getcwd(), ".claude", "beacon")):
            msg += (" In this chat: `! .claude/beacon log` (what beacon replied), "
                    "`! .claude/beacon transcript -n 40`, `! .claude/beacon status`.")
        else:
            msg += f" Its log: `python3 -m beacon log -f --run {L.dir}` in a terminal."
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "\n".join(parts)[:9000]},
                "systemMessage": msg}
    finally:
        L.close()


# ---------------------------------------------------------------- PreToolUse
def _variants(path):
    """A path as it may appear in a shell command: absolute, ~/..., $HOME/..."""
    out = {path}
    home = os.path.expanduser("~")
    if home and path.startswith(home + os.sep):
        rest = path[len(home):]
        out |= {"~" + rest, "$HOME" + rest, "${HOME}" + rest}
    return out


def protected_paths(run):
    rd = os.path.realpath(run_dir(run))
    pkg = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    return [rd, pkg]


def _under(path, roots):
    rp = os.path.realpath(os.path.expanduser(path))
    return any(rp == r or rp.startswith(r + os.sep) for r in roots)


def guard(tool, tool_input, roots, cfg):
    """Return a denial reason, or None to let the tool run."""
    ti = tool_input or {}
    if tool in WEB_TOOLS and cfg.get("no_web"):
        return "Web tools are disabled for this beacon run (no_web)."
    if tool in TASK_TOOLS and cfg.get("block_internal_tasks", True):
        return ("During a beacon run the ledger is the task list: use add_node / start_node / set_status "
                "instead of the internal task tools.")
    if tool in SCHEDULE_TOOLS:
        return ("Scheduled prompts are disabled during a beacon run (the Stop gate keeps the session going; "
                "use mark_waiting for a job that needs time).")
    if tool in WRITE_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            p = ti.get(key)
            if not p:
                continue
            if _under(p, roots):
                return "That path belongs to the beacon supervisor; change the ledger only through the beacon tools."
            base = os.path.basename(p)
            if base == ".mcp.json" or (base in PROTECTED_NAMES + ("beacon",) and
                                        os.path.basename(os.path.dirname(os.path.abspath(p))) == ".claude"):
                return "Claude Code settings and MCP configuration are off-limits during a beacon run."
    if tool.startswith("mcp__") and not tool.startswith(OWN_TOOL_PREFIX):
        # another MCP server's tool (a filesystem or shell server) pointed at the supervisor's files
        blob = json.dumps(ti, ensure_ascii=False)
        needles = {"ledger.db"} | {v for r in roots for v in _variants(r)}
        hit = next((n for n in needles if n and n in blob), None)
        if hit:
            return f"This tool call touches the beacon supervisor ({hit!r}). Use the beacon tools instead."
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        if HUMAN_CLI.search(cmd):
            return ("That beacon command is for the human (answer, verify, reopen, config, lighthouse, init, "
                    "setup-project). Use the beacon tools; ask() if you need the human.")
        if NESTED_CLAUDE.search(cmd):
            return ("Starting another Claude Code session is disabled during a beacon run; use the Agent tool "
                    "for a subagent.")
        needles = {"BEACON_RUN", "ledger.db", ".claude/settings", ".mcp.json"}
        for r in roots:
            needles |= _variants(r)
        hit = next((n for n in needles if n and n in cmd), None)
        if hit:
            return (f"This command touches the beacon supervisor ({hit!r}). Use the beacon tools "
                    "(ledger_view, attach_evidence, set_status, ...) instead.")
    return None


def pre_tool_use(run, inp):
    cfg = {}
    try:  # the path rules do not depend on the ledger: a broken ledger must not disarm them
        L = Ledger.open(run)
        try:
            cfg = L.config()
        finally:
            L.close()
    except Exception:  # noqa: BLE001
        _log_error(run, "pre-tool-use (config unreadable; path rules still applied)")
    reason = guard(inp.get("tool_name", ""), inp.get("tool_input") or {}, protected_paths(run), cfg)
    if not reason:
        return None
    _append_jsonl(run, "denials.jsonl", {"ts": time.time(), "tool": inp.get("tool_name"), "reason": reason,
                                         "input": str(inp.get("tool_input"))[:300]})
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason},
            "systemMessage": f"beacon guard ▸ blocked {inp.get('tool_name')}: {reason}"[:400]}


# ---------------------------------------------------------------- PostToolUse
def post_tool_use(run, inp):
    """Append a hash of the tool output to <run>/activity.jsonl (no ledger write: this
    runs after every tool call and must stay cheap). The gate reads it only to tell
    "working without recording" from "idle"."""
    from .gate import activity_line
    tool = str(inp.get("tool_name", ""))
    if tool.startswith(OWN_TOOL_PREFIX):
        # a beacon tool changed the ledger: keep the live graph current (only if it was opened once)
        if os.path.exists(os.path.join(run_dir(run), "graph.html")):
            L = Ledger.open(run)
            try:
                refresh_if_open(L)
            finally:
                L.close()
        return None
    line = activity_line(tool, inp.get("tool_response"))
    line["ts"] = time.time()
    _append_jsonl(run, "activity.jsonl", line)
    return None


# ---------------------------------------------------------------- UserPromptSubmit
def _browser():
    """`beacon: graph` opens the page in the browser of the machine the hooks run on."""
    if os.environ.get("BEACON_NO_BROWSER"):
        return None
    import webbrowser
    return webbrowser.open
def _waiting_for_human(run, session):
    """True if this session's last stop was let through because Claude waits on the human."""
    try:
        with open(os.path.join(run_dir(run), "decisions.jsonl"), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("session") == session:
            return d.get("action") == "allow" and d.get("why") == "awaiting_human"
    return False


def user_prompt_submit(run, inp):
    """`beacon: <command>` and replies to open questions, typed in the chat (see beacon.chat)."""
    if os.environ.get("BEACON_WATCHDOG") == "1":   # under `beacon watch` the prompts are the watchdog's
        return None
    from .chat import handle
    _remember_session(run, inp)
    L = Ledger.open(run)
    try:
        if not L.verify_integrity()[0]:
            return None
        r = handle(L, inp.get("prompt") or "", _waiting_for_human(run, inp.get("session_id")), opener=_browser())
        if r and r.get("changed"):
            refresh_if_open(L)
    finally:
        L.close()
    if r is None:
        return None
    _append_jsonl(run, "chat.jsonl", {"ts": time.time(), "session": inp.get("session_id"),
                                      "prompt": (inp.get("prompt") or "")[:500], "block": r["block"],
                                      "show": r["show"][:2000]})
    if r["block"]:
        return {"decision": "block", "reason": r["show"]}
    out = {"systemMessage": r["show"][:600]}
    if r.get("note"):
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": "[beacon] " + r["note"][:4000]}
    return out


# ---------------------------------------------------------------- StopFailure
def stop_failure(run, inp):
    _append_jsonl(run, "failures.jsonl", {
        "ts": time.time(), "session": inp.get("session_id"),
        "error_type": inp.get("error_type"), "error": inp.get("error"),
        "message": (inp.get("error_message") or "")[:500]})
    L = Ledger.open(run)
    try:
        if L.verify_integrity()[0]:
            _notify(L, "stop_failure:" + str(inp.get("error_type") or inp.get("error") or "unknown"))
    finally:
        L.close()
    return None  # output and exit code are ignored for StopFailure


UNSUPERVISED = ("beacon is set up in this project, but this session was started without BEACON_RUN, so beacon is "
                "NOT supervising it (no ledger tools, no stop checks). To supervise it: exit, then run "
                "`BEACON_RUN=<run dir> claude --continue`.")

HANDLERS = {"stop": stop, "session-start": session_start, "pre-tool-use": pre_tool_use,
            "post-tool-use": post_tool_use, "stop-failure": stop_failure,
            "user-prompt-submit": user_prompt_submit}


def _log_error(run, event):
    try:
        with open(os.path.join(run_dir(run), "hook-errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    try:
        # Claude Code sends UTF-8. Read bytes: on Windows the default text encoding is the ANSI code page
        # (cp1252, ...), which garbles a Vietnamese chat command or a non-ASCII path.
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        raw = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    except Exception:  # noqa: BLE001
        raw = ""
    run = os.environ.get("BEACON_RUN", "").strip()
    if (not run or run.startswith("${")) and event == "session-start":
        # The hooks are installed in this project but this session is unsupervised. Say so to the human once,
        # at the start: otherwise it shows up later as broken beacon tools (measured 2026-11-12, a restart
        # with `claude --continue` that dropped BEACON_RUN).
        sys.stdout.write(json.dumps({"systemMessage": UNSUPERVISED}, ensure_ascii=True))
        sys.stdout.flush()
        return 0
    if not run or event not in HANDLERS:
        return 0
    try:
        inp = json.loads(raw) if raw.strip() else {}
        if not isinstance(inp, dict):
            inp = {}
        out = HANDLERS[event](run, inp)
        if out:
            # Pure ASCII JSON (non-ASCII as \uXXXX): whatever the console or pipe encoding, nothing to garble
            # and nothing that cannot be encoded (a UnicodeEncodeError here would fail open and let the stop pass).
            sys.stdout.write(json.dumps(out, ensure_ascii=True))
            sys.stdout.flush()
    except Exception:  # noqa: BLE001  -- fail-open, see module docstring
        _log_error(run, event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
