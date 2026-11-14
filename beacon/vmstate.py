# -*- coding: utf-8 -*-
"""beacon.vmstate — has a VM drifted since the baseline?

A dead end is only as good as the environment it was reached in: a hung debugger,
a process left behind by an earlier attempt, a port still bound, or a modified
file in samples/ makes the next approach fail for reasons that have nothing to do
with the approach. The lighthouse cannot see that unless it is recorded.

`fingerprint(host)` reads, through labhand (read-only commands, the samples/
working directory labhand already uses):
  * the set of running process names (kernel threads and our own probe filtered out),
  * the listening TCP ports,
  * a digest of the samples/ tree (path, size, mtime), excluding labhand's own
    bookkeeping files (names starting with "_": _script.*, spill files, screenshots).
`diff(baseline, now)` says what changed. The beacon MCP tool `vm_check` stores the
baseline in the ledger and attaches each check as evidence, so a dead end can carry
"the VM was clean" (or not) next to it.

The runner is injectable: tests (and hosts without the lab) pass a fake.
"""
import json
import re

KALI_PROBE = r"""echo "##PROC"; ps -eo comm= | sort -u
echo "##PORTS"; (ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null | tail -n +3) | awk '{print $4}' | sort -u
echo "##FILES"; find . -maxdepth 3 -type f ! -name '_*' -printf '%P %s %T@\n' 2>/dev/null | sort | sha256sum | cut -c1-16
"""

WIN_PROBE = r"""'##PROC'; Get-Process | Select-Object -ExpandProperty ProcessName | Sort-Object -Unique
'##PORTS'; Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" } | Sort-Object -Unique
'##FILES'; $t = Get-ChildItem -Recurse -File -ErrorAction SilentlyContinue | Where-Object { -not $_.Name.StartsWith('_') } | ForEach-Object { "$($_.FullName) $($_.Length) $($_.LastWriteTimeUtc.Ticks)" } | Sort-Object | Out-String
$s = [System.Security.Cryptography.SHA256]::Create(); ([BitConverter]::ToString($s.ComputeHash([Text.Encoding]::UTF8.GetBytes($t))) -replace '-','').Substring(0,16).ToLower()
"""

# Names that come and go on their own or belong to the probe itself.
_NOISE = {
    "linux": re.compile(r"^(ps|sort|sha256sum|cut|find|bash|sh|dash|awk|ss|netstat|tail|sleep|timeout|sshd|"
                       r"sshd-session|sudo|systemd-.*|kworker.*|ksoftirqd.*|migration.*|rcu_.*|cpuhp.*|"
                       r"idle_inject.*|irq/.*|kthreadd|.*/.*)$"),
    "windows": re.compile(r"^(powershell|conhost|cmd|sshd|sshd-session|WmiPrvSE|RuntimeBroker|"
                          r"backgroundTaskHost|dllhost|SearchProtocolHost|SearchFilterHost|audiodg|"
                          r"MoUsoCoreWorker|TiWorker|TrustedInstaller|smartscreen)$", re.I),
}


class VMStateError(Exception):
    pass


_LEGACY_OS = {"kali": "linux", "windows": "windows"}


def host_os(host):
    """'linux' or 'windows' for a lab host: from labhand's inventory (the probe follows the
    host's OS, never its name); without labhand, the two pre-inventory names."""
    try:
        from labhand import hosts
    except ImportError:
        hosts = None
    if hosts is not None:
        try:
            return hosts.get(host).os
        except hosts.HostError as e:
            raise VMStateError(str(e)) from None
    if host in _LEGACY_OS:
        return _LEGACY_OS[host]
    raise VMStateError("host must be kali or windows")


def labhand_runner(script, host, kind):
    """Default runner: labhand.shell.run_script (imported lazily; needs the lab). No dualvm
    fallback: dualvm is a shim over labhand, so it fails whenever labhand does."""
    from labhand import shell
    r = shell.run_script(script, host=host, kind=kind, timeout=60)
    return bool(r.ok), r.detail or r.summary or ""


def parse(text, host, os_name=None):
    sections, cur = {"PROC": [], "PORTS": [], "FILES": []}, None
    for raw in (text or "").splitlines():
        line = raw.strip().strip("\r")
        m = re.match(r"^##(PROC|PORTS|FILES)$", line)
        if m:
            cur = m.group(1)
            continue
        if cur and line and not line.startswith("__RC="):
            sections[cur].append(line)
    digests = [x.lower() for x in sections["FILES"] if re.fullmatch(r"[0-9a-fA-F]{16}", x)]
    if not digests:
        raise VMStateError("probe output had no samples/ digest (did the probe run?)")
    noise = _NOISE.get(os_name or host_os(host))
    procs = sorted({p for p in sections["PROC"] if not (noise and noise.match(p))})
    ports = sorted(set(sections["PORTS"]))
    return {"host": host, "processes": procs, "ports": ports, "files": digests[-1]}


def fingerprint(host, runner=None):
    os_name = host_os(host)
    runner = runner or labhand_runner
    script, kind = (WIN_PROBE, "powershell") if os_name == "windows" else (KALI_PROBE, "bash")
    ok, text = runner(script, host, kind)
    if not ok:
        raise VMStateError(f"probe failed on {host}: {str(text)[:300]}")
    return parse(text, host, os_name)


def diff(base, now):
    bp, np_ = set(base.get("processes", [])), set(now.get("processes", []))
    bo, no = set(base.get("ports", [])), set(now.get("ports", []))
    d = {"new_processes": sorted(np_ - bp), "gone_processes": sorted(bp - np_),
         "new_ports": sorted(no - bo), "gone_ports": sorted(bo - no),
         "files_changed": base.get("files") != now.get("files")}
    d["clean"] = not (d["new_processes"] or d["new_ports"] or d["files_changed"])
    return d


def describe(d):
    if d["clean"]:
        return "clean: no new processes, no new listening ports, samples/ unchanged"
    parts = []
    if d["new_processes"]:
        parts.append("new processes: " + ", ".join(d["new_processes"][:15]))
    if d["new_ports"]:
        parts.append("new listening ports: " + ", ".join(d["new_ports"][:15]))
    if d["files_changed"]:
        parts.append("samples/ changed since the baseline")
    if d["gone_processes"]:
        parts.append("gone since baseline: " + ", ".join(d["gone_processes"][:10]))
    return "DRIFT: " + "; ".join(parts)


def as_content(fp, d):
    return json.dumps({"fingerprint": fp, "diff": d}, sort_keys=True)
