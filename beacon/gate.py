# -*- coding: utf-8 -*-
"""beacon.gate — what the Stop hook decides.

`on_stop(ledger, hook_input, lighthouse=...)` returns a Decision:
  * block  -> Claude Code continues, with `reason` as the next instruction;
  * allow  -> the session may end; `report` is for the human.

The decision reads the LEDGER, never the worker's prose. The last assistant
message is used for exactly three things: spotting a possible refusal (route to
the human, never push through it), spotting options offered to the human
("if you want I can ...") so the nudge asks for them to be recorded as nodes,
and spotting a free-text question so the nudge points at ask().

Escalation ladder:
  level 0  nudge: name the next node and its recorded remaining steps ($0)
  level 1  lighthouse: after k_stall stops without a conclusion, or k_idle stops
           without any new evidence, or repeated near-done claims with no new
           evidence (audit). Non-preemptive: it only queues todo nodes.
  level 2  allow the stop and write the report: lighthouse dry for
           lighthouse_dry_max rounds, call budget spent, or a budget cap hit.

"Progress" = a node concluded (done / dead_end / superseded), a candidate
verified or refuted, or the human answered a question. New *unique* evidence
only keeps the idle counter at zero; on a node the worker claimed is near done
it also counts as progress (so a real near-finish is never escalated).
"""
import contextlib
import hashlib
import inspect
import json
import math
import os
import re
import time
from dataclasses import dataclass, field

from .ledger import CHOICE_RULE

_REFUSAL = [re.compile(p, re.I) for p in (
    r"\bI can(?:'|’)?t help with\b", r"\bI(?:'|’)m not able to help\b",
    r"\bI (?:won(?:'|’)?t|will not) (?:help|assist|provide|do that)\b",
    r"\bI can(?:'|’)?t assist\b", r"\bI(?:'|’)m unable to (?:help|assist)\b",
    r"\bcan(?:'|’)?t (?:help|assist) with (?:that|this)\b",
    r"\bI (?:have to|must) decline\b",
    r"không thể (?:hỗ trợ|giúp)", r"(?:mình|tôi) (?:sẽ )?không (?:thể )?(?:giúp|hỗ trợ|làm) (?:việc|điều) (?:này|đó)",
)]
_REFUSAL_MAX_LEN = 1200   # long analytic messages that merely mention "can't help" are not refusals

_OFFER = [re.compile(p, re.I) for p in (
    r"\bif you(?:'d)? (?:like|want)\b", r"\bwant me to\b", r"\bshould I\b", r"\bI could also\b",
    r"\bnếu (?:bạn )?muốn\b", r"\bbạn có muốn\b", r"\bcó nên\b",
)]


@contextlib.contextmanager
def stop_lock(run_dir, timeout=120.0):
    """One Stop decision at a time per run. The gate state is read, changed and written back whole, so two
    sessions stopping at once (two windows on one project, the watchdog plus an interactive session) would
    lose each other's counts. Yields whether the lock was taken; after `timeout` the decision goes ahead
    without it rather than letting Claude Code kill the hook."""
    fh = open(os.path.join(run_dir, "gate.lock"), "a+b")
    got, end = False, time.monotonic() + timeout
    try:
        while True:
            try:
                _lock_file(fh, True)
                got = True
                break
            except OSError:
                if time.monotonic() >= end:
                    break
                time.sleep(0.2)
        yield got
    finally:
        if got:
            try:
                _lock_file(fh, False)
            except OSError:
                pass
        fh.close()


def _lock_file(fh, lock):
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


@dataclass
class Decision:
    action: str                 # "block" | "allow"
    why: str                    # short machine-readable cause
    reason: str = ""            # shown to Claude when blocking
    report: str = ""            # extra text for the human report
    lighthouse: dict = field(default_factory=dict)
    classified: dict = field(default_factory=dict)   # {"kind", "source": model|model-unsure|heuristic, "note", ...}


def possible_refusal(text):
    text = (text or "").strip()
    if not text or len(text) > _REFUSAL_MAX_LEN:
        return False
    return any(p.search(text) for p in _REFUSAL)


def offers_options(text):
    return any(p.search(text or "") for p in _OFFER)


def asks_question(text):
    return (text or "").rstrip().endswith("?")


def _near_done_audit(L, g, cfg):
    """Nodes whose near-done claims piled up without new evidence."""
    out, audited = [], g.get("audited", {}) or {}
    for n in L.near_done_nodes():
        if n["near_done_claims"] < int(cfg["near_done_claims_max"]):
            continue
        if n["near_done_claims"] <= int(audited.get(n["id"], 0)):
            continue
        unique_now = len(L.evidence(n["id"], unique_only=True))
        if unique_now <= n["near_done_evidence"]:
            out.append(n["id"])
    return out


def _classify(L, classifier, text, cfg, asked=()):
    """Call the classifier -> dict(kind, question, source, note, choice, why, real_sample). A real one
    gets its call budget and the ledger/session context; a simple test double gets (text, cfg) and may
    return a tuple (kind, question, source[, note])."""
    try:
        params = inspect.signature(classifier).parameters
    except (TypeError, ValueError):
        params = {}
    if "context" not in params:
        res = classifier(text, cfg)
    else:
        def context():
            from .decider import _session_text
            nxt = L.next_node()
            root = L.problem_of(nxt["id"])["id"] if nxt is not None else None
            tree = L.render_tree(root=root, evidence=True, max_evidence=2) if root else L.render_tree(max_evidence=1)
            session = _session_text(L, "observations", 20000)
            esc = lambda t: t.replace("<", "&lt;")  # noqa: E731  -- data from the worker and the analysed files
            prior = "\n".join(f"#{q['id']} ({q['kind']}): {q['question'][:300]}" for q in asked)
            return ((f"<asked_this_turn>(already asked through the ask tool; a question that is one of these is "
                     f"rhetorical, note \"already asked as #N\")\n{esc(prior)}\n</asked_this_turn>\n\n" if prior else "")
                    + f"<ledger>\n{esc(tree[:8000])}\n</ledger>"
                    + (f"\n\n<session>(recent observations: tool calls and outputs)\n{esc(session)}\n</session>"
                       if session else ""))

        used = L.db.execute("SELECT COUNT(*) FROM events WHERE type='classified'").fetchone()[0]
        res = classifier(text, cfg, context=context, calls_used=used)
    if isinstance(res, dict):
        return dict({"note": "", "choice": "", "why": "", "real_sample": False}, **res)
    kind, question, source = res[:3]
    return {"kind": kind, "question": question, "source": source, "note": res[3] if len(res) > 3 else "",
            "choice": "", "why": "", "real_sample": False}


STEP_BACK = ("You seem stuck ({why}). Before more of the same, step back: (1) re-read the problem statement and the "
             "files you were given, start to end; (2) re-read your own evidence (ledger_view) and list every step "
             "you took: what did you skip, assume, or never check?; (3) list the assumptions under your dead ends and "
             "test the weakest one directly; (4) widen the view: other files and components, other approach classes "
             "(static survey, dynamic tracing, emulating one routine, symbolic execution, patching, inverting a "
             "transform instead of brute-forcing it); (5) check the VM state (vm_check); (6) the obvious answer may "
             "be a decoy.")


def build_nudge(L, g, text, lh=None, waiting=None, answered=(), flags=(), asked=None, choice=False,
                review_note="", picked=None, approved=None, cfg=None, followups=None):
    probs = L.problems()
    open_p = L.open_problems()
    lines = [f"Beacon ledger: {len(open_p)} of {len(probs)} problem(s) still open, so the run is not finished."]
    # Stuck: the step-back comes first (the nudge is capped; this is the part that must not be cut). The
    # lighthouse's own review, written by Opus from the ledger, the evidence and the worker's actual steps,
    # goes before the generic reminder.
    stall_n, idle_n = int(g.get("stall", 0)), int(g.get("idle", 0))
    # stuck = no new evidence for 2 stops, or close to the lighthouse threshold without concluding anything
    # (new evidence alone keeps a long piece of work from looking stuck), or the lighthouse came back dry
    stall_at = max(2, int((cfg or {}).get("k_stall", 4)) - 1)
    dry = bool(lh and lh.get("status") and lh.get("status") != "postponed" and not lh.get("added"))
    if lh and lh.get("review"):
        lines.append(f"The lighthouse reviewer (Opus) read your ledger, evidence and steps, and asks you to step back "
                     f"first: {lh['review']}")
    if stall_n >= stall_at or idle_n >= 2 or dry:
        why = (f"{stall_n} stops without concluding anything" if stall_n >= stall_at else
               f"{idle_n} stops without new evidence" if idle_n >= 2 else "the reviewer found nothing new")
        lines.append(STEP_BACK.format(why=why))
    nxt = L.next_node()
    work = L.workable()
    lines.extend(_followup_lines(followups))
    if g.get("leftovers_pending"):
        lines.append(LEFTOVERS_UNRECORDED.format(what=""))
        g["leftovers_pending"] = False          # delivered with this nudge
    if nxt is not None:
        if nxt["kind"] == "problem" and nxt["source"] == "followup" and L.children(nxt["id"]):
            lines.append(f"Every item of follow-up {nxt['id']} is closed: close it with propose_done({nxt['id']}, "
                         "candidate=<one line on what was done>, evidence_ids=[...]); it needs no human check.")
        elif nxt["kind"] == "problem":
            lines.append(f"{nxt['id']} \"{nxt['title']}\" has no open solution or approach. "
                         "Add one with add_node, or mark the problem blocked with a reason.")
        else:
            lines.append(f"Next node: {nxt['id']} [{nxt['status']}] \"{nxt['title']}\".")
            steps = json.loads(nxt["remaining"] or "[]")
            if steps:
                lines.append("Remaining steps you recorded for it: " + "; ".join(steps))
    others = [n for n in work if nxt is None or n["id"] != nxt["id"]][:6]
    if others:
        lines.append("Other workable nodes: " + ", ".join(f"{n['id']} \"{n['title']}\"" for n in others))
    bg = [t for t in ((g.get("_bg") or [])) if isinstance(t, dict)]
    if bg:
        lines.append("Background task(s) running: " + "; ".join(
            str(t.get("description") or t.get("command") or t.get("id"))[:120] for t in bg[:3])
            + ". Work on other nodes meanwhile and check their output when they finish.")
    if waiting:
        lines.append(f"Job {waiting['job']} on {waiting['host']} is registered as running for node "
                     f"{waiting['node']}. Poll it (labhand.jobs.job_poll) and record its output before "
                     "starting something else.")
    for q in answered:
        lines.append(f"The human answered question #{q['id']} ({q['kind']}): {q['answer']}")
    aw = L.awaiting_human()
    if aw.get("proposed"):
        lines.append("Problems you proposed, waiting for the human to accept them: " + ", ".join(
            p["id"] for p in aw["proposed"]) + ". Do not work on them until they are accepted.")
    if aw["questions"]:
        lines.append("Waiting on the human: " + ", ".join(
            f"#{q['id']} ({q['kind']})" for q in aw["questions"]) + ". Continue with other nodes meanwhile.")
    if aw["pending_verification"]:
        lines.append("Candidate awaiting human verification: " + ", ".join(
            p["id"] for p in aw["pending_verification"]) + ".")
    if lh:
        if lh.get("added"):
            lines.append("The lighthouse reviewer queued: " + "; ".join(lh.get("added_desc", lh["added"]))
                         + ". Finish the current node first, then take these in order.")
    for f in flags:
        lines.append(f"Reviewer flag on {f['node']} ({f['kind']}): {f['why']}")
    if picked:
        lines.append(f"Your question left a choice; the reviewer (Opus) made it: {picked['choice']}"
                     + (f" — {picked['why']}" if picked.get("why") else "") + ". Record each option you named as an "
                     "approach (add_node) so none is lost, then start that one; if it fails, close it as a dead end "
                     "with its assumption and take the next.")
    elif choice or (offers_options(text) and ((followups or {}).get("applied") is None)):
        # (When the follow-up reader read the message, it already sorted what was left to the human; measured
        # 2026-11-12: "reopening P3 is your call" became a proposal for the human AND this push to decide it.)
        lines.append("Your last message left a choice to the human. Do not wait: call ask(kind=\"approach_choice\", "
                     "options=[best first], node_id=<the problem or solution>, context=<what bears on the choice>). "
                     "The options become nodes and the decider model ranks them from the ledger and this session "
                     f"(or, if it is unavailable, your order stands: {CHOICE_RULE}). Then start the option it names; "
                     "a failed option becomes a dead end with its assumption, and you take the next.")
    if approved:
        lines.append(f"Approved — the human's standing rule grants requests to run things (locally or on the VMs): "
                     f"go ahead with \"{approved}\". Record what you ran and its output with attach_evidence.")
    if review_note:
        lines.append(f"Reviewer's note: {review_note}")
    if asked:
        lines.append(f"Your question was recorded for the human as #{asked['qid']} ({asked['kind']})"
                     + (f"; {asked['node']} is blocked until it is answered" if asked["node"] else "")
                     + ". Continue with the other workable nodes; next time use ask() directly.")
    elif asks_question(text) and not choice and not picked and not approved:
        lines.append("Only information that the human alone has goes to the human: ask(kind=\"need_info\"). "
                     "Requests to run things are approved by standing rule: ask(kind=\"need_permission\") answers at "
                     "once. Otherwise decide and continue.")
    lines.append("Record outputs with attach_evidence. Close nodes with set_status (dead_end needs the "
                 "assumption it rests on). A problem ends only through propose_done with evidence.")
    return "\n".join(lines)[:3500]


def new_activity(L, g, max_seen=300):
    """Unique tool outputs recorded by the PostToolUse hook since the last stop.
    Activity only keeps the idle counter at zero; it never counts as progress."""
    path = os.path.join(L.dir, "activity.jsonl")
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    off = int(g.get("activity_offset", 0))
    if off > size:          # file was truncated or replaced: start over
        off = 0
    with open(path, "rb") as fh:
        fh.seek(off)
        data = fh.read()
    end = data.rfind(b"\n") + 1     # only complete lines; a half-written one waits for next time
    seen = list(g.get("activity_seen", []) or [])
    known, fresh = set(seen), 0
    for line in data[:end].splitlines():
        try:
            h = json.loads(line.decode("utf-8")).get("h")
        except (ValueError, UnicodeDecodeError):
            continue
        if h and h not in known:
            known.add(h)
            seen.append(h)
            fresh += 1
    g["activity_offset"] = off + end
    g["activity_seen"] = seen[-max_seen:]
    return fresh


def activity_line(tool, response):
    """What the PostToolUse hook appends: a hash of the tool output, not the output."""
    blob = json.dumps(response, sort_keys=True, ensure_ascii=False, default=str)
    return {"tool": tool, "h": hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:32]}


def _followups(L, extractor, text, cfg, g):
    """Read the work the last message lists as not done and put it in the ledger.
    -> {"applied": {...} | None, "unavailable": bool, "error": str}"""
    from . import followups
    try:
        params = inspect.signature(extractor).parameters
    except (TypeError, ValueError):
        params = {}
    if "context" in params:
        res = extractor(text, cfg, context=lambda: followups.context_for(L), calls_used=int(g.get("followup_calls", 0)))
    else:
        res = extractor(text, cfg)
    if res.get("attempted", res.get("source") == "model"):
        g["followup_calls"] = int(g.get("followup_calls", 0)) + 1
    if res.get("source") != "model":
        return {"applied": None, "unavailable": True, "error": str(res.get("error") or "")}
    applied = followups.apply(L, res.get("items") or [], cfg)
    L.record("followups", {"items": len(res.get("items") or []), "work": applied["work"],
                           "human": [list(x) for x in applied["human"]], "optional": applied["optional"],
                           "problems": applied["problems"], "skipped": [list(x) for x in applied["skipped"]][:12],
                           "model": cfg.get("followup_model")})
    return {"applied": applied, "unavailable": False, "error": ""}


LIGHTHOUSE_MIN_TIME = 90     # seconds a lighthouse review needs out of a stop's budget

LEFTOVERS_UNRECORDED = (
    "A message of yours listed work that is not done{what}. A list of what is left is not a way to end a turn: do "
    "each item now, or record it so it is not lost: add_node under the open problem it belongs to, propose_problem "
    "for work beyond the checklist, ask(kind=\"need_info\") for what only the human has, or set_status dead_end "
    "(with the assumption) for what cannot be done. Then continue.")


def _followup_lines(fu):
    a = (fu or {}).get("applied") or {}
    lines = []
    if a.get("work"):
        lines.append("Your last message listed work that is not done; it is now in the ledger: "
                     + ", ".join(a["work"]) + ". Do each one (or close it as a dead end with its assumption): a "
                     "list of what is left is not a way to end a turn.")
    if a.get("human"):
        lines.append("Items that need the human were asked: " + ", ".join(f"#{q} ({n})" for n, q in a["human"]) + ".")
    if a.get("optional"):
        lines.append("Items recorded for the human to accept first: " + ", ".join(a["optional"])
                     + ". Do not work on them until they are accepted.")
    if a.get("supervisor"):
        lines.append(f"Your {len(a['supervisor'])} remark(s) about beacon itself went into the human's report; "
                     "nothing for you to do about them.")
    return lines


def on_stop(L, inp, lighthouse=None, now=None, classifier=None, rotate=False, followup=None, started=None):
    ok, why = L.verify_integrity()
    if not ok:
        # Write nothing: a new event on top of a tampered state would re-seal it.
        return Decision("allow", "integrity", report=f"LEDGER INTEGRITY CHECK FAILED: {why}. "
                        "Nothing was written. Inspect the ledger before resuming.")
    cfg, g = L.config(), L.gate_state()
    # One clock for every model call this stop makes: Claude Code kills the hook at its timeout, and a killed
    # hook saves nothing. (Local to this decision; never written back.)
    # `started`: when the hook began (time spent waiting for the Stop lock counts against the same 300 s).
    cfg["_deadline"] = (started or time.monotonic()) + float(cfg.get("stop_budget_seconds", 270))
    text = inp.get("last_assistant_message") or ""
    session = inp.get("session_id") or ""
    g["stops"] += 1
    if session and session not in g["sessions"]:
        g["sessions"] = (g["sessions"] + [session])[-50:]

    prev_seq = int(g["last_evidence_seq"])
    prog, ev = L.counter("progress"), L.counter("unique_evidence")
    new_ev = L.evidence(since_seq=prev_seq, unique_only=True)
    activity = new_activity(L, g)
    waiting = L.waiting(now)
    near_sub = {nid for n in L.near_done_nodes() for nid in L.subtree_ids(n["id"])}
    near_active = any(e["node_id"] in near_sub for e in new_ev)
    # Background work the worker is legitimately waiting for: a job registered with mark_waiting, or
    # Claude Code's own background tasks (the Stop input lists them). Waiting is neither stalling nor idling.
    bg = [t for t in (inp.get("background_tasks") or []) if isinstance(t, dict)]
    if prog > g["last_progress"]:
        # A conclusion also ends any earlier dry streak: the next stall gets a full set of rounds.
        g["stall"], g["near_grace"], g["lighthouse_dry"] = 0, 0, 0
    elif waiting or bg:
        pass
    elif near_active and int(g.get("near_grace", 0)) < int(cfg["near_done_grace"]):
        # Real near-finish work is not escalated -- but only for a bounded number of stops,
        # so a standing "almost there" cannot replace concluding anything.
        g["stall"], g["near_grace"] = 0, int(g.get("near_grace", 0)) + 1
    else:
        g["stall"] += 1
    g["idle"] = 0 if (new_ev or waiting or activity or bg) else g["idle"] + 1
    g["last_progress"], g["last_evidence"] = prog, ev
    g["last_evidence_seq"] = L.last_seq()

    answered = [q for q in L.questions("answered") if not q["delivered"]]
    last_flag = int(g.get("last_flag_seq", 0))
    flags = [f for f in L.flags() if int(f.get("seq", 0)) > last_flag]
    if flags:
        g["last_flag_seq"] = max(int(f["seq"]) for f in flags)

    classified = None

    def finish(action, why, reason="", report="", lh=None):
        g["last_decision"] = f"{action}:{why}"
        if action == "block":
            g["forced"] += 1
            if session:
                sf = dict(g.get("session_forced", {}) or {})
                sf[session] = int(sf.get(session, 0)) + 1
                g["session_forced"] = dict(list(sf.items())[-50:])
        g.pop("_bg", None)
        # What the gate itself recorded during this stop (follow-up nodes, questions it asked) is not the
        # worker's: the next stop must not take it for new evidence or a question asked through ask().
        g["last_evidence_seq"] = L.last_seq()
        L.save_gate(g, {"action": action, "why": why, "stop": g["stops"], "forced": g["forced"],
                        "stall": g["stall"], "idle": g["idle"], "session": session,
                        "clear_waiting": waiting is None and L._meta_get("waiting") is not None})
        if action == "block" and answered:
            L.mark_delivered([q["id"] for q in answered])
        return Decision(action, why, reason=reason, report=report, lighthouse=lh or {}, classified=classified or {})

    if inp.get("permission_mode") == "plan":
        return finish("allow", "plan_mode", report="Claude Code is in plan mode: the human is reviewing a plan.")
    if L.human_flags().get("paused"):
        g["stall"], g["idle"] = 0, 0        # a pause must not leave an escalation waiting for the resume
        return finish("allow", "paused", report="Supervision is paused by the human (`beacon: resume` ends it).")
    if possible_refusal(text):
        return finish("allow", "possible_refusal", report=(
            "The worker's last message looks like a refusal. The gate does not push through refusals: "
            "decide whether the task is legitimate and how to scope it, then resume.\n> " + text[:600]))
    # Asked through the ask tool during this turn (before anything the gate itself records below).
    recent_qs = [q for q in L.questions() if q["seq"] > prev_seq]
    recent_ask = bool(recent_qs)
    # Work the worker listed as not done becomes nodes (read once per distinct message).
    fu = None
    if followup is not None and cfg.get("followup_policy", "work") != "off":
        from .followups import lists_leftovers, message_hash
        h = message_hash(text)
        seen = list(g.get("followup_seen", []) or [])
        if lists_leftovers(text) and h not in seen:
            g["followup_seen"] = (seen + [h])[-30:]
            try:
                fu = _followups(L, followup, text, cfg, g)
            except Exception as e:  # noqa: BLE001  -- reading the list must never break the stop decision
                fu = {"applied": None, "unavailable": True, "error": f"follow-up reader failed: {e}"[:300]}
            a = fu.get("applied") or {}
            # Recorded either way, so `beacon log` shows whether the list was read (measured 2026-11-12: an
            # Opus call that failed on TLS left no trace at all).
            classified = dict(classified or {}, followups=({k: a.get(k) for k in ("work", "human", "optional", "supervisor")}
                                                           if a else {"error": fu.get("error") or "not read"}))
            if fu["unavailable"]:
                # No model read the list. Remember it: if this stop is let through (waiting on the human), the
                # next nudge still tells the worker to record or do those items.
                g["leftovers_pending"] = True
    lh = None
    asked_for = L.human_flags().get("lighthouse_request") if lighthouse is not None else None
    if asked_for is not None:
        # The human asked for a review (`beacon: lighthouse`): it runs now, outside the budget -- before the
        # finished-run and awaiting-human checks (measured 2026-11-13: asked while the run waited on the human,
        # and again after every problem was closed, the review never ran). What it queues reopens the work.
        L.take_human_flag("lighthouse_request")
        note = str((asked_for or {}).get("note") or "").strip()
        res = lighthouse(L, task="human", focus=[note] if note else [], cfg=cfg) or {}
        g["lighthouse_calls"] += 1
        lh = dict(res, requested_by="human")
        if res.get("added"):
            g["lighthouse_dry"], g["stall"], g["idle"] = 0, 0, 0
        newer = [f for f in L.flags() if int(f.get("seq", 0)) > int(g.get("last_flag_seq", 0))]
        if newer:
            flags = flags + newer
            g["last_flag_seq"] = max(int(f["seq"]) for f in newer)
    if not L.open_problems():
        if fu and fu["unavailable"] and g["forced"] < int(cfg["hard_forced"]) and int(g.get("leftover_nudges", 0)) < 3:
            # No model to read the list: the worker records (or does) the items itself. A few times per run
            # at most -- a worker that keeps rewording its list is not held here until the hard budget.
            g["leftover_nudges"] = int(g.get("leftover_nudges", 0)) + 1
            g["leftovers_pending"] = False
            return finish("block", "leftovers_unrecorded", reason=LEFTOVERS_UNRECORDED.format(what=""))
        aw = L.awaiting_human()
        if aw["questions"] or aw["pending_verification"] or aw.get("proposed"):
            # Nothing is left for the worker, but the run is not finished: it waits on the human (measured
            # 2026-11-13: P10 proposed and waiting for `beacon: accept`, logged as "no_open_problems").
            return finish("allow", "awaiting_human", lh=lh, report="Nothing is left for the worker; what remains "
                          "waits on the human (see Waiting on you).")
        return finish("allow", "no_open_problems", lh=lh)
    if bg and not rotate:
        # Interactive: Claude Code wakes the session when its background task ends; pushing now would
        # only make the worker poll. (Under the watchdog the session is headless: keep it going.)
        names = ", ".join(str(t.get("description") or t.get("command") or t.get("id") or "?")[:80] for t in bg[:3])
        return finish("allow", "background_wait", report=f"Waiting for Claude Code background task(s): {names}.")
    hard = int(cfg["hard_forced"])
    if g["forced"] >= hard:
        return finish("allow", "hard_budget", report=f"Hard budget of {hard} forced continuations reached.")
    soft = int(g["soft_cap"] or cfg["soft_forced"])
    if g["forced"] >= soft:
        if not g["extension_used"] and near_active:
            g["extension_used"] = True
            g["soft_cap"] = min(hard, soft + max(1, math.ceil(soft * float(cfg["near_done_extension"]))))
        else:
            return finish("allow", "soft_budget", report=(
                f"Soft budget of {soft} forced continuations reached"
                + (" (the one-time near-done extension was already used)." if g["extension_used"] else ".")))
    # A question written as prose (not through ask): classify it; info/permission becomes a
    # real question for the human, everything else is nudged on.
    asked, choice, picked, approved = None, False, None, None
    fu_asked = ((fu or {}).get("applied") or {}).get("human") or []
    # Measured 2026-11-12: a prose question ("ascending or descending?") was never read because the worker had
    # asked two other questions through ask() in the same turn. Now it is read; the classifier sees those questions.
    if asks_question(text) and classifier is not None:
        c = _classify(L, classifier, text, cfg, asked=recent_qs)
        if recent_ask and c["source"] == "heuristic" and c["kind"] == "need_info":
            c = dict(c, kind="rhetorical")   # keywords cannot tell a new question from one just asked
        kind, question, source = c["kind"], c["question"], c["source"]
        classified = dict(classified or {}, kind=kind, source=source, note=c["note"])
        if source.startswith("model"):
            L.record("classified", {"kind": kind, "question": question[:300], "note": c["note"][:300],
                                    "choice": c["choice"][:300], "real_sample": c["real_sample"],
                                    "model": cfg.get("classifier_model")})
        choice = kind == "approach_choice"
        if choice and c["choice"]:
            picked = {"choice": c["choice"], "why": c["why"]}
            classified["choice"] = c["choice"]
        needs_human = kind == "need_info" or (kind == "need_permission" and (
            cfg.get("permission_policy", "approve") != "approve"
            or (c["real_sample"] and cfg.get("ask_before_running_real_samples", False))))
        if kind == "need_permission" and not needs_human:
            nxt = L.next_node()
            L.approve_request(question, node_id=nxt["id"] if nxt is not None else None,
                              basis=f"classified by {source}", actor="gate")
            approved = question
            classified["approved"] = True
        elif needs_human and kind == "need_info" and fu_asked:
            # The follow-up reader already put what only the human has to them.
            asked = {"qid": fu_asked[0][1], "kind": kind, "node": fu_asked[0][0]}
        elif needs_human:
            nxt = L.next_node()
            target = nxt["id"] if nxt is not None else None
            r = L.ask(kind, f"[classified by {source}] {question}", node_id=target, actor="gate")
            asked = {"qid": r["qid"], "kind": kind, "node": target}
    if not L.workable():
        return finish("allow", "awaiting_human", lh=lh, report=(
            f"The worker's question was recorded as #{asked['qid']} ({asked['kind']})." if asked else ""))
    if rotate and int(cfg["rotate_every"]) > 0 and session and \
            int((g.get("session_forced", {}) or {}).get(session, 0)) >= int(cfg["rotate_every"]):
        return finish("allow", "rotate", lh=lh, report=(
            f"Session {session} reached {cfg['rotate_every']} forced continuations; the watchdog starts a "
            "fresh session from the ledger (long contexts degrade)."))

    audit = _near_done_audit(L, g, cfg)
    if lh is not None:
        pass                                    # the human's review ran above
    elif (g["stall"] >= int(cfg["k_stall"]) or g["idle"] >= int(cfg["k_idle"]) or audit) and \
            lighthouse is not None and g["lighthouse_calls"] < int(cfg["max_lighthouse_calls"]) and \
            cfg["_deadline"] - time.monotonic() < LIGHTHOUSE_MIN_TIME:
        # The other calls of this stop used the time a review needs: it runs at the next stop (the counters
        # that asked for it are kept), instead of a call that is cut off and counted as a dry round.
        lh = {"status": "postponed", "detail": "not enough time left in this stop; the review runs at the next one"}
    elif g["stall"] >= int(cfg["k_stall"]) or g["idle"] >= int(cfg["k_idle"]) or audit:
        if lighthouse is None or g["lighthouse_calls"] >= int(cfg["max_lighthouse_calls"]):
            return finish("allow", "lighthouse_exhausted", report=(
                "The worker is stuck and the lighthouse budget is spent (or no lighthouse is configured)."))
        task = "audit_near_done" if audit else ("idle" if g["idle"] >= int(cfg["k_idle"]) else "stall")
        res = lighthouse(L, task=task, focus=audit, cfg=cfg) or {}
        g["lighthouse_calls"] += 1
        if audit:
            g["audited"] = dict(g.get("audited", {}) or {})
            for nid in audit:
                g["audited"][nid] = L.node(nid)["near_done_claims"]
        lh = dict(res)
        if res.get("added"):
            g["lighthouse_dry"], g["stall"], g["idle"] = 0, 0, 0
        else:
            g["lighthouse_dry"] += 1
            if g["lighthouse_dry"] >= int(cfg["lighthouse_dry_max"]):
                return finish("allow", "lighthouse_dry", lh=lh, report=(
                    f"The lighthouse reviewer added nothing new in {g['lighthouse_dry']} consecutive rounds "
                    f"(last: {res.get('status') or '?'}: {(res.get('detail') or '')[:300]})."))
        # Flags the lighthouse just raised belong in this nudge.
        newer = [f for f in L.flags() if int(f.get("seq", 0)) > int(g.get("last_flag_seq", 0))]
        if newer:
            flags = flags + newer
            g["last_flag_seq"] = max(int(f["seq"]) for f in newer)

    g["_bg"] = bg[:3]
    reason = build_nudge(L, g, text, lh=lh, waiting=waiting, answered=answered, flags=flags, asked=asked,
                         choice=choice, review_note=(classified or {}).get("note", ""), picked=picked,
                         approved=approved, cfg=cfg, followups=fu)
    return finish("block", "open_work", reason=reason, lh=lh)
