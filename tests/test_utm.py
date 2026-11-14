# -*- coding: utf-8 -*-
"""labhand.utm without UTM: a fake utmctl, a fake SSH check and fake job lists."""
import pytest

from labhand import hosts, jobs, remote, utm
from labhand._result import VMResult

ENV = {"KALI_HOST": "k", "KALI_USER": "u", "KALI_UTM_VM": "Kali Linux",
       "WIN_HOST": "w", "WIN_USER": "u"}                      # windows: no UTM VM name


class FakeUTM:
    def __init__(self, help_has_snapshot=False):
        self.state = {"Kali Linux": "started"}
        self.calls = []
        self.help_has_snapshot = help_has_snapshot

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "--help":
            return 0, "SUBCOMMANDS:\n  version\n  list\n  status\n  start\n  stop\n" + (
                "  snapshot                Manage snapshots.\n" if self.help_has_snapshot else "")
        if cmd == "version":
            return 0, "4.7.4"
        if cmd == "status":
            return 0, self.state.get(args[1], "unknown")
        if cmd == "stop":
            self.state[args[1]] = "stopped"
            return 0, ""
        if cmd == "start":
            self.state[args[1]] = "started"
            return 0, ""
        if cmd == "snapshot":
            return 0, f"snapshot {args[1]} ok"
        return 1, "unknown"


@pytest.fixture
def lab(monkeypatch, tmp_path):
    hosts.reload(ENV)
    fake = FakeUTM()
    monkeypatch.setattr(utm, "_utmctl", fake)
    monkeypatch.setattr(utm, "_wait_ssh", lambda host, timeout: (True, ""))
    monkeypatch.setattr(utm.time, "sleep", lambda s: None)
    monkeypatch.setattr(utm, "_REGISTRY", str(tmp_path / "reg.json"))
    monkeypatch.setattr(utm, "_SESSIONS", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(utm, "_guest_shutdown", lambda host: "")
    monkeypatch.setattr(utm, "_sync", lambda host: None)
    monkeypatch.setattr(remote, "_drop", lambda host: None)
    monkeypatch.setattr(utm, "_running_jobs", lambda host: [])
    fake.boot = {"n": 0}

    def start_bumps_boot(args, timeout=60, _orig=fake.__call__):
        if args[0] == "start":
            fake.boot["n"] += 1                  # every start is a new boot of the guest
        return _orig(args, timeout)
    monkeypatch.setattr(utm, "_utmctl", start_bumps_boot)
    monkeypatch.setattr(utm, "_boot_id", lambda host: f"boot-{fake.boot['n']}")
    yield fake
    hosts.reload()


def test_status_and_unknown_or_unnamed_hosts(lab):
    r = utm.vm_status("kali")
    assert r.ok and r.summary == "kali (Kali Linux): started"
    assert utm.vm_status("kaly").summary == "vm_status: unknown host 'kaly'; known hosts: kali, windows"
    r = utm.vm_status("windows")
    assert not r.ok and "set WINDOWS_UTM_VM in .env" in r.summary
    assert lab.calls == [["status", "Kali Linux"]]               # nothing run for the bad ones


def test_revert_shuts_down_cleanly_then_starts_disposable(lab):
    r = utm.vm_revert("kali")
    assert r.ok, r.summary
    assert ["stop", "Kali Linux", "--request"] in lab.calls
    assert ["start", "Kali Linux", "--disposable"] in lab.calls
    assert lab.calls.index(["stop", "Kali Linux", "--request"]) < lab.calls.index(
        ["start", "Kali Linux", "--disposable"])


def test_start_normal_and_already_started(lab):
    assert not utm.vm_start("kali").ok                          # already started
    lab.state["Kali Linux"] = "stopped"
    r = utm.vm_start("kali")
    assert r.ok and "normal session" in r.summary
    assert ["start", "Kali Linux"] in lab.calls


def test_stop_refuses_while_a_job_runs(lab, monkeypatch):
    monkeypatch.setattr(utm, "_running_jobs", lambda host: ["rdlab-kali-1"])
    r = utm.vm_stop("kali")
    assert not r.ok and "rdlab-kali-1" in r.summary
    assert not any(c[0] == "stop" for c in lab.calls)
    assert not utm.vm_revert("kali").ok
    assert not any(c[0] in ("stop", "start") for c in lab.calls)


def test_stop_rejects_bad_mode_and_reports_already_stopped(lab):
    assert not utm.vm_stop("kali", how="yank").ok
    lab.state["Kali Linux"] = "stopped"
    assert utm.vm_stop("kali").ok and not any(c[0] == "stop" for c in lab.calls)


def test_wait_ssh_failure_is_reported(lab, monkeypatch):
    monkeypatch.setattr(utm, "_wait_ssh", lambda host, timeout: (False, "refused"))
    lab.state["Kali Linux"] = "stopped"
    r = utm.vm_start("kali", disposable=True, timeout=5)
    assert not r.ok and "SSH to kali did not answer in 5s (refused)" in r.summary


def test_snapshots_refused_with_the_measured_reason_on_old_utm(lab):
    r = utm.vm_snapshot("create", "kali", name="clean")
    assert not r.ok
    assert "needs UTM >= 5.0.6" in r.summary and "macOS 26" in r.summary and "UTM 4.7.4" in r.summary
    assert not any(c[0] == "snapshot" for c in lab.calls)


def test_snapshots_once_utmctl_has_them_and_only_own_ones_are_restored(lab):
    lab.help_has_snapshot = True
    assert utm.vm_snapshot("list", "kali").ok
    r = utm.vm_snapshot("restore", "kali", name="theirs")
    assert not r.ok and "not created by labhand" in r.summary
    assert utm.vm_snapshot("create", "kali", name="clean").ok
    assert utm.vm_snapshot("restore", "kali", name="clean").ok
    assert utm.vm_snapshot("delete", "kali", name="clean").ok
    assert not utm.vm_snapshot("delete", "kali", name="clean").ok   # gone from the registry
    assert ["snapshot", "create", "Kali Linux", "clean"] in lab.calls


def test_running_jobs_fails_closed(monkeypatch):
    monkeypatch.setattr(utm, "_job_logs", lambda host: ["_job_done.log", "_job_run.log",
                                                       "_job_err.log", "_job_odd.log",
                                                       "_job_gone.log", "junk.txt"]
                        + [f"_job_old{i}.log" for i in range(200)])        # past any 1500-char cut

    def fake_poll(name=None, tail=40, grep=None, stop=False, host=None):
        if name == "run" or name == "old199":
            return VMResult(True, f"job '{name}': RUNNING, log 3 bytes")
        if name == "err":
            return VMResult(False, "job_poll failed: SSHException: boom")
        if name == "odd":
            return VMResult(True, f"job '{name}': ?")
        if name == "gone":
            return VMResult(False, f"job_poll: no job named '{name}' on kali - ...")
        return VMResult(True, f"job '{name}': FINISHED, exit code 0")
    monkeypatch.setattr(jobs, "job_poll", fake_poll)
    blocking = utm._running_jobs("kali")
    assert [b.split()[0] for b in blocking] == ["err", "gone", "odd", "old199", "run"]
    why = dict(b.split(" ", 1) for b in blocking)
    assert why["run"] == "(RUNNING)" and why["err"].startswith("(poll failed")
    assert why["odd"].startswith("(state unknown") and why["gone"].startswith("(poll failed")


def test_a_poll_hiccup_refuses_stop_revert_and_restore(monkeypatch, lab):
    monkeypatch.setattr(utm, "_running_jobs",
                        lambda host: ["j1 (poll failed: job_poll failed: SSHException: boom)"])
    r = utm.vm_stop("kali")
    assert not r.ok and "j1 (poll failed" in r.summary
    assert not utm.vm_revert("kali").ok
    lab.help_has_snapshot = True
    utm._remember("Kali Linux", "clean", True)
    r = utm.vm_snapshot("restore", "kali", name="clean")
    assert not r.ok and "j1 (poll failed" in r.summary
    assert not any(c[0] in ("stop", "start") or c[:2] == ["snapshot", "restore"] for c in lab.calls)


def test_a_failed_listing_refuses_the_stop(monkeypatch, lab):
    def boom(host):
        raise RuntimeError("listing jobs on kali failed: Permission denied")
    monkeypatch.setattr(utm, "_running_jobs", boom)
    r = utm.vm_stop("kali")
    assert not r.ok and "Permission denied" in r.summary and not any(c[0] == "stop" for c in lab.calls)


def test_an_incomplete_job_listing_blocks_the_stop(monkeypatch, lab):
    monkeypatch.undo()                                      # the real _running_jobs/_job_logs
    hosts.reload(ENV)
    monkeypatch.setattr(utm, "_utmctl", FakeUTM())
    monkeypatch.setattr(remote, "ssh_exec", lambda cmd, host=None, read_timeout=None: ("_job_a.lo", ""))
    with pytest.raises(RuntimeError, match="did not complete"):
        utm._job_logs("kali")
    r = utm.vm_stop("kali")
    assert not r.ok and "not stopping kali" in r.summary


def test_utmctl_missing(monkeypatch):
    monkeypatch.setattr(utm, "utmctl_path", lambda: None)
    rc, out = utm._utmctl(["list"])
    assert rc is None and "utmctl not found" in out


def test_revert_powers_off_only_a_session_labhand_started_disposable(lab):
    lab.state["Kali Linux"] = "stopped"
    assert utm.vm_revert("kali").ok                         # starts disposable, recorded
    assert utm.vm_revert("kali").ok                         # now a disposable one is running
    stops = [c for c in lab.calls if c[0] == "stop"]
    assert stops == [["stop", "Kali Linux", "--force"]]
    assert utm.vm_stop("kali").ok and utm.vm_start("kali").ok   # a normal session again
    assert utm.vm_revert("kali").ok
    assert [c for c in lab.calls if c[0] == "stop"][-1] == ["stop", "Kali Linux", "--request"]


def test_clean_shutdown_that_the_guest_ignores_fails_without_forcing(lab, monkeypatch):
    def ignore_stop(args, timeout=60):
        if args[0] == "stop":
            lab.calls.append(list(args))
            return 0, ""                                     # utmctl ok, guest stays up
        return FakeUTM.__call__(lab, args, timeout)
    monkeypatch.setattr(utm, "_utmctl", ignore_stop)
    monkeypatch.setattr(utm, "_wait_status", lambda vm, want, timeout: (False, "started"))
    r = utm.vm_stop("kali", timeout=5)
    assert not r.ok and "how='force'" in r.summary
    assert not any(c[:3] == ["stop", "Kali Linux", "--force"] for c in lab.calls)
    assert not utm.vm_revert("kali", timeout=5).ok
    assert not any(c[0] == "start" for c in lab.calls)


def test_session_kind_is_stale_after_a_restart_outside_labhand(lab):
    lab.state["Kali Linux"] = "stopped"
    assert utm.vm_revert("kali").ok                         # disposable, recorded with boot-1
    assert utm._session_kind("Kali Linux", "kali") == "disposable"
    lab.boot["n"] += 1                                      # someone rebooted it from the UTM window
    assert utm._session_kind("Kali Linux", "kali") is None
    assert utm.vm_revert("kali").ok
    stops = [c for c in lab.calls if c[0] == "stop"]
    assert stops == [["stop", "Kali Linux", "--request"]]   # clean, not forced


def test_a_stop_forgets_the_session(lab):
    lab.state["Kali Linux"] = "stopped"
    assert utm.vm_start("kali", disposable=True).ok
    assert utm.vm_stop("kali", how="force").ok
    assert utm._sessions() == {}
