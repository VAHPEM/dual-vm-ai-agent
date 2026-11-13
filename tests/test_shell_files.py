# -*- coding: utf-8 -*-
"""dualvm.shell: the samples/ change report and upload_file. No VM is contacted: the wrapper runs in a local bash
(Linux, GNU find) and remote.upload is monkeypatched."""
import shutil
import subprocess

import pytest

from dualvm import shell

_GNU = shutil.which("bash") and shutil.which("timeout") and subprocess.run(
    ["find", ".", "-maxdepth", "0", "-printf", ""], capture_output=True).returncode == 0


def _run_wrapped(tmp_path, command):
    (tmp_path / "samples").mkdir(exist_ok=True)
    p = subprocess.run(["bash", "-c", shell._wrap_cmd(command, "kali", 20)], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    text, rc = shell._peel_rc(p.stdout)
    return shell._peel_newfiles(text)[1], rc


@pytest.mark.skipif(not _GNU, reason="needs bash, timeout and GNU find (the Kali side of the wrapper)")
def test_a_command_that_leaves_samples_does_not_report_its_new_dir(tmp_path):
    """Measured 2026-11-13: every run_cmd that cd'd into a fresh clone reported README.md, CLAUDE.md, ... as
    "FILES WRITTEN OR CHANGED in samples/" -- the after-snapshot ran in the command's new working dir."""
    away = tmp_path / "away"
    away.mkdir()
    (away / "README.md").write_text("from a clone\n")
    new, rc = _run_wrapped(tmp_path, f"cd {away} && ls >/dev/null")
    assert rc == 0 and new == []
    new, rc = _run_wrapped(tmp_path, f"cd {away} && echo hi > \"$OLDPWD/out.txt\"")
    assert rc == 0 and new == [("out.txt", 3)]
    new, rc = _run_wrapped(tmp_path, "echo hello > made.txt; exit 3")
    assert rc == 3                                   # the exit code still comes through


def test_upload_file_puts_a_local_file_into_samples(monkeypatch, tmp_path):
    src = tmp_path / "patch.diff"
    src.write_bytes(b"x" * 7000)
    seen = []
    monkeypatch.setattr(shell.remote, "upload", lambda local, name, host="kali": seen.append((local, name, host))
                        or f"samples/{name}")
    r = shell.upload_file(str(src), host="kali")
    assert r.ok and seen == [(str(src), "patch.diff", "kali")] and "7000 bytes" in r.summary
    assert "samples/patch.diff" in r.summary and r.known == ["kali:samples/patch.diff = patch.diff (7000 bytes)"]
    r = shell.upload_file(str(src), name="data/./sub/p.diff", host="windows")
    assert r.ok and seen[-1] == (str(src), "data/sub/p.diff", "windows")


@pytest.mark.parametrize("name", ["../x", "a/../../x", "/etc/passwd", "C:/Windows/x", "\\\\server\\x", "."])
def test_upload_file_refuses_names_outside_samples(monkeypatch, tmp_path, name):
    src = tmp_path / "f.bin"
    src.write_bytes(b"1")
    monkeypatch.setattr(shell.remote, "upload", lambda *a, **k: pytest.fail("must not upload"))
    r = shell.upload_file(str(src), name=name)
    assert not r.ok and "relative path inside samples/" in r.summary


def test_upload_file_reports_missing_files_bad_hosts_and_failures(monkeypatch, tmp_path):
    assert "no such local file" in shell.upload_file(str(tmp_path / "nope")).summary
    src = tmp_path / "f.bin"
    src.write_bytes(b"1")
    assert "host must be" in shell.upload_file(str(src), host="mac").summary

    def boom(*a, **k):
        raise OSError("Failure")
    monkeypatch.setattr(shell.remote, "upload", boom)
    r = shell.upload_file(str(src))
    assert not r.ok and "upload to kali failed: Failure" in r.summary
