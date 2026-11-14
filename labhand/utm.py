# -*- coding: utf-8 -*-
"""labhand.utm — power control and disposable sessions for lab VMs that run in UTM.

Runs `utmctl` LOCALLY on the Mac that hosts UTM (utmctl does not work from an SSH
session). A host's UTM VM name comes from the inventory (<NAME>_UTM_VM, labhand.hosts).

    vm_status(host)                    started | stopped | suspended | ...
    vm_start(host, disposable=False)   start, then wait until SSH answers
    vm_stop(host, how="request")       clean shutdown (default), "force" power-off, "kill"
    vm_revert(host)                    stop + start --disposable + wait for SSH: a fresh
                                       session on the saved disk; every disk write of a
                                       disposable session is discarded when it stops

Measured 2026-11-14 on this Mac (UTM 4.7.4, macOS 15.5): utmctl has no snapshot
subcommand. UTM 5.0.6 added `utmctl snapshot`; QEMU-backend snapshots also need
macOS 26 (Apple-Virtualization ones macOS 27). snapshot_* below detect the subcommand
from `utmctl --help` and return a clear refusal until it exists; afterwards they call
it, and restore/delete only touch snapshots labhand itself created.

Before stopping a VM every function checks the host for labhand background jobs that
are still RUNNING and refuses to stop it if there are any. Nothing here edits a VM's
config.plist or settings.

A clean shutdown needs the guest's cooperation: vm_stop(how="request") asks the guest
over SSH (`shutdown /s /t 0` on Windows; `sudo -n /usr/bin/systemctl poweroff` on linux,
`-n` so a missing sudo rule fails at once) and also sends UTM's ACPI power button. If
the guest stays up, it fails and says so - it never forces. Measured 2026-11-14/15: the
Kali VM ignores the ACPI button (xfce4-power-manager holds the power-key inhibitor) and
shuts down cleanly in ~9 s through its passwordless sudo rule for poweroff (vahpem,
/etc/sudoers.d/labhand-power, see docs/LAB-ISOLATION.md); Windows shuts down on
`shutdown /s`. After a restart
Windows waits at the logon screen unless autologon is set: the win_* GUI tools need
someone logged on at the console (run_cmd / jobs / files work over SSH regardless).
"""
import json
import os
import platform
import shutil
import subprocess
import time

from . import hosts, remote
from .hosts import workdir as _wd, workdir_win as _wdw  # noqa: F401
from ._result import VMResult

UTMCTL_DEFAULT = "/Applications/UTM.app/Contents/MacOS/utmctl"
SNAPSHOT_NEEDS = ("utmctl snapshot needs UTM >= 5.0.6 and, for a QEMU VM, macOS 26 "
                  "(Apple-Virtualization VMs: macOS 27)")
_REGISTRY = os.path.join(os.path.expanduser("~"), ".labhand", "utm-snapshots.json")


def utmctl_path():
    """$LABHAND_UTMCTL, else utmctl inside UTM.app, else utmctl on PATH, else None."""
    for p in (os.environ.get("LABHAND_UTMCTL"), UTMCTL_DEFAULT, shutil.which("utmctl")):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _utmctl(args, timeout=60):
    """(rc, combined output) of `utmctl <args>`; rc None = could not run / timed out."""
    exe = utmctl_path()
    if exe is None:
        return None, ("utmctl not found (UTM.app not installed, or set LABHAND_UTMCTL); "
                      f"labhand runs it locally on the UTM host, this is {platform.system()}")
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"utmctl {' '.join(args)} did not finish in {timeout}s"
    except OSError as e:
        return None, f"utmctl could not run: {e}"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _vm(host, tool, vm=None):
    """(host name, UTM VM name, None) or (None, None, VMResult error)."""
    h, err = hosts.check(host, tool)
    if err:
        return None, None, err
    name = vm or h.utm_vm
    if not name:
        return None, None, VMResult(False, f"{tool}: host {h.name!r} has no UTM VM name; set "
                                           f"{hosts.prefix(h.name)}_UTM_VM in .env (the name "
                                           "`utmctl list` shows)")
    return h.name, name, None


def _status(vm):
    rc, out = _utmctl(["status", vm], timeout=30)
    if rc != 0:
        return None, out
    return (out.strip().splitlines() or [""])[-1].strip().lower(), out


def vm_status(host=None, vm=None):
    """The UTM state of a host's VM (utmctl status)."""
    name, vmn, err = _vm(host, "vm_status", vm)
    if err:
        return err
    st, out = _status(vmn)
    if st is None:
        return VMResult(False, f"vm_status: utmctl status {vmn!r} failed: {out[:300]}")
    return VMResult(True, f"{name} ({vmn}): {st}", detail=out, known=[f"UTM VM {vmn!r} is {st}"])


def _job_logs(host):
    """Every labhand job log name on `host` (job_poll()'s own list is cut at 1500 chars).
    Raises RuntimeError unless the listing provably ran to its end."""
    if remote.is_windows(host):
        o, e = remote.ssh_exec(f'dir /b "{remote.win_samples(host)}\\_job_*.log" & echo __END__',
                               host=host, read_timeout=30)
        err = (e or "").strip()
        if err and err.lower() not in ("file not found", "the system cannot find the file specified."):
            raise RuntimeError(f"listing jobs on {host} failed: {err[:200]}")
    else:
        o, e = remote.ssh_exec(f"cd {_wd()} 2>/dev/null || {{ echo __END__; exit 0; }}; "
                               "ls -1 _job_*.log 2>/dev/null; echo __END__",
                               host=host, read_timeout=30)
    lines = [l.strip() for l in (o or "").splitlines() if l.strip()]
    if "__END__" not in lines:
        raise RuntimeError(f"listing jobs on {host} did not complete (output: {(o or '')[:200]!r})")
    return [l for l in lines if l != "__END__"]


def _running_jobs(host):
    """Jobs that block stopping `host`, as "name (why)" strings ([] = safe to stop).

    Fails closed: only a job whose poll succeeded and says FINISHED is clear. RUNNING, an
    unknown state, a poll that failed (an SSH hiccup included), a job whose files vanished
    between listing and polling - all block, and a failed listing raises."""
    from . import jobs
    names = set()
    for line in _job_logs(host):
        base = line.replace("\\", "/").rsplit("/", 1)[-1]
        if base.startswith("_job_") and base.endswith(".log"):
            names.add(base[len("_job_"):-len(".log")])
    blocking = []
    for nm in sorted(names):
        try:
            r = jobs.job_poll(nm, tail=1, host=host)
        except Exception as e:  # noqa: BLE001
            blocking.append(f"{nm} (poll raised {e.__class__.__name__})")
            continue
        summary = r.summary or ""
        if r.ok and summary.startswith(f"job '{nm}': FINISHED"):
            continue
        if r.ok and "RUNNING" in summary:
            state = "RUNNING"
        elif not r.ok:
            state = f"poll failed: {summary[:80]}"
        else:
            state = f"state unknown: {summary[:80]}"
        blocking.append(f"{nm} ({state})")
    return blocking


def _wait_status(vm, want, timeout):
    deadline = time.monotonic() + timeout
    st = None
    while time.monotonic() < deadline:
        st, _ = _status(vm)
        if st == want:
            return True, st
        time.sleep(2)
    return False, st


def _wait_ssh(host, timeout):
    """Poll SSH until `echo` answers on a FRESH connection, or the timeout passes."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        remote._drop(host)
        try:
            o, _ = remote.ssh_exec("echo __labhand_up__", host=host, read_timeout=15)
            if "__labhand_up__" in (o or ""):
                return True, ""
        except Exception as e:  # noqa: BLE001 - not up yet
            last = f"{e.__class__.__name__}: {e}"
        time.sleep(3)
    return False, last


_SESSIONS = os.path.join(os.path.expanduser("~"), ".labhand", "utm-sessions.json")


def _sessions():
    try:
        with open(_SESSIONS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_sessions(data):
    os.makedirs(os.path.dirname(_SESSIONS), exist_ok=True)
    tmp = _SESSIONS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, _SESSIONS)


def _boot_id(host):
    """An id of the guest's current boot (linux boot_id, Windows LastBootUpTime), or None."""
    try:
        if remote.is_windows(host):
            o, _ = remote.ssh_exec('powershell -NoProfile -Command "(Get-CimInstance '
                                   "Win32_OperatingSystem).LastBootUpTime.ToString('o')\"",
                                   host=host, read_timeout=30)
        else:
            o, _ = remote.ssh_exec("cat /proc/sys/kernel/random/boot_id", host=host,
                                   read_timeout=30)
    except Exception:  # noqa: BLE001
        return None
    v = (o or "").strip().splitlines()
    return v[-1].strip() if v and v[-1].strip() else None


def _record_session(vm, kind, boot):
    data = _sessions()
    data[vm] = {"kind": kind, "boot": boot}
    _save_sessions(data)


def _forget_session(vm):
    data = _sessions()
    if data.pop(vm, None) is not None:
        _save_sessions(data)


def _session_kind(vm, host=None):
    """"disposable" / "normal" if labhand started the session RUNNING NOW (the guest's
    current boot id matches the one recorded at start), else None. A VM restarted from the
    UTM window, or by the guest itself, no longer matches: the record is stale."""
    rec = _sessions().get(vm)
    if not isinstance(rec, dict) or not rec.get("boot"):
        return None
    if host is not None and _boot_id(host) != rec["boot"]:
        return None
    return rec.get("kind")


def _guest_shutdown(host):
    """Ask the guest OS itself to shut down over SSH (best effort). MEASURED 2026-11-14:
    on the Kali VM `utmctl stop --request` (an ACPI power-button event) does nothing:
    xfce4-power-manager holds the handle-power-key inhibitor and only shows a dialog.
    Windows (SeShutdownPrivilege) shuts down on `shutdown /s`. A linux guest shuts down
    only if the SSH user may (passwordless sudo, or polkit says yes)."""
    if remote.is_windows(host):
        cmd = "shutdown /s /t 0"
    else:
        cmd = "sudo -n /usr/bin/systemctl poweroff 2>&1 || systemctl poweroff --no-ask-password"
    try:
        o, e = remote.ssh_exec(cmd, host=host, read_timeout=20)
        return ((o or "") + (e or "")).strip()
    except Exception as ex:  # noqa: BLE001 - the connection dropping is the normal outcome
        return f"({ex.__class__.__name__}: {ex})"


def _sync(host):
    """Flush the guest's dirty pages before a power-off (linux; best effort)."""
    if remote.is_windows(host):
        return
    try:
        remote.ssh_exec("sync", host=host, read_timeout=60)
    except Exception:  # noqa: BLE001
        pass


def vm_stop(host=None, how="request", timeout=180, vm=None, check_jobs=True):
    """Stop a host's VM. how="request": a clean shutdown - the guest is asked over SSH
    (shutdown /s; systemctl poweroff), and UTM sends an ACPI power-button event; if the
    guest does not go down in `timeout` s this fails (nothing is forced). how="force":
    power-off (a linux guest is `sync`ed first); "kill": kill the VM process. Refuses
    while a labhand background job is RUNNING on the host."""
    name, vmn, err = _vm(host, "vm_stop", vm)
    if err:
        return err
    if how not in ("request", "force", "kill"):
        return VMResult(False, "vm_stop: how must be request, force or kill")
    st, out = _status(vmn)
    if st is None:
        return VMResult(False, f"vm_stop: utmctl status {vmn!r} failed: {out[:300]}")
    if st == "stopped":
        _forget_session(vmn)
        return VMResult(True, f"{name} ({vmn}) is already stopped", known=[f"{vmn} stopped"])
    if check_jobs and st == "started":
        try:
            running = _running_jobs(name)
        except Exception as e:  # noqa: BLE001
            return VMResult(False, f"vm_stop: not stopping {name}: {e}")
        if running:
            return VMResult(False, f"vm_stop: not stopping {name}: background job(s) running "
                                   f"or of unknown state there: {', '.join(running)} "
                                   "(job_poll(stop=True) them first, or wait)")
    guest = ""
    if how == "request" and st == "started":
        guest = _guest_shutdown(name)
    elif how == "force" and st == "started":
        _sync(name)
    remote._drop(name)
    rc, out = _utmctl(["stop", vmn, f"--{how}"], timeout=60)
    if rc != 0 and how != "request":
        return VMResult(False, f"vm_stop: utmctl stop {vmn!r} --{how} failed: {out[:300]}")
    ok, st = _wait_status(vmn, "stopped", timeout)
    if not ok:
        return VMResult(False, f"vm_stop: {vmn} still {st!r} {timeout}s after a clean shutdown "
                               f"request (guest said: {guest[:200] or '-'}; utmctl: {out[:120] or 'ok'}). "
                               "The guest refused or ignored it - on a linux guest the SSH user "
                               "needs passwordless sudo/polkit for poweroff, and a desktop power "
                               "manager may swallow the ACPI button. Use how='force' to power off."
                        if how == "request" else
                        f"vm_stop: {vmn} still {st!r} {timeout}s after stop --{how}")
    _forget_session(vmn)
    return VMResult(True, f"stopped {name} ({vmn}) with --{how}", known=[f"{vmn} stopped"])


def vm_start(host=None, disposable=False, timeout=300, vm=None):
    """Start a host's VM (disposable=True: as a snapshot whose disk writes are discarded
    when it stops) and wait until SSH answers."""
    name, vmn, err = _vm(host, "vm_start", vm)
    if err:
        return err
    st, out = _status(vmn)
    if st is None:
        return VMResult(False, f"vm_start: utmctl status {vmn!r} failed: {out[:300]}")
    if st == "started":
        return VMResult(False, f"vm_start: {vmn} is already started; vm_stop it first"
                               + (" (a disposable session must start from stopped)" if disposable else ""))
    args = ["start", vmn] + (["--disposable"] if disposable else [])
    rc, out = _utmctl(args, timeout=90)
    if rc != 0:
        return VMResult(False, f"vm_start: utmctl {' '.join(args)} failed: {out[:300]}")
    _forget_session(vmn)
    t0 = time.monotonic()
    up, why = _wait_ssh(name, timeout)
    if up:
        _record_session(vmn, "disposable" if disposable else "normal", _boot_id(name))
    if not up:
        return VMResult(False, f"vm_start: {vmn} started but SSH to {name} did not answer in "
                               f"{timeout}s ({why})")
    kind = "disposable session (disk writes discarded at stop)" if disposable else "normal session"
    return VMResult(True, f"started {name} ({vmn}) as a {kind}; SSH up after "
                          f"{time.monotonic() - t0:.0f}s",
                    known=[f"{vmn} started, {kind}"])


def vm_revert(host=None, timeout=300, vm=None):
    """Back to the saved disk: stop the VM, then start a new disposable session and wait for
    SSH. A session labhand started as disposable is powered off (it keeps nothing anyway);
    any other session is shut down cleanly, and if the guest will not shut down the revert
    stops there instead of forcing it."""
    name, vmn, err = _vm(host, "vm_revert", vm)
    if err:
        return err
    st, out = _status(vmn)
    if st is None:
        return VMResult(False, f"vm_revert: utmctl status {vmn!r} failed: {out[:300]}")
    if st != "stopped":
        # Power off only a disposable session labhand started in THIS boot of the guest.
        how = "force" if _session_kind(vmn, name) == "disposable" else "request"
        r = vm_stop(name, how=how, timeout=timeout, vm=vmn)
        if not r.ok:
            return VMResult(False, f"vm_revert: {r.summary}")
    r = vm_start(name, disposable=True, timeout=timeout, vm=vmn)
    if not r.ok:
        return VMResult(False, f"vm_revert: {r.summary}")
    return VMResult(True, f"reverted {name} ({vmn}): new disposable session on the saved disk",
                    known=r.known)


# --- snapshots (utmctl >= UTM 5.0.6) ---------------------------------------------------
def snapshots_supported():
    """(True, "") if this utmctl has a snapshot subcommand, else (False, why)."""
    rc, out = _utmctl(["--help"], timeout=30)
    if rc is None:
        return False, out
    if any(line.strip().startswith("snapshot") for line in out.splitlines()):
        return True, ""
    ver = _utmctl(["version"], timeout=30)[1].strip() or "?"
    mac = platform.mac_ver()[0] or platform.system()
    return False, f"{SNAPSHOT_NEEDS}; this Mac has UTM {ver} on macOS {mac} (no snapshot subcommand)"


def _registry():
    try:
        with open(_REGISTRY, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _remember(vm, name, add):
    reg = _registry()
    names = set(reg.get(vm, []))
    (names.add if add else names.discard)(name)
    reg[vm] = sorted(names)
    os.makedirs(os.path.dirname(_REGISTRY), exist_ok=True)
    tmp = _REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=1)
    os.replace(tmp, _REGISTRY)


def vm_snapshot(action, host=None, name=None, vm=None):
    """action: list | create | restore | delete, over `utmctl snapshot`. restore / delete
    only act on snapshots labhand created (recorded in ~/.labhand/utm-snapshots.json)."""
    tool = f"vm_snapshot({action})"
    if action not in ("list", "create", "restore", "delete"):
        return VMResult(False, "vm_snapshot: action must be list, create, restore or delete")
    hname, vmn, err = _vm(host, tool, vm)
    if err:
        return err
    ok, why = snapshots_supported()
    if not ok:
        return VMResult(False, f"{tool}: not available here - {why}")
    if action != "list" and not name:
        return VMResult(False, f"{tool}: give the snapshot `name`")
    if action in ("restore", "delete") and name not in _registry().get(vmn, []):
        return VMResult(False, f"{tool}: {name!r} was not created by labhand for {vmn}; "
                               "labhand only restores or deletes its own snapshots")
    if action == "restore":
        st, _ = _status(vmn)
        if st == "started":
            try:
                running = _running_jobs(hname)
            except Exception as e:  # noqa: BLE001
                return VMResult(False, f"{tool}: {e}")
            if running:
                return VMResult(False, f"{tool}: background job(s) running or of unknown state on {hname}: "
                                       f"{', '.join(running)}")
    # The subcommand's argument order is the one UTM 5.0.6 documents; if a later utmctl
    # differs, its own help comes back in the error.
    args = ["snapshot", action, vmn] + ([name] if name else [])
    rc, out = _utmctl(args, timeout=600)
    if rc != 0:
        helptext = _utmctl(["snapshot", "--help"], timeout=30)[1]
        return VMResult(False, f"{tool}: utmctl {' '.join(args)} failed: {out[:300]}",
                        detail=helptext[:2000])
    if action == "create":
        _remember(vmn, name, True)
    elif action == "delete":
        _remember(vmn, name, False)
    if action == "restore":
        remote._drop(hname)
    return VMResult(True, f"{tool} on {hname} ({vmn}) ok", detail=out,
                    known=[f"utmctl snapshot {action} {vmn} {name or ''}".strip()])
