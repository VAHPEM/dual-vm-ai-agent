# -*- coding: utf-8 -*-
"""beacon.tools — the worker-facing operations, as plain functions.

beacon.mcp_server publishes these as MCP tools; tests call them directly. Each
opens the run named by BEACON_RUN (or an explicit `run`), acts as actor
"worker", and returns a short text the worker reads. Rule violations come back
as text starting with "REFUSED:" instead of an exception, so the worker sees
the rule and can comply.
"""
import functools
import os

from .ledger import CHOICE_RULE, Ledger, LedgerError


def _run(run=None):
    run = run or os.environ.get("BEACON_RUN", "").strip()
    # .mcp.json passes "${BEACON_RUN}"; when Claude Code was started without it the server gets that text as is
    # (measured 2026-11-12: "no ledger at <cwd>/${BEACON_RUN}/ledger.db", which the worker could not act on).
    if not run or run.startswith("${"):
        raise LedgerError("this Claude Code session was started without BEACON_RUN, so beacon is not supervising it "
                          "(no ledger, no hooks). Tell the human: exit, then start again with "
                          "`BEACON_RUN=<run dir> claude --continue`. Do not edit .mcp.json or the settings")
    return run


def _tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, run=None, **kwargs):
        try:
            L = Ledger.open(_run(run))
        except LedgerError as e:
            return f"REFUSED: {e}"
        try:
            ok, why = L.verify_integrity()
            if not ok:
                return f"REFUSED: ledger integrity check failed ({why}). Tell the human; do not retry."
            return fn(L, *args, **kwargs)
        except LedgerError as e:
            return f"REFUSED: {e}"
        finally:
            L.close()
    return wrapper


@_tool
def ledger_view(L, node_id=None):
    """The ledger as a tree (or one node's subtree), with recent evidence."""
    head = f"Run: {L.title()}\n"
    nxt = L.next_node()
    tail = f"\n\nNext node by the ledger's ordering: {nxt['id']} \"{nxt['title']}\"" if nxt else ""
    return head + L.render_tree(root=node_id or None, max_evidence=3) + tail


@_tool
def add_node(L, parent, title, rationale="", kind=None):
    nid = L.add_node(parent, title, rationale=rationale, kind=kind or None, actor="worker")
    return f"added {nid}"


@_tool
def start_node(L, node_id):
    L.start(node_id, actor="worker")
    return f"{node_id} is in_progress"


@_tool
def attach_evidence(L, node_id, summary, content="", ref="", kind="output"):
    eid, dup = L.attach_evidence(node_id, summary, ref=ref, content=content or None, kind=kind, actor="worker")
    if dup:
        return (f"evidence #{eid} recorded, but it is identical to evidence #{dup}: it does not count "
                "as new evidence.")
    return f"evidence #{eid} recorded"


@_tool
def set_status(L, node_id, status, reason="", assumption="", superseded_by="", evidence_ids=None):
    L.set_status(node_id, status, reason=reason, assumption=assumption,
                 superseded_by=superseded_by or None, evidence_ids=evidence_ids or (), actor="worker")
    return f"{node_id} -> {status}"


@_tool
def propose_done(L, problem_id, candidate, evidence_ids, summary=""):
    r = L.propose_done(problem_id, candidate, evidence_ids or [], summary=summary, actor="worker")
    if r["verified"] is True:
        return f"{problem_id} VERIFIED and done. {r['detail']}"
    if r["verified"] is False:
        hint = ""
        if r["detail"].strip().endswith(":"):           # "exit N:" and nothing after it: the verifier printed nothing
            hint = (" It printed nothing, so the check itself said no: re-read the problem's note in ledger_view for "
                    "the exact form of the answer (often the bare value, not a sentence) before changing anything "
                    "else.")
        return (f"{problem_id}: the verifier REJECTED the candidate ({r['detail']}). The problem is "
                "back in progress; the refuted candidate is recorded." + hint)
    return f"{problem_id} is pending human verification. Continue with other open problems."


@_tool
def claim_near_done(L, node_id, remaining_steps):
    if isinstance(remaining_steps, str):
        remaining_steps = [s for s in remaining_steps.splitlines() if s.strip()]
    L.claim_near_done(node_id, remaining_steps, actor="worker")
    return f"near-done claim recorded for {node_id}; the gate will hold you to these steps"


@_tool
def propose_problem(L, title, rationale, evidence_ids=None):
    r = L.propose_problem(title, rationale, evidence_ids=evidence_ids or (), actor="worker")
    if r["status"] == "todo":
        return f"added {r['id']} \"{title}\": it is open now and part of the run; work on it like any other problem"
    return (f"proposed {r['id']} \"{title}\": it waits for the human to accept it (beacon: accept {r['id']}). "
            "Do not work on it until then; carry on with the open problems.")


DECIDER_TRANSPORT = None   # tests inject a fake Messages API; None = the real one
SAMPLE_TRANSPORT = None    # the same, for the real-sample check of a need_permission request


@_tool
def ask(L, kind, question, node_id="", options=None, context=""):
    if kind == "need_permission":
        cfg = L.config()
        held, basis = False, "requested through ask"
        if cfg.get("permission_policy", "approve") == "approve" and cfg.get("ask_before_running_real_samples", False):
            from .classify import check_real_sample
            used = L.db.execute("SELECT COUNT(*) FROM events WHERE type IN ('classified','sample_check')").fetchone()[0]
            ctx = "<context>\n" + L.render_tree(root=L.problem_of(node_id)["id"] if node_id else None,
                                                  max_evidence=1)[:6000].replace("<", "&lt;") + "\n</context>"
            held, source, why = check_real_sample(question + "\n" + (context or ""), cfg, transport=SAMPLE_TRANSPORT,
                                                  context=ctx, calls_used=used)
            if source == "model":
                L.record("sample_check", {"question": question[:300], "real_sample": held, "why": why,
                                          "model": cfg.get("classifier_model")}, actor="gate")
            basis += f"; real-sample check ({source}): no" + (f" — {why}" if why else "")
            if held:
                question = f"[real malware sample, checked by {source}: {why}] {question}"
        if cfg.get("permission_policy", "approve") == "approve" and not held:
            L.approve_request(question, node_id=node_id or None, basis=basis, actor="worker")
            return ("Approved — the human's standing rule grants requests to run things (locally or on the VMs). "
                    "Go ahead, and record what you ran and its output with attach_evidence.")
    r = L.ask(kind, question, node_id=node_id or None, options=options or (), actor="worker")
    if kind == "approach_choice":
        from .decider import decide
        d = decide(L, node_id, question, r["created"], brief=context, transport=DECIDER_TRANSPORT)
        if d["status"] == "ok":
            first = d["order"][0]
            return (f"The decider ({d['model']}) chose {first} \"{L.node(first)['title']}\". Order: "
                    + " > ".join(d["order"]) + (f" ({d['new']} is an option it added)" if d.get("new") else "")
                    + f".\nWhy: {d['why']}\nFirst step: {d['first_step']}\nSwitch to the next if: {d['switch_if']}\n"
                    f"Start {first} now with start_node. If a result contradicts the decision, record it "
                    "(attach_evidence), close the option as a dead end with its assumption, and take the next. "
                    "Not sent to the human; none of the options is lost.")
        return ("Not sent to the human: each option is now an approach, queued in the order you gave ("
                + ", ".join(r["created"]) + f"). Start {r['created'][0]} now with start_node. If the order "
                f"was not already {CHOICE_RULE}, start the one that is. When an option fails, close it as a "
                f"dead end with its assumption and take the next; none of them is lost. (Decider: {d['status']}"
                f" — {d.get('detail', '')})")
    return (f"question #{r['qid']} recorded for the human"
            + (f"; {node_id} is blocked until it is answered" if node_id else "")
            + ". Continue with other workable nodes.")


@_tool
def mark_waiting(L, node_id, job, host="kali", max_minutes=30):
    w = L.mark_waiting(node_id, job, host=host, max_minutes=max_minutes, actor="worker")
    return f"waiting on job {w['job']} ({w['host']}) for up to {w['minutes']} min; poll it with dualvm.jobs.job_poll"


VM_RUNNER = None   # tests inject a fake; None = dualvm.shell.run_script


@_tool
def vm_check(L, host="kali", node_id="", set_baseline=False):
    from . import vmstate
    try:
        fp = vmstate.fingerprint(host, runner=VM_RUNNER)
    except Exception as e:  # noqa: BLE001  -- no lab, SSH down, probe refused: report, don't crash
        return f"vm_check failed on {host}: {e}"
    base = L.vm_baseline(host)
    diff = None
    if set_baseline or base is None:
        L.set_vm_baseline(host, fp, actor="worker")
        msg = (f"baseline recorded for {host}: {len(fp['processes'])} processes, "
               f"{len(fp['ports'])} listening ports, samples/ digest {fp['files']}")
    else:
        diff = vmstate.diff(base, fp)
        msg = f"{host}: " + vmstate.describe(diff)
    if node_id:
        eid, dup = L.attach_evidence(node_id, f"VM state check ({host}): {msg[:300]}",
                                     content=vmstate.as_content(fp, diff), kind="note", actor="worker")
        msg += f" (evidence #{eid}" + ("; same state as an earlier check" if dup else "") + ")"
    return msg


TOOLS = [ledger_view, add_node, start_node, attach_evidence, set_status, propose_done,
         claim_near_done, ask, mark_waiting, vm_check, propose_problem]
