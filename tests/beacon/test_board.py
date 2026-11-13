# -*- coding: utf-8 -*-
"""The visual graph page and the gate's handling of the human's pause / lighthouse request."""
import json
import os
import re
import subprocess
import sys

from beacon import board
from beacon.gate import on_stop

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _data(html):
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data block missing"
    return json.loads(m.group(1))


def _sample(L):
    L.add_node("P1", "the key is derived from the build timestamp")          # P1.S1
    L.add_node("P1.S1", "emulate the key schedule", kind="approach")         # P1.S1.A1
    L.add_node("P1", "strings survey", kind="approach")                       # P1.A1
    L.start("P1.A1")
    e, _ = L.attach_evidence("P1.A1", "floss found nothing useful", "no strings")
    L.set_status("P1.A1", "dead_end", reason="no plaintext strings at all",
                 assumption="the flag is stored as a plain string", evidence_ids=[e])
    L.start("P1.S1.A1")
    L.ask("need_info", "Which sample build should I use?", node_id="P1.S1.A1")


def test_board_contains_the_ledger(make_run):
    L = make_run()
    _sample(L)
    d = _data(board.render_board(L))
    ids = {n["id"]: n for n in d["nodes"]}
    assert set(ids) >= {"P1", "P2", "P1.S1", "P1.S1.A1", "P1.A1"}
    assert ids["P1.A1"]["status"] == "dead_end" and ids["P1.A1"]["assumption"].startswith("the flag is stored")
    assert ids["P1.A1"]["ev"] == 1 and ids["P1.A1"]["evidence"][0]["summary"] == "floss found nothing useful"
    assert ids["P1.S1.A1"]["status"] == "blocked" and ids["P1.S1.A1"]["blocked_by"] == 1
    assert d["questions"][0]["question"] == "Which sample build should I use?"
    assert d["integrity"]["ok"] and d["paused"] is False


def test_board_cannot_be_broken_out_of(make_run):
    L = make_run()
    L.add_node("P1", "</script><script>alert(1)</script> <!-- x")
    html = board.render_board(L)
    block = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert "<" not in block                       # every < in the data is <
    assert _data(html)["nodes"][-1]["title"].startswith("</script><script>")
    assert "innerHTML" not in html                # rendered with textContent only
    assert "http://" not in html and "https://" not in html   # no external resources


def test_write_board_and_refresh_only_once_opened(make_run):
    L = make_run()
    board.refresh_if_open(L)
    assert not os.path.exists(board.board_path(L))
    path = board.write_board(L)
    L.add_node("P1", "a new hypothesis here")
    board.refresh_if_open(L)
    with open(path, encoding="utf-8") as fh:
        assert any(n["id"] == "P1.S1" for n in _data(fh.read())["nodes"])
    assert not [f for f in os.listdir(L.dir) if f.endswith(".tmp")]


def test_hooks_refresh_the_open_board(make_run):
    L = make_run(k_stall=99, k_idle=99)
    path = board.write_board(L)
    L.add_node("P1", "added after the page was written")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BEACON_", "ANTHROPIC_"))}
    env.update(PYTHONPATH=ROOT, BEACON_RUN=L.dir)
    subprocess.run([sys.executable, "-m", "beacon.hooks", "post-tool-use"], env=env, timeout=60,
                   input=json.dumps({"tool_name": "mcp__beacon__add_node", "tool_response": "ok"}), text=True)
    with open(path, encoding="utf-8") as fh:
        assert any(n["title"] == "added after the page was written" for n in _data(fh.read())["nodes"])


def test_cli_graph(make_run, capsys):
    from beacon import cli
    L = make_run()
    assert cli.main(["graph", "--no-open", "--run", L.dir]) == 0
    assert os.path.exists(os.path.join(L.dir, "graph.html")) and "graph: file://" in capsys.readouterr().out
    assert cli.main(["graph", "--format", "mermaid", "--run", L.dir]) == 0
    assert capsys.readouterr().out.startswith("flowchart TD")


# ------------------------------------------------------------------ gate: the human's requests
def test_pause_lets_every_stop_through(make_run):
    L = make_run(k_stall=1, k_idle=1)
    L.set_human_flag("paused", True)
    for _ in range(3):
        d = on_stop(L, {"session_id": "s", "last_assistant_message": "stopping"}, lighthouse=lambda *a, **k: {})
        assert (d.action, d.why) == ("allow", "paused")
    assert L.gate_state()["stall"] == 0 and L.gate_state()["forced"] == 0
    L.set_human_flag("paused", None)
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "stopping"}, lighthouse=lambda *a, **k: {})
    assert d.action == "block"


def test_lighthouse_request_runs_once_outside_the_budget(make_run):
    L = make_run(k_stall=99, k_idle=99, max_lighthouse_calls=0)
    calls = []

    def lh(L_, task, focus, cfg):
        calls.append((task, list(focus)))
        return {"status": "ok", "added": ["P1.S1"], "added_desc": ["P1.S1 something"]}

    L.set_human_flag("lighthouse_request", {"note": "check the packer assumption"})
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "working"}, lighthouse=lh)
    assert d.action == "block" and calls == [("human", ["check the packer assumption"])]
    assert d.lighthouse["requested_by"] == "human"
    assert "lighthouse_request" not in L.human_flags()
    on_stop(L, {"session_id": "s", "last_assistant_message": "working"}, lighthouse=lh)
    assert len(calls) == 1                              # one request, one review


def test_a_review_the_human_asks_for_runs_even_while_the_run_waits_on_them(make_run):
    """Measured 2026-11-13: `beacon: lighthouse xem lại P5` while every node waited on the human; each stop was let
    through as awaiting_human before the review step, so the review never ran."""
    L = make_run(problems=[{"title": "Find the second stage"}], k_stall=99, k_idle=99)
    a = L.add_node("P1", "ask the human first", kind="approach")
    L.start(a)
    L.ask("need_info", "Which file is the second stage?", node_id=a)
    calls = []

    def dry(L_, task, focus, cfg):
        calls.append(task)
        return {"status": "ok", "added": []}

    L.set_human_flag("lighthouse_request", {"note": "look at P1 again"})
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "waiting"}, lighthouse=dry)
    assert calls == ["human"] and d.action == "allow" and d.why == "awaiting_human"
    assert d.lighthouse["requested_by"] == "human" and "lighthouse_request" not in L.human_flags()

    def adds(L_, task, focus, cfg):          # a review that queues work makes the run workable again
        nid = L_.add_node("P1", "read the loader's import table", kind="approach", actor="lighthouse")
        return {"status": "ok", "added": [nid], "added_desc": [nid]}

    L.set_human_flag("lighthouse_request", {"note": ""})
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "waiting"}, lighthouse=adds)
    assert d.action == "block" and "read the loader's import table" in d.reason


def test_a_review_the_human_asks_for_after_the_run_finished_still_runs(make_run):
    """Measured 2026-11-13: every problem was closed; `beacon: lighthouse xem lại P5` was queued three times and
    each stop was let through as no_open_problems before the review step. Now it runs; checks on a done problem
    become a follow-up problem, so the run goes on with them."""
    import json as _json

    from beacon.lighthouse import run_lighthouse
    L = make_run(problems=[{"title": "Fix the loader"}], k_stall=99, k_idle=99)
    a = L.add_node("P1", "patch it", kind="approach")
    L.start(a)
    eid, _ = L.attach_evidence(a, "patched", content="diff")
    L.set_status(a, "done", evidence_ids=[eid])
    L.propose_done("P1", "patched", [eid])
    L.verify("P1", True, "ok", actor="human")
    sug = {"parent": "P1", "title": "Diff the --json output on the real binaries before and after the patch",
           "kind": "new_approach", "targets": "the patch changes analysis on every binary",
           "first_command": "git stash && python3 x.py --json build/a > /tmp/before.json",
           "expected_observation": "identical recall on every build/ binary", "rationale": "unit tests only so far"}
    tr = lambda body, key, timeout: {"stop_reason": "end_turn", "usage": {},  # noqa: E731
                                     "content": [{"type": "text", "text": _json.dumps(
                                         {"suggestions": [sug], "review": "Only unit tests ran after the patch."})}]}
    lh = lambda L_, task, focus, cfg: run_lighthouse(L_, task=task, focus=focus, cfg=cfg, transport=tr, key="k")  # noqa: E731
    L.set_human_flag("lighthouse_request", {"note": "look at P1 again"})
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "All done."}, lighthouse=lh)
    assert d.action == "block" and d.lighthouse["requested_by"] == "human"
    assert "Only unit tests ran after the patch." in d.reason and "Diff the --json output" in d.reason
    fu = [p for p in L.problems() if p["source"] == "followup"]
    assert len(fu) == 1 and fu[0]["status"] == "todo"


def test_human_flags_are_the_humans(make_run):
    import pytest
    from beacon.ledger import LedgerError
    L = make_run()
    with pytest.raises(LedgerError):
        L.set_human_flag("paused", True, actor="worker")
    with pytest.raises(LedgerError):
        L.set_human_flag("something_else", True)
    assert L.take_human_flag("lighthouse_request") is None
