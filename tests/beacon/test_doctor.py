import json
import sys

from beacon.doctor import doctor
from beacon.project import merge_settings, setup_project


def test_doctor_on_a_wired_project(make_run, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BEACON_API_KEY", raising=False)
    L = make_run([{"title": "Problem with a verifier", "verify_cmd": "true"}])
    proj = tmp_path / "proj"
    proj.mkdir()
    setup_project(str(proj), sys.executable, sys.executable)
    rep = doctor(run=L.dir, project=str(proj), claude="/nonexistent/claude")
    text = rep.text()
    assert "[OK]   hook Stop in .claude/settings.local.json" in text
    assert "integrity ok" in text and "mcp__beacon is pre-approved" in text
    assert "[WARN] no BEACON_API_KEY" in text
    assert "[FAIL] cannot run /nonexistent/claude" in text            # a broken CLI path is a failure


def test_doctor_catches_missing_and_double_hooks(tmp_path):
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    s, _ = merge_settings({}, sys.executable)
    (proj / ".claude" / "settings.json").write_text(json.dumps(s))
    (proj / ".claude" / "settings.local.json").write_text(json.dumps(s))     # twice
    rep = doctor(project=str(proj))
    assert rep.failed and "installed 2 times" in rep.text() and "no beacon server in .mcp.json" in rep.text()
    empty = tmp_path / "e"
    empty.mkdir()
    rep = doctor(project=str(empty))
    assert "hook Stop is not installed" in rep.text()
