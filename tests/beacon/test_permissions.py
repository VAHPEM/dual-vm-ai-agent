import io
import contextlib

import pytest

from beacon.cli import launch_argv, main
from beacon.doctor import doctor
from beacon.ledger import Ledger, LedgerError


def test_permission_mode_is_validated(make_run, tmp_path):
    L = make_run(permission_mode="bypassPermissions")
    assert L.config()["permission_mode"] == "bypassPermissions"
    with pytest.raises(LedgerError):
        L.set_config("permission_mode", "yolo")
    with pytest.raises(LedgerError):
        Ledger.create(str(tmp_path / "bad"), "t", [{"title": "abc def"}], config={"permission_mode": "everything"})
    assert not (tmp_path / "bad" / "ledger.db").exists()


def test_launch_argv():
    argv, env = launch_argv("/runs/r1", "bypassPermissions", "claude", ["--model", "opus"])
    assert argv == ["claude", "--permission-mode", "bypassPermissions", "--model", "opus"]
    assert env["BEACON_RUN"] == "/runs/r1" and "BEACON_WATCHDOG" not in env
    assert launch_argv("/runs/r1", "", "claude")[0] == ["claude"]


def test_watch_uses_the_runs_mode(make_run, tmp_path):
    L = make_run(permission_mode="bypassPermissions")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["watch", "--run", L.dir, "--cwd", str(tmp_path), "--dry-run"])
    assert "--permission-mode bypassPermissions" in out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["watch", "--run", L.dir, "--cwd", str(tmp_path), "--dry-run", "--permission-mode", "auto"])
    assert "--permission-mode auto" in out.getvalue()        # an explicit flag wins


def test_doctor_flags_bypass(make_run):
    L = make_run(permission_mode="bypassPermissions")
    assert "permission_mode is bypassPermissions" in doctor(run=L.dir).text()
