# -*- coding: utf-8 -*-
"""beacon.report — what the human reads when a run stops (REPORT.md), and the
graph view. Near-done work goes first: a stop is a checkpoint, and the cheapest
thing to do next is usually to resume exactly there."""
import html
import json
import os
import re

_STATUS_STYLE = {
    "todo": "fill:#f1efe8,stroke:#888780", "in_progress": "fill:#e6f1fb,stroke:#185fa5",
    "done": "fill:#eaf3de,stroke:#3b6d11", "dead_end": "fill:#fcebeb,stroke:#a32d2d",
    "blocked": "fill:#faeeda,stroke:#854f0b", "superseded": "fill:#f1efe8,stroke:#b4b2a9",
    "abandoned": "fill:#f1efe8,stroke:#b4b2a9", "pending_verification": "fill:#eeedfe,stroke:#534ab7",
}


def render_report(L, decision=None, integrity=None):
    ok, why = integrity if integrity is not None else L.verify_integrity()
    g = L.gate_state()
    probs = L.problems()
    lines = [f"# Beacon report — {L.title()}", ""]
    if decision is not None:
        lines.append(f"Stopped because: **{decision.why}**")
        if decision.report:
            lines += ["", decision.report]
        lines.append("")
    lines.append(f"Ledger integrity: {'OK' if ok else 'FAILED'} — {why}")
    done = sum(p["status"] == "done" for p in probs)
    lines += [f"Problems: {done}/{len(probs)} done. Gate: {g['stops']} stops, {g['forced']} forced "
              f"continuations, {g['lighthouse_calls']} lighthouse calls.", ""]

    near = L.near_done_nodes()
    if near:
        lines.append("## Near done (resume here first)")
        for n in near:
            steps = json.loads(n["remaining"] or "[]")
            lines.append(f"- {n['id']} \"{n['title']}\" — claimed {n['near_done_claims']}x; remaining: "
                         + ("; ".join(steps) or "?"))
        lines.append("")

    aw = L.awaiting_human()
    if aw["questions"] or aw["pending_verification"] or aw["blocked"] or aw.get("proposed"):
        lines.append("## Needs you")
        for q in aw["questions"]:
            lines.append(f"- question #{q['id']} ({q['kind']}, node {q['node_id'] or '-'}): {q['question']}"
                         f"  → in the chat `#{q['id']} <answer>`, or `beacon answer {q['id']} \"...\"`")
        for p in aw["pending_verification"]:
            lines.append(f"- {p['id']} candidate `{p['candidate']}` awaits verification"
                         f"  → `beacon: verify {p['id']}` in the chat, or `beacon verify {p['id']}` (or `--reject \"why\"`)")
        for p in aw.get("proposed") or []:
            lines.append(f"- {p['id']} proposed by Claude: {p['title']}  → `beacon: accept {p['id']}` / "
                         f"`beacon: reject {p['id']} <why>`")
        for n in aw["blocked"]:
            if not n["blocked_by_question"]:
                lines.append(f"- {n['id']} blocked: {n['reason']}  → `beacon reopen {n['id']}`")
        lines.append("")

    work = L.workable() if ok else []
    if work:
        lines.append("## Still open")
        lines += [f"- {n['id']} [{n['status']}] {n['title']}" for n in work]
        lines.append("")

    dead = [n for n in L.nodes() if n["status"] == "dead_end"]
    if dead:
        lines.append("## Dead ends and the assumptions they rest on")
        lines += [f"- {n['id']} {n['title']}: {n['reason']} — assumes: {n['assumption']}" for n in dead]
        lines.append("")

    fus = [n for n in L.nodes() if n["source"] == "followup" and n["kind"] != "problem"]
    if fus:
        lines.append("## Unfinished work Claude listed, turned into nodes")
        lines += [f"- {n['id']} [{n['status']}] {n['title']}" for n in fus[-30:]]
        lines.append("")
    fb = [json.loads(e["payload"]) for e in L.events(etype="supervisor_feedback")]
    if fb:
        lines.append("## What Claude reported about beacon itself (for you; not part of the run)")
        lines += [f"- {p.get('title', '')}" + (f" -- {p['detail']}" if p.get("detail") and p["detail"] != p.get("title")
                                               else "") for p in fb[-20:]]
        lines.append("")
    approved = [q for q in L.questions() if q["status"] == "auto_approved"]
    if approved:
        lines.append("## Approved by your standing rule")
        lines += [f"- #{q['id']} {q['question']}" for q in approved[-20:]]
        lines.append("")

    flags = L.flags()
    if flags:
        lines.append("## Reviewer flags")
        lines += [f"- {f['node']} ({f['kind']}): {f['why']}" for f in flags[-20:]]
        lines.append("")

    lines += ["## Ledger", "```", L.render_tree(max_evidence=2), "```", "",
              "Resume: `BEACON_RUN=%s claude` and tell it to continue from the beacon ledger." % L.dir]
    return "\n".join(lines)


def render_mermaid(L):
    out = ["flowchart TD"]
    used = set()
    for n in L.nodes():
        nid = n["id"].replace(".", "_")
        label = re.sub(r'[<>"`]', "'", f"{n['id']}: {n['title']}"[:80])
        out.append(f'  {nid}["{label}<br/><i>{n["status"]}</i>"]')
        out.append(f"  class {nid} st_{n['status']}")
        used.add(n["status"])
        if n["parent"]:
            out.append(f"  {n['parent'].replace('.', '_')} --> {nid}")
    for st in sorted(used):
        out.append(f"  classDef st_{st} {_STATUS_STYLE.get(st, 'fill:#fff,stroke:#888')}")
    return "\n".join(out)


def render_html(L):
    """A self-contained page that renders the mermaid graph (mermaid from a CDN)."""
    mer = html.escape(render_mermaid(L), quote=False)
    title = html.escape(L.title() or "beacon")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title} — beacon graph</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px}}</style></head>
<body><h2>{title}</h2><pre class="mermaid">
{mer}
</pre>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
mermaid.initialize({{startOnLoad:true, securityLevel:'strict'}});
</script></body></html>
"""


def write_report(L, decision=None, integrity=None):
    path = os.path.join(L.dir, "REPORT.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_report(L, decision, integrity))
    return path


def _jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def stop_log(run_dir, last=10, width=600):
    from .logview import stop_log as _stop_log
    return _stop_log(run_dir, last=last, width=width)


_REFUSALISH = re.compile(r"\b(can(?:'|’)?t|cannot|won(?:'|’)?t|unable|decline|not able|refuse)\b|không thể|từ chối", re.I)


def calibrate_refusals(run_dir):
    """What the refusal heuristic flags in the recorded stop messages, and the near
    misses (refusal-ish wording it did not flag) to check by eye."""
    from .gate import possible_refusal
    texts = [o.get("text") or "" for o in _jsonl(os.path.join(run_dir, "outputs.jsonl"))]
    flagged = [t for t in texts if possible_refusal(t)]
    near = [t for t in texts if not possible_refusal(t) and _REFUSALISH.search(t)]
    out = [f"{len(texts)} stop messages; {len(flagged)} flagged as possible refusals; "
           f"{len(near)} near misses (refusal-ish wording, not flagged)."]
    for title, group in (("FLAGGED (the gate stops the run on these)", flagged),
                         ("NEAR MISSES (would a human call these refusals?)", near)):
        if group:
            out.append(f"\n== {title}")
            for t in group[:15]:
                out.append("- " + t.strip()[:300].replace("\n", " "))
    return "\n".join(out)
