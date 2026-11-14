# -*- coding: utf-8 -*-
"""LABHAND_WORKDIR: the tools' working directory on every host (default samples/)."""
import pytest

from labhand import hosts, jobs, remote, shell
from labhand._result import VMResult


@pytest.fixture
def wd(monkeypatch):
    monkeypatch.setenv("LABHAND_WORKDIR", "lab/work")
    yield "lab/work"


def test_default_is_samples(monkeypatch):
    monkeypatch.delenv("LABHAND_WORKDIR", raising=False)
    assert hosts.workdir() == "samples" and hosts.workdir_win() == "samples"


@pytest.mark.parametrize("bad", ["../x", "/abs", "a b", "a/../b", "C:x", "a;rm", "$HOME"])
def test_bad_workdir_is_refused_before_any_connection(monkeypatch, bad):
    monkeypatch.setenv("LABHAND_WORKDIR", bad)
    monkeypatch.setattr(remote, "_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    r = shell.run_cmd("id", host="kali")
    assert not r.ok and "LABHAND_WORKDIR" in r.summary


def test_paths_and_commands_follow_the_workdir(wd, monkeypatch):
    assert hosts.workdir_win() == r"lab\work"
    assert remote.sftp_path("x.bin", "kali") == "lab/work/x.bin"
    assert remote.sftp_path("lab/work/x.bin", "kali") == "lab/work/x.bin"
    assert remote.sftp_path("/etc/hosts", "kali") == "/etc/hosts"
    assert "bash -c 'mkdir -p lab/work 2>/dev/null; cd lab/work 2>/dev/null" in shell._wrap_cmd("ls", "kali")
    assert shell._wrap_cmd("dir", "windows").startswith(r"cd /d lab\work 2>nul & ")
    monkeypatch.setattr(remote, "win_home", lambda host=None: r"C:\Users\u")
    assert remote.win_samples("windows") == r"C:\Users\u\lab\work"


def test_jobs_and_spills_write_into_the_workdir(wd, monkeypatch):
    sent = {"files": [], "cmds": []}
    monkeypatch.setattr(remote, "write_remote", lambda p, c, host=None: sent["files"].append(p))
    monkeypatch.setattr(remote, "ssh_exec", lambda cmd, host=None, read_timeout=None:
                        sent["cmds"].append(cmd) or ("JOB_PID=7\n", ""))
    r = jobs.job_start("j", script="print(1)\n", host="kali")
    assert r.ok and "log lab/work/_job_j.log" in r.known[0]
    assert sent["files"] == ["lab/work/_job_j.py"]
    assert "cd lab/work 2>/dev/null || exit 9" in sent["cmds"][0]
    from labhand import output
    sent["files"].clear()
    assert output._save_to_host("kali", "out.txt", "x") == "lab/work/out.txt"


def test_utm_job_listing_uses_the_workdir(wd, monkeypatch):
    from labhand import utm
    seen = []
    monkeypatch.setattr(remote, "ssh_exec", lambda cmd, host=None, read_timeout=None:
                        seen.append(cmd) or ("_job_a.log\n__END__\n", ""))
    assert utm._job_logs("kali") == ["_job_a.log"]
    assert seen[0].startswith("cd lab/work 2>/dev/null")


def test_read_file_sftp_path_is_rooted_at_the_workdir(wd, monkeypatch):
    got = []
    monkeypatch.setattr(remote, "read_bytes", lambda p, host=None: got.append(p) or b"hi")
    assert shell._read_file_sftp("a.txt", 0, 10, "windows").ok
    assert shell._read_file_sftp("lab/work/b.txt", 0, 10, "windows").ok
    assert got == ["lab/work/a.txt", "lab/work/b.txt"]
