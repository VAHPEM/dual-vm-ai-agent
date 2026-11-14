import json

import pytest

from beacon import tools, vmstate

KALI_OUT = """##PROC
bash
gdb
kworker/0:1-events
python3
ps
sshd
##PORTS
0.0.0.0:22
127.0.0.1:1234
##FILES
3f9a0c1d2e4b5a67
__RC=0
"""

WIN_OUT = "##PROC\r\nexplorer\r\nx64dbg\r\npowershell\r\nconhost\r\n##PORTS\r\n0.0.0.0:22\r\n##FILES\r\n0123456789abcdef\r\n"


def runner_for(outputs):
    calls = []

    def run(script, host, kind):
        calls.append((host, kind, script))
        return True, outputs[host] if isinstance(outputs[host], str) else outputs[host].pop(0)
    run.calls = calls
    return run


def test_parse_filters_noise():
    fp = vmstate.parse(KALI_OUT, "kali")
    assert fp["processes"] == ["gdb", "python3"]
    assert fp["ports"] == ["0.0.0.0:22", "127.0.0.1:1234"] and fp["files"] == "3f9a0c1d2e4b5a67"
    w = vmstate.parse(WIN_OUT, "windows")
    assert w["processes"] == ["explorer", "x64dbg"] and w["files"] == "0123456789abcdef"
    with pytest.raises(vmstate.VMStateError):
        vmstate.parse("garbage", "kali")


def test_probes_skip_dualvm_bookkeeping_and_use_the_right_shell():
    r = runner_for({"kali": KALI_OUT, "windows": WIN_OUT})
    vmstate.fingerprint("kali", r)
    vmstate.fingerprint("windows", r)
    (h1, k1, s1), (h2, k2, s2) = r.calls
    assert (k1, k2) == ("bash", "powershell")
    assert "! -name '_*'" in s1 and "StartsWith('_')" in s2
    with pytest.raises(vmstate.VMStateError):
        vmstate.fingerprint("macos", r)


def test_diff_and_describe():
    base = vmstate.parse(KALI_OUT, "kali")
    now = dict(base, processes=base["processes"] + ["sample.bin"], ports=base["ports"] + ["0.0.0.0:4444"],
               files="ffffffffffffffff")
    d = vmstate.diff(base, now)
    assert not d["clean"] and d["new_processes"] == ["sample.bin"] and d["files_changed"]
    text = vmstate.describe(d)
    assert text.startswith("DRIFT") and "sample.bin" in text and "4444" in text
    assert vmstate.describe(vmstate.diff(base, base)).startswith("clean")


def test_vm_check_tool_baseline_then_drift(make_run, monkeypatch):
    L = make_run()
    a = L.add_node("P1", "debug the unpacker in gdb", kind="approach")
    drifted = KALI_OUT.replace("python3\n", "python3\nleftover_unpacker\n")
    monkeypatch.setattr(tools, "VM_RUNNER", runner_for({"kali": [KALI_OUT, KALI_OUT, drifted]}))
    assert "baseline recorded for kali" in tools.vm_check(host="kali", run=L.dir)
    out = tools.vm_check(host="kali", node_id=a, run=L.dir)
    assert "clean" in out and "evidence #1" in out
    out = tools.vm_check(host="kali", node_id=a, run=L.dir)
    assert "DRIFT" in out and "leftover_unpacker" in out
    ev = L.evidence(a)
    assert len(ev) == 2 and ev[0]["kind"] == "note"
    assert json.loads(ev[1]["excerpt"])["diff"]["new_processes"] == ["leftover_unpacker"]
    assert L.verify_integrity()[0]


def test_vm_check_reports_a_missing_lab(make_run, monkeypatch):
    L = make_run()

    def down(script, host, kind):
        return False, "ssh: connect to host kali.invalid port 22: Connection refused"

    monkeypatch.setattr(tools, "VM_RUNNER", down)
    out = tools.vm_check(host="kali", run=L.dir)
    assert out.startswith("vm_check failed on kali") and "Connection refused" in out


def test_digest_line_is_picked_even_with_noise_after_it():
    out = KALI_OUT.replace("3f9a0c1d2e4b5a67\n", "3f9a0c1d2e4b5a67\nfind: './x': Permission denied\n")
    assert vmstate.parse(out, "kali")["files"] == "3f9a0c1d2e4b5a67"


def test_default_runner_goes_through_labhand(monkeypatch):
    from labhand import shell
    from labhand._result import VMResult
    seen = []
    monkeypatch.setattr(shell, "run_script", lambda s, host, kind, timeout: seen.append((host, kind)) or
                        VMResult(True, "s", detail="out"))
    assert vmstate.labhand_runner("echo", "kali", "bash") == (True, "out")
    assert seen == [("kali", "bash")]
