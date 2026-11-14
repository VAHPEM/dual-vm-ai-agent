# -*- coding: utf-8 -*-
"""Cases found in the review: background work, plan mode, dangling questions, Claude Code's newer exit
codes and credential expiry, the guard's blind spots, and the API settings Opus 5.5 needs."""
import json
import os

from beacon import decider, tools
from beacon.classify import classify
from beacon.gate import on_stop
from beacon.hooks import guard, protected_paths
from beacon.watchdog import SessionResult, _epoch, classify_end, watch


def stop(L, text="working", **kw):
    inp = {"session_id": "s1", "last_assistant_message": text}
    inp.update(kw.pop("inp", {}))
    return on_stop(L, inp, **kw)


# ------------------------------------------------------------------ waiting is not stalling
def test_a_registered_job_freezes_the_stall_counter(make_run):
    L = make_run(k_stall=2, k_idle=99)
    a = L.add_node("P1", "fuzz the parser", kind="approach")
    L.start(a)
    L.mark_waiting(a, "fuzz1", host="kali", max_minutes=60)
    calls = []
    lh = lambda *a, **k: calls.append(1) or {"status": "empty", "added": []}  # noqa: E731
    for _ in range(5):
        d = stop(L, lighthouse=lh)
        assert d.action == "block"
    assert L.gate_state()["stall"] == 0 and not calls          # no lighthouse while a job runs


def test_background_tasks_interactive_vs_watchdog(make_run):
    L = make_run(k_stall=2, k_idle=99)
    L.start(L.add_node("P1", "fuzz the parser", kind="approach"))
    bg = [{"id": "t1", "type": "shell", "status": "running", "description": "afl-fuzz on the parser"}]
    d = stop(L, inp={"background_tasks": bg})
    assert (d.action, d.why) == ("allow", "background_wait") and "afl-fuzz" in d.report
    for _ in range(3):                                        # headless (watchdog): keep going, but not "stuck"
        d = stop(L, inp={"background_tasks": bg}, rotate=True)
        assert d.action == "block" and "Background task(s) running: afl-fuzz" in d.reason
    assert L.gate_state()["stall"] == 0
    assert "_bg" not in L.gate_state()


def test_plan_mode_is_left_alone(make_run):
    L = make_run()
    d = stop(L, inp={"permission_mode": "plan"})
    assert (d.action, d.why) == ("allow", "plan_mode")


# ------------------------------------------------------------------ questions that no longer need the human
def test_solving_a_problem_drops_its_open_questions(make_run):
    L = make_run(k_stall=99, k_idle=99)
    a = L.add_node("P1", "decrypt with the key", kind="approach")
    b = L.add_node("P1", "emulate the routine", kind="approach")
    L.start(a)
    L.ask("need_info", "Where is the key file for this one?", node_id=a)
    L.start(b)
    e, _ = L.attach_evidence(b, "emulation printed the flag", content="flag{x}")
    L.propose_done("P1", "flag{x}", [e])
    L.verify("P1", True, actor="human")
    assert not L.questions("open") and L.questions()[0]["status"] == "obsolete"
    assert L.node(a)["status"] == "superseded" and L.verify_integrity()[0]


def test_closing_a_node_drops_its_open_questions(make_run):
    L = make_run(problem_approval="human")
    a = L.add_node("P1", "decrypt with the key", kind="approach")
    L.start(a)
    L.ask("need_info", "Where is the key file for this one?", node_id=a)
    L.set_status(a, "abandoned", reason="the human chose another way", actor="human")
    assert not L.questions("open")
    pid = L.propose_problem("A second problem found on the way", "found while working on P1")["id"]
    L.ask("need_info", "Is the second binary in scope at all?", node_id=pid)
    L.accept_problem(pid, accept=False, note="out of scope this week", actor="human")
    assert not L.questions("open")


# ------------------------------------------------------------------ Claude Code's exits and login
def test_new_exit_codes_are_classified():
    assert classify_end(None, [], 1, "Error: ENOSPC: no space left on device, write") == "disk"
    assert classify_end(None, [], 1, "error: unknown option '--permision-mode'") == "config"
    assert classify_end(None, [], 1, "Claude Code could not start: bubblewrap is required for subprocess env "
                                     "scrubbing and isolation.") == "config"
    assert classify_end(None, [], 1, "Your login has expired. Run `claude auth login`.") == "auth"
    assert classify_end({"is_error": False, "result": "ok"}, [], 0) == ""


def test_credential_expiry_is_parsed_and_warned(make_run):
    assert _epoch(1_800_000_000) == 1_800_000_000.0
    assert _epoch(1_800_000_000_000) == 1_800_000_000.0             # milliseconds
    assert abs(_epoch("2027-01-15T00:00:00Z") - 1_799_971_200.0) < 1
    assert _epoch("soon") == 0.0
    L = make_run()
    logs = []
    now = 1_000_000.0

    class R:
        def run(self, prompt, resume=None):
            L.save_gate(L.gate_state(), {"action": "allow", "why": "no_open_problems"})
            return SessionResult(session_id="s1", credential_expires_at=now + 3600 * 20)

    watch(L.dir, R(), clock=lambda: now, sleep=lambda s: None, log=logs.append, max_sessions=1)
    assert any("login expires in 20 h" in m for m in logs)
    from beacon.ledger import Ledger
    with open(os.path.join(Ledger.open(L.dir).dir, "watch.jsonl")) as fh:
        assert any(json.loads(x).get("kind") == "credential_expiry" for x in fh)


# ------------------------------------------------------------------ guard
def test_guard_covers_the_wrapper_and_other_mcp_servers(make_run, tmp_path):
    L = make_run()
    roots = protected_paths(L.dir)
    assert guard("Write", {"file_path": str(tmp_path / "proj" / ".claude" / "beacon")}, roots, {})
    assert "touches the beacon supervisor" in guard("mcp__fs__write_file",
                                                    {"path": os.path.join(L.dir, "ledger.db"), "content": "x"}, roots, {})
    assert guard("mcp__beacon__ledger_view", {}, roots, {}) is None
    assert guard("mcp__dualvm__run_cmd", {"command": "file /home/kali/samples/a.exe"}, roots, {}) is None
    assert guard("mcp__labhand__run_cmd", {"command": "file /home/kali/samples/a.exe"}, roots, {}) is None


# ------------------------------------------------------------------ what the API calls ask for
def _capture(reply):
    sent = []

    def t(body, key, timeout):
        sent.append(body)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(reply)}]}
    t.sent = sent
    return t


def test_opus_calls_carry_effort_schema_and_room_to_think(make_run):
    t = _capture({"kind": "need_info", "question": "which file?", "confidence": 0.9, "choice": "", "why": "",
                  "real_sample": False, "note": ""})
    classify("Which file holds the config?", {}, transport=t, key="k")
    body = t.sent[0]
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] >= 4000
    assert body["output_config"]["effort"] == "medium" and body["output_config"]["format"]["type"] == "json_schema"
    L = make_run()
    ids = L.ask("approach_choice", "emulate or hook?", node_id="P1",
                options=["emulate the routine", "hook it with Frida"])["created"]
    t = _capture({"ranking": [2, 1], "why": "w", "first_step": "f", "switch_if": "s",
                  "new_option": {"title": "", "why": ""}, "confidence": 0.7})
    d = decider.decide(L, "P1", "emulate or hook?", ids, transport=t, key="k")
    assert d["status"] == "ok" and d.get("new") is None and d["order"] == [ids[1], ids[0]]
    body = t.sent[0]
    assert body["output_config"]["effort"] == "high" and body["max_tokens"] >= 8000
    assert "<reasoning>" not in body["system"]          # no request to write the reasoning out (refusal risk)
    from beacon.lighthouse import run_lighthouse
    t = _capture({"suggestions": [], "suspicious_dead_ends": [], "weak_done": [], "review": "re-read the brief",
                  "notes": ""})
    res = run_lighthouse(L, transport=t, key="k")
    assert res["review"] == "re-read the brief" and t.sent[0]["output_config"]["effort"] == "high"


def test_tools_ask_still_works_without_output_config_support(make_run, monkeypatch):
    L = make_run()
    monkeypatch.setenv("BEACON_API_KEY", "k")

    def old_api(body, key, timeout):
        if "output_config" in body:
            raise RuntimeError("HTTP 400: output_config: unknown field")
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(
            {"ranking": [1, 2], "why": "first is direct", "first_step": "x", "switch_if": "y",
             "new_option": {"title": "", "why": ""}, "confidence": 0.6})}]}
    monkeypatch.setattr(tools, "DECIDER_TRANSPORT", old_api)
    out = tools.ask("approach_choice", "which first?", node_id="P1", options=["emulate it here", "hook it there"],
                    run=L.dir)
    assert out.startswith("The decider (claude-opus-5-5) chose P1.A1")


def test_one_retry_on_rate_limit_or_overload(monkeypatch):
    from beacon import lighthouse
    from beacon.lighthouse import ModelError, call_json
    slept = []
    monkeypatch.setattr(lighthouse, "_sleep", slept.append)
    calls = []

    def flaky(body, key, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 529: overloaded")
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"ok": true}'}]}
    assert call_json("m", "s", "u", transport=flaky)[0] == {"ok": True} and slept == [lighthouse.RETRY_AFTER]

    def down(body, key, timeout):
        raise RuntimeError("HTTP 503: unavailable")
    try:
        call_json("m", "s", "u", transport=down)
        raise AssertionError("expected ModelError")
    except ModelError as e:
        assert "503" in str(e) and len(slept) == 2          # retried once, then gave up
