# -*- coding: utf-8 -*-
"""labhand.jobs on Windows, without a VM: what job_start sends, and the launcher's behaviour.

The VM measurements these guard (2026-11-10, Windows 11 ARM64): a Start-Process child of the
SSH session died ~1 s after job_start returned, and the one-line `& echo __JOB_RC=%errorlevel%`
logged `cmd /c exit 3` as 0. See the comment above _WIN_JOB_LAUNCHER in labhand/jobs.py.
"""
import os
import subprocess
import sys

import pytest

from labhand import jobs, remote


@pytest.fixture
def fake_windows(monkeypatch):
    sent = {"files": {}, "cmds": []}

    def write_remote(path, content, host="kali"):
        assert host == "windows"
        sent["files"][path] = content

    def ssh_exec(cmd, host="kali", read_timeout=None):
        sent["cmds"].append(cmd)
        if "_start.ps1" in cmd:
            return "JOB_PID=4242\n", ""
        return "STATE=RUNNING\nSIZE=0\nLINES=0\n--- last 5 lines ---\n", ""

    monkeypatch.setattr(remote, "write_remote", write_remote)
    monkeypatch.setattr(remote, "ssh_exec", ssh_exec)
    monkeypatch.setattr(remote, "win_samples", lambda: r"C:\Users\o'neil\samples")
    monkeypatch.setattr(remote, "WIN_PYTHON_EXE", r"C:\Py\python.exe")
    return sent


def test_windows_job_is_created_through_wmi(fake_windows):
    r = jobs.job_start("j1", command='"C:\\tool.exe" --x "a b"', host="windows")
    assert r.ok and "pid 4242" in r.summary
    ps = fake_windows["files"]["samples/_job_j1_start.ps1"]
    assert "Win32_Process" in ps and "Invoke-CimMethod" in ps
    assert "Start-Process" not in ps            # dies with the SSH channel (measured)
    # the command line reaches the launcher verbatim, through a file: no second quoting layer
    assert fake_windows["files"]["samples/_job_j1_cmd.txt"] == '"C:\\tool.exe" --x "a b"'
    assert fake_windows["files"]["samples/_job_j1_launch.py"] == jobs._WIN_JOB_LAUNCHER
    # the apostrophe in the profile path is doubled inside the PowerShell literals
    assert r"'C:\Users\o''neil\samples'" in ps
    assert r"""'"C:\Py\python.exe" -u "C:\Users\o''neil\samples\_job_j1_launch.py" j1'""" in ps


def test_windows_script_job_runs_the_script_under_its_interpreter(fake_windows):
    r = jobs.job_start("j2", script="print(1)\n", host="windows", python=r"C:\Other\python.exe")
    assert r.ok
    assert fake_windows["files"]["samples/_job_j2.py"] == "print(1)\n"
    assert fake_windows["files"]["samples/_job_j2_cmd.txt"] == r'"C:\Other\python.exe" -u _job_j2.py'


def test_windows_job_create_failure_is_not_a_start(fake_windows, monkeypatch):
    monkeypatch.setattr(remote, "ssh_exec", lambda *a, **k: ("JOB_CREATE_FAILED=9\n", ""))
    r = jobs.job_start("j3", command="x", host="windows")
    assert not r.ok and "did NOT start" in r.summary


def test_windows_stop_kills_the_tree_of_this_jobs_launcher_only(fake_windows):
    jobs.job_poll("j1", stop=True, host="windows")
    ps = fake_windows["files"]["samples/_job_j1_poll.ps1"]
    assert "taskkill.exe /T /F /PID $pid0" in ps and "Stop-Process" not in ps
    # alive = the pid is still THIS job's launcher, not a reused pid
    assert "Win32_Process" in ps and "Contains('_job_j1_launch.py')" in ps
    # a job polled before its launcher has created the log still exists (pid file)
    assert 'Test-Path "_job_j1.pid"' in ps


def _run_launcher(tmp_path, command):
    (tmp_path / "launch.py").write_text(jobs._WIN_JOB_LAUNCHER, encoding="utf-8")
    (tmp_path / "_job_t_cmd.txt").write_text(command, encoding="utf-8")
    subprocess.run([sys.executable, str(tmp_path / "launch.py"), "t"], check=True, timeout=60)
    return (tmp_path / "_job_t.log").read_text(encoding="utf-8")


def test_launcher_logs_the_real_exit_code(tmp_path):
    log = _run_launcher(tmp_path, "echo out && echo err 1>&2 && exit 3")
    lines = log.split()
    assert "out" in lines and "err" in lines       # stdout and stderr both land in the log
    assert log.rstrip().endswith("__JOB_RC=3"), log


def test_launcher_success_and_cwd(tmp_path):
    log = _run_launcher(tmp_path, f'"{sys.executable}" -c "import os; print(os.getcwd())"')
    assert os.path.samefile(log.splitlines()[0].strip(), tmp_path)
    assert log.rstrip().endswith("__JOB_RC=0"), log
