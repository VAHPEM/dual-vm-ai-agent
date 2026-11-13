import json
import os
import subprocess
import sys

from beacon.hooks import guard, protected_paths
from beacon.shellrun import posix_path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_hook(event, payload, run=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BEACON_", "ANTHROPIC_"))}
    env["PYTHONPATH"] = ROOT
    if run:
        env["BEACON_RUN"] = run
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-m", "beacon.hooks", event], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


def test_unsupervised_session_passes_through(make_run):
    rc, out, _ = run_hook("stop", {"session_id": "x", "last_assistant_message": "bye"})
    assert rc == 0 and out == ""


def test_unsupervised_session_is_announced_once_at_the_start(make_run):
    """Measured 2026-11-12: a restart with `claude --continue` dropped BEACON_RUN and nothing said so until the
    beacon tools failed. Only SessionStart speaks (to the human); every other event stays silent."""
    for env in ({}, {"BEACON_RUN": "${BEACON_RUN}"}):
        rc, out, _ = run_hook("session-start", {"session_id": "x", "source": "resume"}, extra_env=env)
        assert rc == 0 and "NOT supervising" in json.loads(out)["systemMessage"]
        assert "BEACON_RUN=<run dir> claude --continue" in json.loads(out)["systemMessage"]
        assert run_hook("stop", {"session_id": "x", "last_assistant_message": "bye"}, extra_env=env)[1] == ""
        assert run_hook("pre-tool-use", {"session_id": "x", "tool_name": "Bash", "tool_input": {"command": "ls"}},
                        extra_env=env)[1] == ""


def test_stop_blocks_and_logs_output(make_run):
    L = make_run(k_stall=99, k_idle=99)
    rc, out, err = run_hook("stop", {"session_id": "s", "last_assistant_message": "pausing here"}, run=L.dir)
    assert rc == 0, err
    obj = json.loads(out)
    assert obj["decision"] == "block" and "still open" in obj["reason"]
    with open(os.path.join(L.dir, "outputs.jsonl"), encoding="utf-8") as fh:
        assert json.loads(fh.readline())["text"] == "pausing here"


def test_stop_allows_and_writes_report(make_run, tmp_path):
    L = make_run([{"title": "Only problem here", "status": "done"}],
                 notify_cmd=f"echo $BEACON_EVENT > {posix_path(tmp_path / 'notified')}")   # sh / Git Bash syntax
    rc, out, _ = run_hook("stop", {"session_id": "s", "last_assistant_message": "all done"}, run=L.dir)
    obj = json.loads(out)
    assert rc == 0 and "decision" not in obj          # no decision = Claude Code may stop
    assert obj["systemMessage"].startswith("beacon ▸ let the session stop: no_open_problems")
    assert os.path.exists(os.path.join(L.dir, "REPORT.md"))
    assert (tmp_path / "notified").read_text().strip() == "stop:no_open_problems"


def test_fail_open_on_broken_ledger(tmp_path):
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "ledger.db").write_bytes(b"not a database at all")
    rc, out, _ = run_hook("stop", {"session_id": "s"}, run=str(bad))
    assert rc == 0 and out == ""
    assert (bad / "hook-errors.log").exists()


def test_session_start_injects_ledger(make_run):
    L = make_run()
    L.add_node("P1", "the obvious hypothesis")
    rc, out, err = run_hook("session-start", {"session_id": "s", "source": "compact"}, run=L.dir)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "P1.S1" in ctx and "compact" in ctx


def test_pre_tool_use_denies_through_the_hook(make_run):
    L = make_run()
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Bash",
                                           "tool_input": {"command": f"sqlite3 {L.path} 'update nodes'"}},
                          run=L.dir)
    obj = json.loads(out)["hookSpecificOutput"]
    assert obj["permissionDecision"] == "deny"
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, run=L.dir)
    assert out == ""


def test_guard_rules(make_run, tmp_path):
    L = make_run()
    roots = protected_paths(L.dir)
    cfg = {"no_web": True}
    assert guard("Write", {"file_path": os.path.join(L.dir, "ledger.db")}, roots, cfg)
    assert guard("Edit", {"file_path": str(tmp_path / "proj" / ".claude" / "settings.json")}, roots, cfg)
    assert guard("Write", {"file_path": str(tmp_path / ".mcp.json")}, roots, cfg)
    assert guard("Write", {"file_path": str(tmp_path / "notes.md")}, roots, cfg) is None
    assert guard("Read", {"file_path": os.path.join(L.dir, "ledger.db")}, roots, cfg) is None
    assert guard("Bash", {"command": "echo $BEACON_RUN"}, roots, cfg)
    assert guard("Bash", {"command": "python3 -c 'import sqlite3; sqlite3.connect(\"ledger.db\")'"}, roots, cfg)
    assert guard("Bash", {"command": "objdump -d sample.bin | head"}, roots, cfg) is None
    assert guard("WebSearch", {"query": "flare-on writeup"}, roots, cfg)
    assert guard("WebSearch", {"query": "x"}, roots, {"no_web": False}) is None


def test_stop_failure_is_logged(make_run):
    L = make_run()
    rc, out, _ = run_hook("stop-failure", {"session_id": "s", "error_type": "rate_limit", "error": "429"}, run=L.dir)
    assert rc == 0 and out == ""
    with open(os.path.join(L.dir, "failures.jsonl"), encoding="utf-8") as fh:
        assert json.loads(fh.readline())["error_type"] == "rate_limit"


def test_guard_survives_a_broken_ledger(tmp_path):
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "ledger.db").write_bytes(b"garbage")
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Write",
                                           "tool_input": {"file_path": str(bad / "ledger.db")}}, run=str(bad))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_post_tool_use_records_activity_hashes(make_run):
    L = make_run()
    run_hook("post-tool-use", {"tool_name": "Bash", "tool_response": {"stdout": "secret-ish output"}}, run=L.dir)
    run_hook("post-tool-use", {"tool_name": "mcp__beacon__ledger_view", "tool_response": "tree"}, run=L.dir)
    with open(os.path.join(L.dir, "activity.jsonl"), encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh]
    assert len(lines) == 1 and lines[0]["tool"] == "Bash"
    assert "secret-ish" not in json.dumps(lines)          # only a hash is stored


def test_guard_blocks_internal_task_tools(make_run):
    L = make_run()
    roots = protected_paths(L.dir)
    assert guard("TaskCreate", {"subject": "x"}, roots, {"block_internal_tasks": True})
    assert guard("TodoWrite", {"todos": []}, roots, {}) is not None
    assert guard("TaskCreate", {"subject": "x"}, roots, {"block_internal_tasks": False}) is None
