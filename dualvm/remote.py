# -*- coding: utf-8 -*-
"""Thin SSH/SFTP layer to the Kali and Windows VMs (self-contained, so the
agent package does not import the old LangGraph pipeline)."""
import os
import shlex
import socket
import paramiko
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.environ.get("DUALVM_ENV", os.path.join(_HERE, "..", ".env")))

KALI_HOST = os.environ["KALI_HOST"]
KALI_USER = os.environ["KALI_USER"]
WIN_HOST = os.environ.get("WIN_HOST", "")
WIN_USER = os.environ.get("WIN_USER", "")
WIN_PYTHON_EXE = os.environ.get(
    "WIN_PYTHON_EXE",
    r"C:\Users\vahpem\AppData\Local\Programs\Python\Python314\python.exe",
)
KALI_PYTHON = os.environ.get("KALI_PYTHON", "/home/vahpem/analysis-venv/bin/python3")
SSH_KEY_PATH = os.environ.get("DUALVM_SSH_KEY", os.path.join(
    _HERE, "..", ".ssh_keys", "dualvm_ed25519"))


def _client(host, user, attempts=3):
    """Connect, retrying a TRANSIENT failure. A dropped/refused TCP connection used to
    raise straight into the orchestrator, which counts any tool exception as a dead end
    - so one VM hiccup was indistinguishable from an analytical failure and two of them
    escalated the Brain to the top tier (or ended the run). A network blip is not a
    reasoning failure; retry it here, where it belongs."""
    import time as _t
    last = None
    for i in range(1, attempts + 1):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(host, username=user, key_filename=SSH_KEY_PATH, timeout=10)
            return c
        except (paramiko.SSHException, socket.error, EOFError) as e:
            last = e
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            if i == attempts:
                break
            _t.sleep(1.5 * i)
    raise last


def get_kali():
    return _client(KALI_HOST, KALI_USER)


def get_windows():
    if not WIN_HOST:
        raise RuntimeError("WIN_HOST not configured")
    return _client(WIN_HOST, WIN_USER)


# --- ONE pooled SSH connection per host -------------------------------------------
# MEASURED 2026-09-23: every remote operation built and tore down its own SSH
# connection - 79 ms per call to Kali, 154 ms to Windows, and run_in_session1 alone
# makes four. Worse than the latency, each handshake is a separate chance to fail, and
# any failure raises into the orchestrator as a dead end (-> escalation). One
# transport per host, reused; a channel per command. A dead transport is detected and
# replaced, and a failure to OPEN a channel (the command never started, so retrying is
# safe) is retried once on a fresh connection.
import atexit as _atexit
import time as _time

_POOL = {}


def _drop(host):
    c = _POOL.pop(host, None)
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _get(host):
    c = _POOL.get(host)
    if c is not None:
        t = c.get_transport()
        if t is not None and t.is_active():
            return c
        _drop(host)
    c = get_windows() if host == "windows" else get_kali()
    try:
        c.get_transport().set_keepalive(30)   # survives a long Brain call in between
    except Exception:  # noqa: BLE001
        pass
    _POOL[host] = c
    return c


def _close_pool():
    for h in list(_POOL):
        _drop(h)


_atexit.register(_close_pool)

_CONN_ERRORS = (paramiko.SSHException, EOFError, ConnectionError, OSError)


def _sftp_op(host, fn):
    """Run fn(sftp) on the pooled connection. SFTP operations here are idempotent
    (overwrite a file, read a file, create a dir), so one retry on a fresh connection
    is safe. An IOError from the remote side (no such file) is NOT a connection
    problem and is passed straight through."""
    for attempt in (1, 2):
        c = _get(host)
        try:
            sftp = c.open_sftp()
        except _CONN_ERRORS:
            _drop(host)
            if attempt == 2:
                raise
            continue
        try:
            return fn(sftp)
        finally:
            try:
                sftp.close()
            except Exception:  # noqa: BLE001
                pass


def ssh_exec(cmd, host="kali", read_timeout=None):
    """Run a shell command, return combined (stdout, stderr). read_timeout guards
    against a stalled channel (the Windows path has no shell `timeout`)."""
    for attempt in (1, 2):
        c = _get(host)
        try:
            _, out, err = c.exec_command(cmd, timeout=read_timeout)
            break
        except _CONN_ERRORS:
            # The channel never opened, so the command never ran: safe to retry once.
            _drop(host)
            if attempt == 2:
                raise
    ch = out.channel
    # Drain the CHANNEL directly, taking whatever has arrived. MEASURED 2026-09-23:
    # ChannelFile.read(n) blocks until it has n bytes or EOF, so when the timeout fired
    # the bytes it had already buffered were lost with the exception - `echo partial;
    # sleep 20` came back as "" - i.e. "printed the answer, then hung" still looked like
    # "produced NO output", the failure shape this project has chased twice.
    buf, ebuf = [], []
    deadline = (_time.monotonic() + read_timeout) if read_timeout else None
    timed_out = False
    try:
        while True:
            got = False
            if ch.recv_ready():
                buf.append(ch.recv(65536)); got = True
            if ch.recv_stderr_ready():
                ebuf.append(ch.recv_stderr(65536)); got = True
            if got:
                continue
            if ch.exit_status_ready() or ch.closed or ch.eof_received:
                # the command is over: take the last bytes still in flight, then stop
                while ch.recv_ready():
                    buf.append(ch.recv(65536))
                while ch.recv_stderr_ready():
                    ebuf.append(ch.recv_stderr(65536))
                if ch.exit_status_ready() or ch.closed:
                    break
            if deadline is not None and _time.monotonic() > deadline:
                timed_out = True
                break
            _time.sleep(0.003)
    finally:
        # Close THIS channel only; the pooled transport stays up for the next call.
        try:
            ch.close()
        except Exception:  # noqa: BLE001
            pass
    o = b"".join(buf).decode(errors="replace")
    e = b"".join(ebuf).decode(errors="replace")
    if timed_out:
        e = (e or "") + (f"\n(TIMEOUT after {read_timeout}s - the command was still "
                         f"running; {len(o)} chars of output above arrived before the "
                         "timeout and are REAL)")
    return o, e


def _ensure_self_symlink(sftp, host):
    """Kali only: create samples/samples -> . (idempotent) so a stray 'samples/'
    prefix on an already-samples/-relative path resolves to the same file instead
    of erroring. Measured 2026-09-22 (ch5 ntfsm runs 8/9/10/11): Brain repeatedly
    double-prefixes paths in run_cmd - cwd is already samples/ on kali (per
    _wrap_cmd's `cd samples`) - and in run 11 this alone burned the entire step
    budget before any real analysis happened. Verified on the real VM: ls/grep/
    find all resolve correctly through this symlink, find does not recurse into
    it (no infinite loop, GNU find does not follow symlinks by default), and
    bare filenames (no prefix) are unaffected. Windows is scoped out for now -
    no instance of this failure has been observed there, and directory
    junctions need different handling (elevated rights, different semantics)."""
    if host != "kali":
        return
    try:
        sftp.symlink(".", "samples/samples")
    except IOError:
        pass


def _sftp_makedirs(sftp, dirpath):
    """mkdir -p over SFTP: create each path component in turn, ignoring 'already
    exists'. Needed because an ingested challenge FOLDER keeps its subdir layout, so a
    file's remote path can be e.g. samples/data/gfx/x.bin - paramiko's sftp.mkdir only
    makes ONE level."""
    parts, cur = [pp for pp in dirpath.split("/") if pp], ""
    for pp in parts:
        cur = f"{cur}/{pp}" if cur else pp
        try:
            sftp.mkdir(cur)
        except IOError:
            pass          # already exists (or a race) - the put() below is the real check


def upload(local_path, filename, host="kali"):
    """Put a local file into the remote samples/ dir (subdir layout preserved: a
    `filename` like 'data/x.bin' lands at samples/data/x.bin); return its remote path."""
    remote = f"samples/{filename}"

    def _op(sftp):
        try:
            sftp.mkdir("samples")
        except IOError:
            pass
        parent = remote.rsplit("/", 1)[0]
        if parent != "samples":                 # nested: make samples/data/gfx/...
            _sftp_makedirs(sftp, parent)
        _ensure_self_symlink(sftp, host)
        sftp.put(local_path, remote)
    if host == "windows":
        # A leftover process from an earlier run LOCKS its own .exe image and the DLLs
        # it loaded, so the SFTP put fails with OSError "Failure" (measured ch8 run 8:
        # the .exe upload failed while its DLLs went up fine). Kill any process with
        # this image name and retry with backoff - the handle is not released the
        # instant taskkill returns.
        _base = filename.rsplit("/", 1)[-1]
        _last = None
        for _attempt in range(3):
            try:
                _sftp_op(host, _op)
                return remote
            except (OSError, IOError) as _e:
                _last = _e
                if _base.lower().endswith(".exe"):
                    try:
                        ssh_exec(f'taskkill /F /T /IM "{_base}"', host="windows", read_timeout=15)
                    except Exception:  # noqa: BLE001 - nothing to kill is fine
                        pass
                _time.sleep(0.6 * (_attempt + 1))
        raise _last
    _sftp_op(host, _op)
    return remote


def write_remote(remote_path, content, host="kali"):
    def _op(sftp):
        try:
            sftp.mkdir("samples")
        except IOError:
            pass
        _ensure_self_symlink(sftp, host)
        with sftp.file(remote_path, "w") as f:
            f.write(content)
    _sftp_op(host, _op)


def read_bytes(remote_path, host="kali"):
    """Read a whole remote file over SFTP (no shell => no cmd.exe/bash quoting issues)."""
    def _op(sftp):
        with sftp.file(remote_path, "rb") as f:
            return f.read()
    return _sftp_op(host, _op)


# --- A REAL timeout on Windows ----------------------------------------------------
# MEASURED 2026-09-23: cmd.exe has no `timeout -k`, so the Windows path only had
# paramiko's read_timeout. A solver run with timeout=3 blocked for 13 s (timeout+10),
# was reported as timed out - and KEPT RUNNING on the VM to completion (it wrote its
# marker file 15 s later). That is exactly the zombie the kali path's `timeout -k 5`
# was added to kill: it can clobber the NEXT step's samples/exploit.py or pile heavy
# processes on the VM. This launcher gives Windows the same contract as kali:
# the command runs in samples/, the WHOLE PROCESS TREE is killed at the limit
# (taskkill /T /F), exit code 124 means "timed out", and output streams through as
# it is produced (PYTHONUNBUFFERED) so everything printed before the kill survives.
# The command is read from a FILE and run through cmd.exe /c, so it has exactly the
# quoting semantics it had when it was passed on the SSH command line - no second
# layer of escaping to get wrong.
_WIN_LAUNCHER = r"""# -*- coding: utf-8 -*-
import os, sys, subprocess
here = os.path.dirname(os.path.abspath(__file__))
limit = float(sys.argv[1])
cmdline = open(os.path.join(here, "_win_cmd.txt"), encoding="utf-8").read().strip()
env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
sys.stdout.flush()
p = subprocess.Popen(cmdline, shell=True, cwd=here, env=env, stderr=subprocess.STDOUT)
try:
    rc = p.wait(timeout=limit)
except subprocess.TimeoutExpired:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.wait(timeout=10)
    except Exception:
        pass
    sys.stdout.write("\n__TIMEOUT_KILLED__ after %gs (whole process tree)\n" % limit)
    rc = 124
sys.stdout.flush()
print("__RC=%d" % (rc & 0xFFFFFFFF))
"""


def win_run_bounded(command, timeout):
    """Run a cmd.exe command line in samples/ on Windows with a REAL, tree-killing
    timeout. Returns (stdout, stderr); stdout ends with __RC=<n> (124 = timed out)."""
    write_remote("samples/_win_cmd.txt", command, host="windows")
    write_remote("samples/_timeout_run.py", _WIN_LAUNCHER, host="windows")
    launcher = win_samples() + "\\_timeout_run.py"
    return ssh_exec(f'"{WIN_PYTHON_EXE}" "{launcher}" {int(timeout)}',
                    host="windows", read_timeout=int(timeout) + 25)


def run_script(script, host="kali", timeout=20, python=None):
    """Write exploit.py to samples/ and run it under the right interpreter,
    bounded by a REAL timeout on both hosts. `python` overrides the interpreter
    (absolute path) - needed when a challenge's payload is version-locked to an
    interpreter other than KALI_PYTHON (e.g. a CPython the Brain installed with
    `uv python install 3.12`). Returns combined output."""
    write_remote("samples/exploit.py", script, host=host)
    if host == "windows":
        exe = python or WIN_PYTHON_EXE
        # real timeout + real exit code (was: read_timeout only, rc squashed to 0/1)
        o, e = win_run_bounded(f'"{exe}" exploit.py', timeout)
    else:
        exe = python or KALI_PYTHON
        # `timeout -k 5` runs the child in its OWN process group and signals the
        # whole group, so a solver that forks does not leave orphans behind
        # (measured 2026-09-22: without it, a killed SSH channel left the remote
        # command running to completion on the VM).
        cmd = (f"cd samples && timeout -k 5 {int(timeout)} {shlex.quote(exe)} "
               f"exploit.py 2>&1; echo __RC=$?")
        o, e = ssh_exec(cmd, host="kali", read_timeout=timeout + 25)
    return (o + "\n" + e).strip()


# --- Windows interactive desktop (session 1) ---------------------------------------
# MEASURED 2026-09-22: an SSH login on this Windows VM lands in SESSION 0
# ("services", Disconnected) while the real desktop is session 1 ("console", Active).
# Session 0 has no interactive desktop, so:
#   * CopyFromScreen() there fails outright - "The handle is invalid" (not a black
#     image, a hard error), and SystemInformation.VirtualScreen reports a fake
#     1024x768 instead of the real 1440x822;
#   * a GUI program started over SSH paints its window on a desktop nobody can see,
#     and a MODAL dialog blocks the process until the timeout kills it - exactly the
#     "ran it, got 0 chars of output" failure shape seen on ch4.
# The way across is a scheduled task created with /it (interactive): it runs as the
# logged-on user IN session 1. Verified end to end - a 556KB screenshot of the real
# desktop came back this way.
_WIN_HOME = None


def win_home():
    """Absolute path of the Windows user's profile dir (cached)."""
    global _WIN_HOME
    if _WIN_HOME is None:
        o, _ = ssh_exec("echo %USERPROFILE%", host="windows", read_timeout=25)
        lines = [l.strip() for l in (o or "").splitlines() if ":\\" in l]
        _WIN_HOME = lines[-1] if lines else "C:\\Users\\" + WIN_USER
    return _WIN_HOME


def win_samples():
    return win_home() + "\\samples"


_GUI_DRIVER = """
$S = "{samples}"
$T = "{task}"
Remove-Item -Force -ErrorAction SilentlyContinue "$S\\_gui_out.txt","$S\\_gui_done.txt"
schtasks /create /tn $T /tr "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $S\\_gui_task.ps1" /sc once /st 00:00 /f /it /rl highest 2>&1 | Out-Null
schtasks /run /tn $T 2>&1 | Out-Null
$deadline = (Get-Date).AddSeconds({timeout})
while ((Get-Date) -lt $deadline -and -not (Test-Path "$S\\_gui_done.txt")) {{ Start-Sleep -Milliseconds 400 }}
$done = Test-Path "$S\\_gui_done.txt"
schtasks /end /tn $T 2>&1 | Out-Null
schtasks /delete /tn $T /f 2>&1 | Out-Null
if (Test-Path "$S\\_gui_out.txt") {{ Get-Content -Raw "$S\\_gui_out.txt" }}
if (-not $done) {{ Write-Output "__GUI_TIMEOUT__" }}
Write-Output "__GUI_END__"
"""

_GUI_TASK_WRAPPER = """
$ErrorActionPreference = "Continue"
$S = "{samples}"
$out = & {{
{body}
}} 2>&1
$out | Out-File -FilePath "$S\\_gui_out.txt" -Encoding utf8
"done" | Out-File -FilePath "$S\\_gui_done.txt" -Encoding ascii
"""


def run_in_session1(ps_body, timeout=60, task="dualvm_gui"):
    """Run a PowerShell body on the Windows INTERACTIVE desktop; return (text, timed_out).

    The body may use $S for the samples dir; whatever it Write-Output's comes back.
    """
    samples = win_samples()
    ssh_exec('if not exist "' + samples + '" mkdir "' + samples + '"',
             host="windows", read_timeout=25)
    write_remote("samples/_gui_task.ps1",
                 _GUI_TASK_WRAPPER.format(samples=samples, body=ps_body), host="windows")
    write_remote("samples/_gui_driver.ps1",
                 _GUI_DRIVER.format(samples=samples, task=task, timeout=int(timeout)),
                 host="windows")
    o, e = ssh_exec(
        'powershell -NoProfile -ExecutionPolicy Bypass -File "' + samples + '\\_gui_driver.ps1"',
        host="windows", read_timeout=int(timeout) + 45)
    text = ((o or "") + (e or "")).strip()
    timed_out = "__GUI_TIMEOUT__" in text
    text = text.replace("__GUI_TIMEOUT__", "").replace("__GUI_END__", "").strip()
    return text, timed_out
