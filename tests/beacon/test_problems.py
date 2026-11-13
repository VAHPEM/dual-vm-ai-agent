# -*- coding: utf-8 -*-
"""Problems the worker finds itself: proposed, accepted or rejected by the human."""
import json

import pytest

from beacon import board, chat, tools
from beacon.gate import on_stop
from beacon.ledger import LedgerError


def test_propose_accept_reject(make_run):
    L = make_run([{"title": "Triage the crash in the XML parser"}], k_stall=99, k_idle=99, problem_approval="human")
    e, _ = L.attach_evidence("P1", "second crash in the YAML loader", content="ASan: heap-use-after-free in yaml")
    r = L.propose_problem("Triage the use-after-free in the YAML loader", "found while fuzzing the XML parser; a "
                          "different component and a different bug class", evidence_ids=[e])
    assert r["id"] == "P2" and r["status"] == "proposed"
    n = L.node("P2")
    assert n["source"] == "worker" and f"#{e}" in n["rationale"]
    assert [p["id"] for p in L.open_problems()] == ["P1"]            # not open: the gate does not push on it
    assert not [w for w in L.workable() if w["id"].startswith("P2")]
    with pytest.raises(LedgerError):
        L.propose_done("P2", "fixed", [e])                           # not before the human accepts it
    with pytest.raises(LedgerError):
        L.propose_problem("Triage the use-after-free in the YAML loader", "again, the same one twice")
    with pytest.raises(LedgerError):
        L.accept_problem("P2", actor="worker")
    L.accept_problem("P2", actor="human")
    assert L.node("P2")["status"] == "todo" and [p["id"] for p in L.open_problems()] == ["P1", "P2"]
    L.propose_problem("Look at the unrelated JSON reader too", "it shares the allocator with the YAML loader")
    with pytest.raises(LedgerError):
        L.accept_problem("P3", accept=False, note="", actor="human")  # a rejection needs a reason
    L.accept_problem("P3", accept=False, note="out of scope this week", actor="human")
    assert L.node("P3")["status"] == "abandoned" and L.verify_integrity()[0]


def test_validation(make_run):
    L = make_run()
    for title, why, ev in (("short", "a reason long enough", ()), ("A long enough title", "no", ()),
                           ("A long enough title", "a reason long enough", (99,))):
        with pytest.raises(LedgerError):
            L.propose_problem(title, why, evidence_ids=ev)


def test_auto_approval(make_run):
    L = make_run(problem_approval="auto")
    r = L.propose_problem("Analyse the second payload stage", "stage 1 drops it; separate binary")
    assert r["status"] == "todo" and any(p["id"] == r["id"] for p in L.open_problems())
    with pytest.raises(LedgerError):
        L.set_config("problem_approval", "decider")


def test_tool_chat_gate_and_graph(make_run):
    L = make_run([{"title": "Triage the crash in the XML parser"}], k_stall=99, k_idle=99, problem_approval="human")
    out = tools.propose_problem("Triage the use-after-free in the YAML loader",
                                "found while fuzzing; different component", run=L.dir)
    assert out.startswith("proposed P2") and "beacon: accept P2" in out
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "working on P1"})
    assert d.action == "block" and "waiting for the human to accept them: P2" in d.reason
    html = board.render_board(L)
    assert '"status": "proposed"' in html and "proposed by Claude" in html
    r = chat.handle(L, "beacon: reject P2")
    assert r["block"] and "usage" in r["show"]
    r = chat.handle(L, "beacon: accept P2")
    assert r["block"] is False and "accepted" in r["show"] and "open problem now" in r["note"]
    assert chat.handle(L, "beacon: accept P2")["show"].startswith("beacon: P2 is not a proposed problem")


def test_stop_message_lists_proposed_problems(make_run, tmp_path):
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    L = make_run([{"title": "Only problem here"}], k_stall=99, k_idle=99, problem_approval="human")
    L.propose_problem("A second problem found on the way", "found while working on P1")
    L.set_status("P1", "blocked", reason="waiting for a VM that is down", actor="human")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BEACON_", "ANTHROPIC_"))}
    env.update(PYTHONPATH=root, BEACON_RUN=L.dir)
    p = subprocess.run([sys.executable, "-m", "beacon.hooks", "stop"], env=env, timeout=60, text=True,
                       input=json.dumps({"session_id": "s", "last_assistant_message": "done for now"}),
                       capture_output=True)
    msg = json.loads(p.stdout)["systemMessage"]
    assert "P2 proposed by Claude" in msg and "beacon: accept P2" in msg


def test_by_default_the_worker_adds_problems_without_approval(make_run):
    """problem_approval=auto is the default: a problem the worker adds is open at once and the gate pushes on it;
    an optional follow-up idea is workable too. The human's own decisions still hold."""
    from beacon import followups
    L = make_run([{"title": "Triage the crash in the XML parser"}], k_stall=99, k_idle=99)
    assert L.config()["problem_approval"] == "auto"
    out = tools.propose_problem("Triage the use-after-free in the YAML loader", "a second crash seen while fuzzing",
                                run=L.dir)
    assert "open now" in out and L.node("P2")["status"] == "todo"
    assert "P2" in [n["id"] for n in L.workable()]
    a = followups.apply(L, [{"title": "Add a fuzzing dictionary for YAML", "detail": "", "kind": "optional",
                             "about": "P2", "existing": "", "question": ""}], L.config())
    assert a["work"] and not a["optional"]
    L.set_status("P1", "abandoned", reason="the human dropped the XML crash", actor="human")
    b = followups.apply(L, [{"title": "Minimise the XML crashing input", "detail": "", "kind": "work",
                             "about": "P1", "existing": "", "question": ""}], L.config())
    assert b["optional"] and not b["work"]                 # leftovers of an abandoned problem still wait
