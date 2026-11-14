import json
import os

import pytest

from beacon.project import MARK_BEGIN, MARK_END, merge_claude_md, merge_settings, setup_project
from beacon.report import calibrate_refusals, stop_log


def test_merge_settings_keeps_existing_and_is_idempotent():
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
                "permissions": {"allow": ["Bash(ls *)"]}, "model": "opus"}
    new, ch = merge_settings(existing, "/usr/bin/python3", repo="/r")
    assert new["model"] == "opus" and "Bash(ls *)" in new["permissions"]["allow"]
    assert "mcp__beacon" in new["permissions"]["allow"]
    stop_cmds = [h["command"] for g in new["hooks"]["Stop"] for h in g["hooks"]]
    assert "echo mine" in stop_cmds and any("beacon.hooks stop" in c for c in stop_cmds)
    assert set(new["hooks"]) >= {"Stop", "SessionStart", "PreToolUse", "PostToolUse", "StopFailure"}
    again, ch2 = merge_settings(new, "/usr/bin/python3", repo="/r")
    assert ch2 == [] and again == new
    moved, ch3 = merge_settings(new, "/opt/py/bin/python3", repo="/r")   # interpreter moved: update, don't duplicate
    stop_cmds = [h["command"] for g in moved["hooks"]["Stop"] for h in g["hooks"]]
    assert len([c for c in stop_cmds if "beacon.hooks stop" in c]) == 1 and "/opt/py" in stop_cmds[-1]
    assert "updated Stop hook" in ch3


def test_merge_claude_md_replaces_its_own_block():
    text, ch = merge_claude_md("# Project\n\nmy rules\n", "RULES v1")
    assert text.startswith("# Project\n\nmy rules\n") and "RULES v1" in text and ch
    text2, ch2 = merge_claude_md(text, "RULES v2")
    assert "RULES v1" not in text2 and "RULES v2" in text2 and text2.count(MARK_BEGIN) == 1
    assert merge_claude_md(text2, "RULES v2")[1] == []
    assert text2.index(MARK_END) > text2.index("RULES v2")


def test_setup_project_end_to_end(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"deny": ["Read(.env)"]}}))
    (proj / "CLAUDE.md").write_text("# My CTF project\n")
    changes = setup_project(str(proj), "/usr/bin/python3", "/venv/bin/python")
    assert changes
    s = json.loads((proj / ".claude" / "settings.local.json").read_text())
    assert s["permissions"]["deny"][0] == "Read(.env)" and "mcp__beacon" in s["permissions"]["allow"]
    assert "Edit(**/ledger.db*)" in s["permissions"]["deny"] and "Bash(sqlite3 *)" in s["permissions"]["deny"]
    assert "mcp__dualvm" not in s["permissions"]["allow"]
    assert (proj / ".claude" / "settings.local.json.beacon-bak").exists()
    m = json.loads((proj / ".mcp.json").read_text())
    assert m["mcpServers"]["beacon"]["command"] == "/venv/bin/python"
    assert m["mcpServers"]["beacon"]["env"]["BEACON_RUN"] == "${BEACON_RUN}"
    md = (proj / "CLAUDE.md").read_text()
    assert md.startswith("# My CTF project") and "Beacon worker rules" in md
    assert setup_project(str(proj), "/usr/bin/python3", "/venv/bin/python") == []
    assert not list(proj.glob("**/*.beacon-tmp"))


def test_setup_project_refuses_malformed_json(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".mcp.json").write_text("{not json")
    with pytest.raises(ValueError):
        setup_project(str(proj), "/usr/bin/python3", "/usr/bin/python3")
    assert (proj / ".mcp.json").read_text() == "{not json"


def test_dry_run_writes_nothing(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    assert setup_project(str(proj), "/usr/bin/python3", "/usr/bin/python3", dry_run=True)
    assert os.listdir(proj) == []


def test_stop_log_and_calibration(make_run):
    L = make_run()
    with open(os.path.join(L.dir, "outputs.jsonl"), "w", encoding="utf-8") as fh:
        for ts, text in ((1, "Working on P1, pausing."), (3, "I can't help with that."),
                         (5, "The tool cannot parse ARM64 Mach-O, switching to radare2. " * 30)):
            fh.write(json.dumps({"ts": ts, "session": "abcdef123", "text": text}) + "\n")
    with open(os.path.join(L.dir, "decisions.jsonl"), "w", encoding="utf-8") as fh:
        for ts, why, act in ((2, "open_work", "block"), (4, "possible_refusal", "allow")):
            fh.write(json.dumps({"ts": ts, "session": "abcdef123", "action": act, "why": why}) + "\n")
    log = stop_log(L.dir, last=10)
    assert "block:open_work" in log and "allow:possible_refusal" in log and "no decision logged" in log
    cal = calibrate_refusals(L.dir)
    assert "1 flagged" in cal and "1 near misses" in cal


def test_stop_log_missing_decision_does_not_shift(make_run):
    L = make_run()
    with open(os.path.join(L.dir, "outputs.jsonl"), "w", encoding="utf-8") as fh:
        for ts in (1, 3, 5):
            fh.write(json.dumps({"ts": ts, "session": "s", "text": f"msg at {ts}"}) + "\n")
    with open(os.path.join(L.dir, "decisions.jsonl"), "w", encoding="utf-8") as fh:
        for ts, why in ((2, "first"), (6, "third")):     # the stop at ts=3 never got its decision logged
            fh.write(json.dumps({"ts": ts, "session": "s", "action": "block", "why": why}) + "\n")
    log = stop_log(L.dir)
    blocks = log.split("\n\n")
    assert "block:first" in blocks[0] and "no decision logged" in blocks[1] and "block:third" in blocks[2]


def test_no_double_hooks_across_settings_files(tmp_path):
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    shared, _ = merge_settings({}, "/usr/bin/python3")
    (proj / ".claude" / "settings.json").write_text(json.dumps(shared))   # pasted earlier by hand
    changes = setup_project(str(proj), "/usr/bin/python3", "/usr/bin/python3")
    assert any("left Stop alone" in c for c in changes)
    local = json.loads((proj / ".claude" / "settings.local.json").read_text())
    assert not local.get("hooks", {}).get("Stop")


def test_malformed_shapes_are_errors():
    with pytest.raises(ValueError):
        merge_settings({"hooks": {"Stop": {"not": "a list"}}}, "/usr/bin/python3")
    with pytest.raises(ValueError):
        merge_settings({"permissions": {"allow": "mcp__x"}}, "/usr/bin/python3")


def test_hook_commands_survive_git_bash_on_windows(monkeypatch):
    """Claude Code runs hook commands through Git Bash on Windows, which eats backslashes: the installed
    command must spell Windows paths with forward slashes."""
    from beacon import shellrun
    from beacon.project import hook_command
    assert shellrun.posix_path(r"C:\x\y") == (r"C:\x\y" if os.name != "nt" else "C:/x/y")
    monkeypatch.setattr(shellrun.os, "name", "nt")
    cmd = hook_command(r"C:\Users\ve\AppData\Local\Programs\Python\Python314\python.exe", "stop",
                       repo=r"C:\Users\ve\dual-vm-ai-agent")
    assert "\\" not in cmd and '"C:/Users/ve/dual-vm-ai-agent"' in cmd and cmd.endswith("-m beacon.hooks stop")


def test_shellrun_runs_posix_shell_and_decodes_utf8(tmp_path):
    from beacon import shellrun
    if os.name == "nt" and not shellrun.git_bash():
        pytest.skip("no Git Bash on this Windows machine")
    r = shellrun.run('printf "%s" "$X"', env=dict(os.environ, X="mũi tên ▸"), cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0 and r.stdout == "mũi tên ▸"


@pytest.mark.parametrize("kw", [{"allow_labhand": True}, {"allow_dualvm": True}])
def test_allow_labhand_preapproves_both_server_names(tmp_path, kw):
    proj = tmp_path / "proj"
    proj.mkdir()
    setup_project(str(proj), "/usr/bin/python3", "/venv/bin/python", **kw)
    allow = json.loads((proj / ".claude" / "settings.local.json").read_text())["permissions"]["allow"]
    assert {"mcp__beacon", "mcp__labhand", "mcp__dualvm"} <= set(allow)


@pytest.mark.parametrize("flag", ["--allow-labhand", "--allow-dualvm"])
def test_cli_allow_flag_and_its_old_spelling(flag):
    from beacon.cli import build_parser
    args = build_parser().parse_args(["setup-project", "/p", flag])
    assert args.allow_labhand is True
    assert build_parser().parse_args(["setup-project", "/p"]).allow_labhand is False
