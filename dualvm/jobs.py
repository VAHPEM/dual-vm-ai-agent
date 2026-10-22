# -*- coding: utf-8 -*-
"""dualvm.jobs — tach tu ctf-brain tools.py (pre-split), verbatim.
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
import re, ast, shlex
from . import remote
from ._result import VMResult, _spill, _find_match  # noqa
from .safety import _security_block


# --- Long jobs: work that outlives one tool call ------------------------------------
# Every other tool here is synchronous and capped by `timeout` (run_cmd 20s, author_and_run
# 20s, run_in_session1 60s). That ceiling is fine for a solver and fatal for a batch:
# ch9-scale work (thousands of files, each unpacked and analysed) cannot finish inside
# any of them, and raising the cap would just block the whole loop on one call while the
# step budget burns. So: start it detached, keep working, poll it.
# setsid+nohup+</dev/null matters - without all three the job dies when the SSH channel
# for THIS call closes, which looks exactly like "the script crashed silently".

_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def _job_paths(name):
    return f"_job_{name}.log", f"_job_{name}.pid", f"_job_{name}.py"


def job_start(name, command=None, script=None, host="kali", python=None, truncated=False):
    nm = str(name or "").strip()
    if not _JOB_NAME_RE.match(nm):
        return VMResult(False, "job_start: `name` must be 1-40 chars of letters, "
                                 "digits, '_', '-' or '.'")
    if not command and not script:
        return VMResult(False, "job_start: give either `command` or `script`")
    if command and script:
        return VMResult(False, "job_start: give `command` OR `script`, not both")
    blocked = _security_block(command or script)
    if blocked:
        return VMResult(False, f"blocked: this job touches {blocked}")
    log, pidf, pyf = _job_paths(nm)

    if script:
        if truncated:
            return VMResult(False, "job_start: the script you just wrote was CUT OFF "
                                     "mid-generation, so it is incomplete - not "
                                     "uploading it. Write a shorter one.")
        try:
            ast.parse(script)
        except SyntaxError as e:
            return VMResult(False, f"job_start: that script does not parse "
                                     f"(line {e.lineno}: {e.msg}) - fix it first")
        try:
            remote.write_remote(f"samples/{pyf}", script, host=host)
        except Exception as e:  # noqa: BLE001
            return VMResult(False, f"job_start: could not upload the script: {e}")

    if host == "windows":
        interp = python or remote.WIN_PYTHON_EXE
        inner = (f'"{interp}" -u {pyf}' if script else command)
        ps = (f'$ErrorActionPreference="Continue"\n'
              f'Set-Location "{remote.win_samples()}"\n'
              f'Remove-Item -Force -ErrorAction SilentlyContinue "{log}","{pidf}"\n'
              f'$p = Start-Process -FilePath "cmd.exe" -ArgumentList '
              f'\'/c {inner} > {log} 2>&1 & echo __JOB_RC=%errorlevel% >> {log}\' '
              f'-WindowStyle Hidden -PassThru\n'
              f'$p.Id | Out-File -Encoding ascii "{pidf}"\n'
              f'Write-Output ("JOB_PID=" + $p.Id)\n')
        try:
            remote.write_remote(f"samples/_job_{nm}_start.ps1", ps, host="windows")
            o, e = remote.ssh_exec(
                f'powershell -NoProfile -ExecutionPolicy Bypass -File '
                f'"{remote.win_samples()}\\_job_{nm}_start.ps1"',
                host="windows", read_timeout=40)
            out = ((o or "") + (e or "")).strip()
        except Exception as e:  # noqa: BLE001
            return VMResult(False, f"job_start failed: {e.__class__.__name__}: {e}")
    else:
        interp = python or remote.KALI_PYTHON
        inner = (f"{shlex.quote(interp)} -u {pyf}" if script else command)
        wrapped = f"{inner}\n__rc=$?\necho __JOB_RC=$__rc"
        sh = ("cd samples 2>/dev/null || exit 9\n"
              f"rm -f {log} {pidf}\n"
              f"setsid nohup bash -c {shlex.quote(wrapped)} > {log} 2>&1 < /dev/null &\n"
              f"echo $! > {pidf}\n"
              "sleep 0.4\n"
              f"echo JOB_PID=$(cat {pidf})\n")
        try:
            o, e = remote.ssh_exec(f"bash -c {shlex.quote(sh)}", host=host,
                                   read_timeout=40)
            out = ((o or "") + (e or "")).strip()
        except Exception as e:  # noqa: BLE001
            return VMResult(False, f"job_start failed: {e.__class__.__name__}: {e}")

    m = re.search(r"JOB_PID=(\d+)", out)
    if not m:
        return VMResult(False, f"job_start: the job did not report a pid, so it did "
                                 f"NOT start. Output: {out[:400]}")
    pid = m.group(1)
    return VMResult(True, f"job '{nm}' started on {host} (pid {pid}) - it keeps "
                            f"running after this call; check it with job_poll(name='{nm}')",
                      detail=out[:600],
                      known=[f"job '{nm}' running on {host}, pid {pid}, log samples/{log}"])


def job_poll(name=None, tail=40, grep=None, stop=False, host="kali"):
    if name is None:
        cmd = ("cd samples 2>/dev/null && ls -la _job_*.log 2>/dev/null | head -40 "
               "|| echo 'no jobs'") if host != "windows" else \
              f'dir /b "{remote.win_samples()}\\_job_*.log"'
        try:
            o, e = remote.ssh_exec(cmd if host == "windows" else f"bash -c {shlex.quote(cmd)}",
                                   host=host, read_timeout=30)
        except Exception as ex:  # noqa: BLE001
            return VMResult(False, f"job_poll failed: {ex.__class__.__name__}: {ex}")
        txt = ((o or "") + (e or "")).strip() or "no jobs"
        return VMResult(True, "jobs on " + host, detail=txt[:1500], info=txt[:1500])

    nm = str(name).strip()
    if not _JOB_NAME_RE.match(nm):
        return VMResult(False, "job_poll: bad `name`")
    log, pidf, _ = _job_paths(nm)
    n = max(1, int(tail))

    if host == "windows":
        ps = [f'Set-Location "{remote.win_samples()}"',
              f'if (-not (Test-Path "{log}")) {{ Write-Output "NO_SUCH_JOB" }} else {{',
              f'  $pid0 = (Get-Content "{pidf}" -ErrorAction SilentlyContinue | Select-Object -First 1)',
              f'  $alive = $false',
              f'  if ($pid0) {{ $alive = [bool](Get-Process -Id $pid0 -ErrorAction SilentlyContinue) }}']
        if stop:
            ps.append('  if ($alive) { Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue; '
                      'Write-Output "STOPPED"; $alive = $false }')
        ps += ['  Write-Output ("STATE=" + $(if ($alive) {"RUNNING"} else {"FINISHED"}))',
               f'  Write-Output ("SIZE=" + (Get-Item "{log}").Length)',
               f'  $c = Get-Content "{log}"',
               '  Write-Output ("LINES=" + $c.Count)',
               '  $rc = $c | Select-String "__JOB_RC=" | Select-Object -Last 1',
               '  if ($rc) { Write-Output ("EXIT " + $rc.Line.Trim()) }']
        if grep:
            g = str(grep).replace('"', "")
            ps.append(f'  Write-Output "--- lines matching {g} ---"')
            ps.append(f'  $c | Select-String "{g}" | Select-Object -Last {n} | ForEach-Object {{ $_.Line }}')
        else:
            ps.append(f'  Write-Output "--- last {n} lines ---"')
            ps.append(f'  $c | Select-Object -Last {n}')
        ps.append('}')
        body = "\n".join(ps)
        try:
            remote.write_remote(f"samples/_job_{nm}_poll.ps1", body, host="windows")
            o, e = remote.ssh_exec(
                f'powershell -NoProfile -ExecutionPolicy Bypass -File '
                f'"{remote.win_samples()}\\_job_{nm}_poll.ps1"',
                host="windows", read_timeout=45)
            txt = ((o or "") + (e or "")).strip()
        except Exception as ex:  # noqa: BLE001
            return VMResult(False, f"job_poll failed: {ex.__class__.__name__}: {ex}")
    else:
        parts = ["cd samples 2>/dev/null || exit 9",
                 f'[ -f {log} ] || {{ echo NO_SUCH_JOB; exit 0; }}',
                 f'pid=$(cat {pidf} 2>/dev/null)']
        if stop:
            parts.append('[ -n "$pid" ] && kill -TERM -"$pid" 2>/dev/null; '
                         '[ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null; '
                         'sleep 0.3; echo STOP_SENT')
        parts += ['if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; '
                  'then echo STATE=RUNNING; else echo STATE=FINISHED; fi',
                  f'echo SIZE=$(wc -c < {log})',
                  f'echo LINES=$(wc -l < {log})',
                  f'grep -o "__JOB_RC=[0-9-]*" {log} | tail -1 | sed "s/^/EXIT /"']
        if grep:
            # quoted: a pattern containing " or $ corrupted (or escaped) this echo
            parts.append("echo " + shlex.quote(f"--- lines matching {grep} ---"))
            parts.append(f'grep -E -- {shlex.quote(str(grep))} {log} | tail -{n}')
        else:
            parts.append(f'echo "--- last {n} lines ---"')
            parts.append(f'tail -n {n} {log}')
        sh = "\n".join(parts)
        try:
            o, e = remote.ssh_exec(f"bash -c {shlex.quote(sh)}", host=host,
                                   read_timeout=60)
            txt = ((o or "") + (e or "")).strip()
        except Exception as ex:  # noqa: BLE001
            return VMResult(False, f"job_poll failed: {ex.__class__.__name__}: {ex}")

    if "NO_SUCH_JOB" in txt:
        return VMResult(False, f"job_poll: no job named '{nm}' on {host} - "
                                 "job_poll() with no name lists what is there")
    state = (re.search(r"STATE=(\w+)", txt) or None)
    state = state.group(1) if state else "?"
    rc = re.search(r"__JOB_RC=(-?\d+)", txt)
    size = re.search(r"SIZE=(\d+)", txt)
    head = f"job '{nm}': {state}"
    if rc:
        head += f", exit code {rc.group(1)}"
    if size:
        head += f", log {size.group(1)} bytes (samples/{log} - page it with read_file)"
    detail, _ = _spill(host, txt, f"job_{nm}")
    return VMResult(True, head, detail=detail, info=detail,
                      known=[f"job '{nm}' {state}" + (f" rc={rc.group(1)}" if rc else "")],
                      flag=_find_match(txt), waiting=(state == "RUNNING"))
