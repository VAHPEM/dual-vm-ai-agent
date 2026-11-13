# -*- coding: utf-8 -*-
"""What the human sees: beacon log / transcript, the in-chat messages, the wrapper,
and the human-only CLI commands."""
import io
import json
import os
import stat
import subprocess
import sys

import pytest

from beacon import cli, logview
from beacon.hooks import HUMAN_CLI, guard
from beacon.project import setup_project

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_hook(event, payload, run=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BEACON_", "ANTHROPIC_"))}
    env["PYTHONPATH"] = ROOT
    if run:
        env["BEACON_RUN"] = run
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-m", "beacon.hooks", event], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


# ------------------------------------------------------------------ hooks record what the views read
def test_stop_logs_reply_and_session(make_run, tmp_path):
    L = make_run(k_stall=99, k_idle=99)
    tp = tmp_path / "t.jsonl"
    rc, out, err = run_hook("stop", {"session_id": "s1", "last_assistant_message": "pausing here",
                                     "transcript_path": str(tp)}, run=L.dir)
    obj = json.loads(out)
    assert obj["decision"] == "block"
    assert obj["systemMessage"].startswith("beacon ▸ kept the session going (stop 1, forced 1/")
    with open(os.path.join(L.dir, "decisions.jsonl"), encoding="utf-8") as fh:
        d = json.loads(fh.readline())
    assert d["reason"] == obj["reason"]            # the exact text Claude Code was sent
    with open(os.path.join(L.dir, "sessions.jsonl"), encoding="utf-8") as fh:
        assert json.loads(fh.readline())["transcript_path"] == str(tp)
    # a second stop in the same session does not duplicate the session line
    run_hook("stop", {"session_id": "s1", "last_assistant_message": "again", "transcript_path": str(tp)}, run=L.dir)
    with open(os.path.join(L.dir, "sessions.jsonl"), encoding="utf-8") as fh:
        assert len(fh.readlines()) == 1


def test_session_start_message_points_at_the_log(make_run, tmp_path):
    L = make_run()
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    _, out, _ = run_hook("session-start", {"session_id": "s", "source": "startup", "cwd": str(proj)}, run=L.dir)
    assert f"python3 -m beacon log -f --run {L.dir}" in json.loads(out)["systemMessage"]
    (proj / ".claude" / "beacon").write_text("#!/bin/sh\n")
    _, out, _ = run_hook("session-start", {"session_id": "s", "source": "startup", "cwd": str(proj)}, run=L.dir)
    assert "! .claude/beacon log" in json.loads(out)["systemMessage"]


def test_denial_is_shown_and_logged(make_run):
    L = make_run()
    _, out, _ = run_hook("pre-tool-use", {"session_id": "s", "tool_name": "Bash",
                                          "tool_input": {"command": "python3 -m beacon verify P1"}}, run=L.dir)
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert obj["systemMessage"].startswith("beacon guard ▸ blocked Bash")
    with open(os.path.join(L.dir, "denials.jsonl"), encoding="utf-8") as fh:
        assert "verify P1" in json.loads(fh.readline())["input"]


# ------------------------------------------------------------------ guard: human CLI
@pytest.mark.parametrize("cmd", [
    "python3 -m beacon verify P1",
    "PYTHONPATH=/x python3 -m beacon --run /r answer 2 yes",
    ".claude/beacon config notify_cmd=x",
    "./.claude/beacon reopen P1",
    "beacon verify P1",
    "python3 -m beacon.cli lighthouse --task t",
    "cd /p && .claude/beacon setup-project /p",
])
def test_human_cli_is_refused(cmd):
    assert HUMAN_CLI.search(cmd)
    assert "for the human" in guard("Bash", {"command": cmd}, [], {})


@pytest.mark.parametrize("cmd", [
    "python3 -m beacon log", ".claude/beacon status", ".claude/beacon transcript -n 40",
    "grep -r verify beacon/", "pytest tests/beacon -k answer", ".claude/beacon log -f",
])
def test_read_only_cli_is_allowed(cmd):
    assert guard("Bash", {"command": cmd}, [], {}) is None


# ------------------------------------------------------------------ human-only commands need a terminal
@pytest.mark.parametrize("argv", [["answer", "1", "yes"], ["verify", "P1"], ["reopen", "P1"],
                                  ["config", "notify_cmd=touch /tmp/x"], ["lighthouse"]])
def test_human_commands_refuse_without_a_terminal(make_run, argv, monkeypatch, capsys):
    L = make_run()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))       # not a TTY, like Claude Code's Bash tool
    rc = cli.main(argv + ["--run", L.dir])
    assert rc != 0
    assert "must be run from a terminal" in capsys.readouterr().err
    assert L.config()["notify_cmd"] == ""


def test_read_only_commands_work_without_a_terminal(make_run, monkeypatch, capsys):
    L = make_run()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["config", "--run", L.dir]) in (0, None)   # showing the config is fine
    assert cli.main(["lighthouse", "--dry-run", "--run", L.dir]) in (0, None)
    assert cli.main(["log", "--run", L.dir]) in (0, None)
    assert "no stops recorded yet" in capsys.readouterr().out


# ------------------------------------------------------------------ beacon log
def _write(path, rows):
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_log_pairs_each_stop_with_its_reply(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "outputs.jsonl"), [
        {"ts": 10, "session": "aaaa1111", "text": "I think I am done"},
        {"ts": 20, "session": "aaaa1111", "text": "hook crashed on this one"},
        {"ts": 30, "session": "aaaa1111", "text": "Finished: flag found"}])
    _write(os.path.join(d, "decisions.jsonl"), [
        {"ts": 11, "session": "aaaa1111", "action": "block", "why": "open_work",
         "reason": "P1 is still open. Next: P1.A2 emulate the routine.",
         "lighthouse": {"status": "ok", "added": ["P1.A3"], "added_desc": ["P1.A3 check the key schedule"]}},
        {"ts": 31, "session": "aaaa1111", "action": "allow", "why": "no_open_problems",
         "report": "1/1 problems solved", "report_path": "/r/REPORT.md"}])
    text = logview.stop_log(d, last=10)
    first, second, third = text.split("\n\n")
    assert "I think I am done" in first and "beacon replied (block:open_work)" in first
    assert "P1.A2 emulate the routine" in first and "P1.A3 check the key schedule" in first
    assert "no decision logged" in second          # the missing decision did not shift the next one
    assert "beacon let it stop (allow:no_open_problems)" in third and "1/1 problems solved" in third
    assert logview.stop_log(d, last=1) == third


def test_log_width_and_full(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "outputs.jsonl"), [{"ts": 1, "session": "s", "text": "x" * 1000}])
    assert "x" * 1000 not in logview.stop_log(d, width=50)
    assert "x" * 1000 in logview.stop_log(d, width=None)


def test_follow_log_prints_only_complete_stops(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "outputs.jsonl"), [{"ts": 1, "session": "s", "text": "old stop"}])
    _write(os.path.join(d, "decisions.jsonl"), [{"ts": 2, "session": "s", "action": "block", "why": "w",
                                                  "reason": "old reply"}])
    # a new stop whose decision is still being written when following starts
    _write(os.path.join(d, "outputs.jsonl"), [{"ts": 3, "session": "s", "text": "new stop"}])
    seen, ticks = [], iter(range(4))

    def stop():
        k = next(ticks)
        if k == 1:
            _write(os.path.join(d, "decisions.jsonl"), [{"ts": 4, "session": "s", "action": "block",
                                                          "why": "w", "reason": "new reply"}])
        return k >= 3

    logview.follow_log(d, interval=0, out=seen.append, stop=stop)
    joined = "\n".join(seen)
    assert "new stop" in joined and "new reply" in joined
    assert "old stop" not in joined                # already printed before following began
    assert joined.count("new stop") == 1


# ------------------------------------------------------------------ beacon transcript
TRANSCRIPT = [
    {"type": "queue-operation", "operation": "enqueue", "content": "ignored"},
    {"type": "user", "message": {"role": "user", "content": "Solve P1"}},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "Looking at the ledger."},
        {"type": "tool_use", "id": "t1", "name": "mcp__beacon__ledger_view", "input": {}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                               "content": [{"type": "text", "text": "P1 [todo] Recover"}]}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                                    "input": {"command": "sqlite3 ledger.db"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                                               "content": "Permission denied"}]}},
    {"type": "system", "subtype": "informational", "content": "beacon ▸ kept the session going"},
    {"type": "system", "subtype": "stop_hook_summary", "content": "Stop hook feedback: P1 still open"},
    {"type": "system", "subtype": "compact_boundary"},
    {"type": "attachment", "attachment": {"type": "date", "date": "2026-11-11"}},
    {"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0.0123, "is_error": False},
]


def test_render_transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(str(p), TRANSCRIPT)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"type": "user", "message": {"content": "half a li')      # a line still being written
    text = logview.render_transcript(str(p))
    for want in ("You / harness: Solve P1", "Claude: Looking at the ledger.", "(thinking)",
                 "→ mcp__beacon__ledger_view({})", "← result: P1 [todo] Recover",
                 '→ Bash({"command": "sqlite3 ledger.db"})', "← result (error): Permission denied",
                 "[notice] beacon ▸ kept the session going", "[stop_hook_summary] Stop hook feedback",
                 "[context compacted]", "[end] success, 3 turns, ~$0.0123"):
        assert want in text, want
    assert "ignored" not in text and "2026-11-11" not in text and "half a li" not in text
    assert logview.render_transcript(str(p), last=1) == "[end] success, 3 turns, ~$0.0123"


def test_sessions_prefer_claude_codes_own_file(tmp_path):
    d = tmp_path / "run"
    (d / "sessions").mkdir(parents=True)
    tp = tmp_path / "cc.jsonl"
    _write(str(tp), TRANSCRIPT)
    _write(str(d / "sessions.jsonl"), [{"ts": 5, "session": "abc123", "transcript_path": str(tp)}])
    _write(str(d / "sessions" / "20261111-1-0.jsonl"), [{"type": "system", "subtype": "init", "session_id": "abc123"}])
    _write(str(d / "sessions" / "20261111-1-1.jsonl"), [{"type": "system", "subtype": "init", "session_id": "zzz999"},
                                                         {"type": "assistant", "message": {"content": "first part"}}])
    _write(str(d / "sessions" / "20261111-1-2.jsonl"), [{"type": "system", "subtype": "init", "session_id": "zzz999"},
                                                         {"type": "assistant", "message": {"content": "resumed part"}}])
    got = logview.sessions(str(d))
    assert [(s["session"], s["kind"]) for s in got] == [("abc123", "transcript"), ("zzz999", "stream")] or \
        sorted((s["session"], s["kind"]) for s in got) == [("abc123", "transcript"), ("zzz999", "stream")]
    assert logview.pick_session(str(d), "abc")["path"] == str(tp)
    z = logview.pick_session(str(d), "zzz")
    assert len(z["paths"]) == 2
    text = logview.render_transcript(z["paths"])
    assert text.index("first part") < text.index("resumed part")
    assert logview.pick_session(str(d), "nope") is None


def test_follow_transcript_from_the_end(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(str(p), TRANSCRIPT[:2])
    seen, ticks = [], iter(range(3))

    def stop():
        if next(ticks) == 1:
            _write(str(p), [TRANSCRIPT[2]])
        return False if len(seen) < 3 else True

    logview.follow_transcript(str(p), interval=0, out=seen.append, stop=stop, start_at_end=True)
    assert "Solve P1" not in "\n".join(seen)
    assert "Claude: Looking at the ledger." in seen


def test_transcript_cli(make_run, tmp_path, capsys):
    L = make_run()
    assert cli.main(["transcript", "--run", L.dir]) == 1
    assert "no session recorded yet" in capsys.readouterr().out
    tp = tmp_path / "cc.jsonl"
    _write(str(tp), TRANSCRIPT)
    _write(os.path.join(L.dir, "sessions.jsonl"), [{"ts": 1, "session": "abc123", "transcript_path": str(tp)}])
    assert cli.main(["transcript", "-n", "2", "--run", L.dir]) == 0
    out = capsys.readouterr().out
    assert "== session abc123" in out and "[end] success" in out
    assert cli.main(["transcript", "--list", "--run", L.dir]) == 0
    assert "abc123  transcript" in capsys.readouterr().out


# ------------------------------------------------------------------ the in-chat wrapper
def test_setup_project_writes_the_wrapper(make_run, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    changes = setup_project(str(proj), sys.executable, sys.executable)
    w = proj / ".claude" / "beacon"
    assert any(".claude/beacon" in c for c in changes)
    assert os.name == "nt" or w.stat().st_mode & stat.S_IXUSR      # Windows has no exec bit (Git Bash reads the shebang)
    with open(proj / ".claude" / "settings.local.json", encoding="utf-8") as fh:
        settings = json.load(fh)
    deny = settings["permissions"]["deny"]
    assert "Bash(.claude/beacon verify *)" in deny and "Edit(**/.claude/beacon)" in deny
    assert "respondToBashCommands" not in settings
    # idempotent: a second run changes nothing
    assert not any(".claude/beacon" in c for c in setup_project(str(proj), sys.executable, sys.executable))
    # the wrapper runs this checkout's beacon, from any directory, with the run from the environment
    L = make_run()
    env = {k: v for k, v in os.environ.items() if not k.startswith("BEACON_")}
    env["BEACON_RUN"] = L.dir
    env.pop("PYTHONPATH", None)
    from beacon.shellrun import git_bash
    argv = [str(w), "status"]
    if os.name == "nt":            # a sh script: Claude Code's Bash tool runs it through Git Bash there
        if not git_bash():
            pytest.skip("no Git Bash on this Windows machine")
        argv = [git_bash(), str(w), "status"]
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=env, cwd=os.path.abspath(os.sep),
                       timeout=60)
    assert p.returncode == 0, p.stderr
    assert L.title() in p.stdout


def test_setup_project_quiet_shell(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    setup_project(str(proj), sys.executable, sys.executable, quiet_shell=True)
    with open(proj / ".claude" / "settings.local.json", encoding="utf-8") as fh:
        assert json.load(fh)["respondToBashCommands"] is False


def test_root_is_this_checkout():
    assert os.path.isdir(os.path.join(ROOT, "beacon"))


def test_clipping_keeps_the_start_and_the_end():
    from beacon.logview import _clip, render_entry
    out = "BUILD START\n" + "x" * 5000 + "\nerror: undefined reference to `decrypt_key'"
    c = _clip(out, 1500)
    assert c.startswith("BUILD START") and c.endswith("undefined reference to `decrypt_key'")
    assert "chars cut]" in c and len(c) < 1600
    e = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": out}]}}
    assert "decrypt_key" in "\n".join(render_entry(e, width=1500))


def test_a_long_session_keeps_its_beginning(make_run, tmp_path):
    from beacon.decider import _session_text
    L = make_run()
    tr = tmp_path / "t.jsonl"
    entries = [{"type": "user", "message": {"content": [{"type": "text", "text": "TASK: recover the flag from chal1"}]}}]
    entries += [{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                          "content": f"step {i} " + "y" * 900}]}} for i in range(200)]
    tr.write_text("\n".join(json.dumps(x) for x in entries) + "\n", encoding="utf-8")
    with open(os.path.join(L.dir, "sessions.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1, "session": "s1", "transcript_path": str(tr)}) + "\n")
    s = _session_text(L, "observations", 20000)
    assert len(s) <= 20000 and "TASK: recover the flag" in s and "step 199" in s and "of the session cut here" in s


def test_clipping_edges_never_return_the_whole_text(make_run, tmp_path):
    from beacon.decider import _session_text
    from beacon.logview import _clip
    big = "a" * 1000
    for w in (0, 1, 2, 5, 19, 20, 21):
        assert len(_clip(big, w)) <= w + 40, w
    L = make_run()
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "z" * 5000}]}}) + "\n",
                  encoding="utf-8")
    with open(os.path.join(L.dir, "sessions.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1, "session": "s1", "transcript_path": str(tr)}) + "\n")
    for b in (1, 30, 60, 100):
        assert len(_session_text(L, "full", b)) <= b, b
