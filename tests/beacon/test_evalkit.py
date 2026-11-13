import json
import os

import pytest

from beacon.evalkit import load_suite, plan, run_eval, summarize
from beacon.ledger import Ledger
from beacon.watchdog import SessionResult


def suite_file(tmp_path, tasks):
    p = tmp_path / "suite.json"
    p.write_text(json.dumps({"tasks": tasks}))
    return str(p)


def make_tasks(tmp_path):
    files = tmp_path / "chal"
    files.mkdir()
    (files / "sample.bin").write_bytes(b"\x7fELF fake")
    return [{"id": "t1", "title": "Recover flag one", "files": str(files),
             "verify": 'test "$BEACON_CANDIDATE" = "flag{one}"'},
            {"id": "t2", "title": "Recover flag two", "verify": 'test "$BEACON_CANDIDATE" = "flag{two}"'}]


class FakeWorker:
    """Baseline writes ANSWER.txt; beacon runs propose through the ledger. t1 is solved
    everywhere; t2 only when a lighthouse is on (config decides via max_lighthouse_calls)."""

    def __init__(self, cwd, run, beacon_env):
        self.cwd, self.run_dir, self.beacon_env = cwd, run, beacon_env

    def run(self, prompt, resume=None):
        task = "t1" if "flag one" in open(os.path.join(self.cwd, "TASK.md"), encoding="utf-8").read() else "t2"
        if not self.beacon_env:
            with open(os.path.join(self.cwd, "ANSWER.txt"), "w", encoding="utf-8") as fh:
                fh.write("flag{one}" if task == "t1" else "flag{wrong}")
            return SessionResult(session_id="b", cost_usd=1.0)
        L = Ledger.open(self.run_dir)
        a = L.add_node("P1", "invert the transform", kind="approach")
        e, _ = L.attach_evidence(a, "inverted output", content=f"{task} out")
        with_lh = L.config()["max_lighthouse_calls"] > 0
        if task == "t2" and with_lh:
            L.add_node("P1", "lighthouse idea for t2", kind="approach", source="lighthouse", actor="lighthouse")
        L.propose_done("P1", "flag{wrong}", [e])               # one refuted candidate everywhere
        good = task == "t1" or with_lh
        if good:
            L.propose_done("P1", "flag{one}" if task == "t1" else "flag{two}", [e])
        L.save_gate(L.gate_state(), {"action": "allow", "why": "no_open_problems" if good else "lighthouse_exhausted"})
        L.close()
        return SessionResult(session_id="s", cost_usd=2.0)


def test_load_suite_validation(tmp_path):
    with pytest.raises(ValueError):
        load_suite(suite_file(tmp_path, []))
    with pytest.raises(ValueError):
        load_suite(suite_file(tmp_path, [{"id": "../escape", "title": "x", "verify": "true"}]))
    with pytest.raises(ValueError):
        load_suite(suite_file(tmp_path, [{"id": "a", "title": "x", "verify": "true"},
                                         {"id": "a", "title": "y", "verify": "true"}]))
    assert len(load_suite(suite_file(tmp_path, make_tasks(tmp_path)))) == 2


def test_plan_interleaves_and_is_reproducible(tmp_path):
    tasks = make_tasks(tmp_path)
    p1 = plan(tasks, ["baseline", "gate", "gate_lh"], 2, seed=7)
    assert p1 == plan(tasks, ["baseline", "gate", "gate_lh"], 2, seed=7) and len(p1) == 12


def test_run_eval_end_to_end(tmp_path):
    tasks = make_tasks(tmp_path)
    out = str(tmp_path / "out")
    logs = []
    summary = run_eval(tasks, out, ["baseline", "gate", "gate_lh"], 1, FakeWorker, "/usr/bin/python3",
                       "/usr/bin/python3", log=logs.append)
    rows = [json.loads(x) for x in open(os.path.join(out, "results.jsonl"), encoding="utf-8")]
    got = {(r["task"], r["config"]): r for r in rows}
    assert got[("t1", "baseline")]["solved"] and got[("t1", "baseline")]["solved_via"] == "answer_file"
    assert not got[("t2", "baseline")]["solved"]
    assert got[("t1", "gate")]["solved_via"] == "ledger" and not got[("t2", "gate")]["solved"]
    assert got[("t2", "gate_lh")]["solved"] and got[("t2", "gate_lh")]["lighthouse_added"] == 1
    assert all(r["rejected_candidates"] == 1 for r in rows if r["config"] != "baseline")
    assert all(r.get("integrity", True) for r in rows)
    # isolated copies, web denied, beacon wired only where it belongs
    proj = os.path.join(out, "t1", "gate", "rep1", "proj")
    assert os.path.exists(os.path.join(proj, "sample.bin")) and os.path.exists(os.path.join(proj, ".mcp.json"))
    s = json.load(open(os.path.join(proj, ".claude", "settings.local.json"), encoding="utf-8"))
    assert "WebSearch" in s["permissions"]["deny"] and "mcp__beacon" in s["permissions"]["allow"]
    assert not os.path.exists(os.path.join(out, "t1", "baseline", "rep1", "proj", ".mcp.json"))
    assert Ledger.open(os.path.join(out, "t1", "gate", "rep1", "run")).config()["no_web"] is True
    assert "| gate_lh | 2 | 2 | 100% |" in summary and "| baseline | 2 | 1 | 50% |" in summary
    # resuming skips what is recorded
    run_eval(tasks, out, ["baseline", "gate", "gate_lh"], 1, FakeWorker, "/usr/bin/python3",
             "/usr/bin/python3", log=logs.append)
    assert len(open(os.path.join(out, "results.jsonl"), encoding="utf-8").readlines()) == 6


def test_alt_config_needs_a_model(tmp_path):
    with pytest.raises(ValueError):
        run_eval(make_tasks(tmp_path), str(tmp_path / "o"), ["gate_lh_alt"], 1, FakeWorker, "p", "p")


def test_summarize_empty(tmp_path):
    os.makedirs(tmp_path / "o")
    assert "0 runs" in summarize(str(tmp_path / "o"))
