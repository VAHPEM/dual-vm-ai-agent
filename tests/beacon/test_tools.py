import importlib.util
import os

import pytest

from beacon import tools


def test_tools_return_text_and_refusals(make_run):
    L = make_run()
    run = L.dir
    assert tools.add_node("P1", "ab", run=run).startswith("REFUSED:")
    assert tools.add_node("P1", "a reasonable hypothesis", run=run) == "added P1.S1"
    assert "in_progress" in tools.start_node("P1.S1", run=run)
    out = tools.set_status("P1.S1", "done", run=run)
    assert out.startswith("REFUSED:") and "evidence" in out
    assert "evidence #1 recorded" == tools.attach_evidence("P1.S1", "decoded output", content="abc", run=run)
    assert "does not count" in tools.attach_evidence("P1.S1", "decoded again", content="abc", run=run)
    out = tools.ask("approach_choice", "which first?", node_id="P1.S1",
                    options=["read the check statically", "trace it with Frida"], run=run)
    assert "Not sent to the human" in out
    assert tools.propose_done("P1", "flag{x}", [1], run=run).startswith("P1 is pending human verification")
    assert "P1" in tools.ledger_view(run=run)


def test_missing_run_is_a_refusal(monkeypatch):
    monkeypatch.delenv("BEACON_RUN", raising=False)
    assert tools.ledger_view().startswith("REFUSED:")


def test_unexpanded_run_variable_says_what_to_do(monkeypatch, tmp_path):
    """.mcp.json passes "${BEACON_RUN}"; without the variable the server got the text itself and answered
    "no ledger at <cwd>/${BEACON_RUN}/ledger.db" (measured 2026-11-12). Now it names the fix and keeps the
    worker away from the config files."""
    monkeypatch.chdir(tmp_path)
    for value in ("${BEACON_RUN}", ""):
        monkeypatch.setenv("BEACON_RUN", value)
        out = tools.ledger_view()
        assert out.startswith("REFUSED:") and "started without BEACON_RUN" in out
        assert "BEACON_RUN=<run dir> claude --continue" in out and "Do not edit .mcp.json" in out
    assert not os.listdir(tmp_path)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp not installed")
def test_mcp_server_registers_all_tools():
    import asyncio

    from beacon import mcp_server
    listed = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in listed}
    assert {f.__name__ for f in tools.TOOLS} <= names
    for t in listed:  # the worker must not see an internal "L" parameter
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        props = schema.get("properties", {})
        assert props and "L" not in props


def test_approach_choice_reply_names_the_first_and_the_rule(make_run):
    run = make_run().dir
    out = tools.ask("approach_choice", "emulate or hook the routine?", node_id="P1",
                    options=["emulate the decrypt routine", "hook it with Frida"], run=run)
    assert "Start P1.A1 now" in out and "effectiveness first, then cost" in out and "none of them is lost" in out


def test_permission_requests_are_approved_except_real_samples(make_run):
    L = make_run(ask_before_running_real_samples=True)        # the check is off by default since 2026-11-13
    run = L.dir
    out = tools.ask("need_permission", "May I run chal5.exe under x64dbg on the Windows VM?", node_id="P1", run=run)
    assert out.startswith("Approved") and not L.questions("open") and L.questions()[0]["status"] == "auto_approved"
    out = tools.ask("need_permission", "May I execute the real malware sample on the Windows VM?", node_id="P1", run=run)
    assert "recorded for the human" in out and L.questions("open")[0]["kind"] == "need_permission"
    L2 = make_run(permission_policy="ask")
    out = tools.ask("need_permission", "May I install capa on Kali?", run=L2.dir)
    assert "recorded for the human" in out


def _sample_transport(verdict, calls):
    import json as _json

    def t(body, key, timeout):
        calls.append(body)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": _json.dumps(
            {"real_sample": verdict, "why": "the triage problem's file came from a phishing mail"})}]}
    return t


def test_a_run_request_that_names_no_real_sample_is_checked_by_the_model(make_run, monkeypatch):
    L = make_run(problems=[{"title": "Triage the attachment from the phishing mail"}], ask_before_running_real_samples=True)
    calls = []
    monkeypatch.setattr(tools, "SAMPLE_TRANSPORT", _sample_transport(True, calls))
    out = tools.ask("need_permission", "May I run invoice.exe on the Windows VM to see what it does?",
                    node_id="P1", run=L.dir)
    q = L.questions("open")[0]
    assert "recorded for the human" in out and "real malware sample, checked by model" in q["question"]
    body = calls[0]
    assert body["output_config"]["effort"] == "low" and "phishing mail" in body["messages"][0]["content"]
    assert [e["type"] for e in L.events(etype="sample_check")] == ["sample_check"]


def test_a_benign_run_is_approved_after_the_check_and_obvious_cases_need_no_call(make_run, monkeypatch):
    L = make_run(ask_before_running_real_samples=True)
    calls = []
    monkeypatch.setattr(tools, "SAMPLE_TRANSPORT", _sample_transport(False, calls))
    out = tools.ask("need_permission", "May I run chal5.exe under x64dbg on the Windows VM?", node_id="P1", run=L.dir)
    assert out.startswith("Approved") and len(calls) == 1
    assert "real-sample check (model): no" in L.questions()[0]["answer"]
    tools.ask("need_permission", "May I install capa on Kali?", run=L.dir)                     # no file run: no call
    tools.ask("need_permission", "May I execute the real malware sample on Windows?", run=L.dir)  # keywords: no call
    assert len(calls) == 1 and L.questions("open")


def test_the_worker_sees_the_checklist_note_but_not_the_verify_command(make_run):
    """Measured on the first live run: the note said "candidate = pass", ledger_view did not show it, the worker
    proposed a sentence and chased a wrong cause. The note is shown; the verify command (it may hold the answer)
    is not; a silent refusal points back at the note."""
    L = make_run([{"title": "Run the gate and get it to pass", "rationale": "candidate = pass once the gate exits 0",
                   "verify_cmd": 'test "$BEACON_CANDIDATE" = "pass"'}, {"title": "Write the notes file"}])
    view = tools.ledger_view(run=L.dir)
    assert "note: candidate = pass once the gate exits 0" in view and "$BEACON_CANDIDATE\" = " not in view
    assert "checked by: a verify command" in view and "checked by: the human" in view
    a = L.add_node("P1", "run the gate", kind="approach")
    L.start(a)
    e, _ = L.attach_evidence(a, "gate passed", content="45 passed")
    out = tools.propose_done("P1", "the gate passes (45 passed)", [e], run=L.dir)
    assert "REJECTED" in out and "re-read the problem's note" in out
    assert "VERIFIED" in tools.propose_done("P1", "pass", [e], run=L.dir)



def test_by_default_running_a_real_sample_is_approved_like_anything_else(make_run, monkeypatch):
    """The human's decision (2026-11-13): "nếu chạy file binary, cứ cho chạy". No model check, no question."""
    L = make_run()
    monkeypatch.setattr(tools, "SAMPLE_TRANSPORT", lambda *a, **k: pytest.fail("no sample check by default"))
    out = tools.ask("need_permission", "May I execute the real malware sample sample.exe on the Windows VM?",
                    node_id="P1", run=L.dir)
    assert out.startswith("Approved") and not L.questions("open")
    from beacon import decider
    assert "ask_before_running_real_samples" not in decider.system_prompt(L.config())
