# -*- coding: utf-8 -*-
"""The named host inventory (labhand.hosts): parsing, the pre-inventory fallback, unknown
hosts refused before any connection, and every tool branching on the host's OS."""
import pytest

from labhand import hosts, jobs, linux, remote, shell, windows
from labhand._result import VMResult

LEGACY = {"KALI_HOST": "10.0.0.5", "KALI_USER": "k", "WIN_HOST": "10.0.0.10", "WIN_USER": "w"}
THREE = {"LABHAND_HOSTS": "lab1, WinBox ,win-2",
         "LAB1_HOST": "10.0.0.1", "LAB1_USER": "u1", "LAB1_OS": "linux",
         "WINBOX_HOST": "10.0.0.2", "WINBOX_USER": "u2", "WINBOX_OS": "Windows",
         "WINBOX_PYTHON": r"C:\Py\python.exe", "WINBOX_UTM_VM": "Windows 11",
         "WIN_2_HOST": "10.0.0.3", "WIN_2_USER": "u3", "WIN_2_OS": "windows"}


@pytest.fixture
def inv():
    """Install an inventory for one test; the conftest one comes back afterwards."""
    def use(env):
        return hosts.reload(env)
    yield use
    hosts.reload()


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open SSH fails the test."""
    def boom(*a, **k):
        raise AssertionError("a connection was attempted")
    monkeypatch.setattr(remote, "_client", boom)
    monkeypatch.setattr(remote, "_POOL", {})


# ------------------------------------------------------------------ parsing
def test_current_env_without_labhand_hosts_still_gives_kali_and_windows():
    inv = hosts.load(dict(LEGACY, KALI_PYTHON="/v/python3"))
    assert list(inv) == ["kali", "windows"]
    k, w = inv["kali"], inv["windows"]
    assert (k.address, k.user, k.os, k.python) == ("10.0.0.5", "k", "linux", "/v/python3")
    assert (w.address, w.user, w.os) == ("10.0.0.10", "w", "windows")
    assert w.python == hosts.LEGACY_WIN_PYTHON and w.is_windows and not k.is_windows


def test_windows_is_optional_in_the_fallback():
    assert list(hosts.load({"KALI_HOST": "a", "KALI_USER": "b"})) == ["kali"]
    assert hosts.load({}) == {}


def test_labhand_hosts_order_case_and_prefixes():
    inv = hosts.load(THREE)
    assert list(inv) == ["lab1", "winbox", "win-2"]
    assert inv["winbox"].os == "windows" and inv["winbox"].python == r"C:\Py\python.exe"
    assert inv["winbox"].utm_vm == "Windows 11"
    assert inv["win-2"].address == "10.0.0.3"               # WIN_2_* for "win-2"
    assert inv["lab1"].python == "python3" and inv["win-2"].python == "python"


@pytest.mark.parametrize("env, msg", [
    ({"LABHAND_HOSTS": "a", "A_HOST": "h", "A_USER": "u"}, "A_OS is not set"),
    ({"LABHAND_HOSTS": "a", "A_HOST": "h", "A_USER": "u", "A_OS": "macos"}, "must be one of linux, windows"),
    ({"LABHAND_HOSTS": "a,a", "A_HOST": "h", "A_USER": "u", "A_OS": "linux"}, "twice"),
    ({"LABHAND_HOSTS": "bad name"}, "bad host name"),
    ({"LABHAND_HOSTS": " , "}, "names no host"),
])
def test_broken_inventory_is_a_clear_error(env, msg):
    with pytest.raises(hosts.HostError, match=msg):
        hosts.load(env)


def test_broken_inventory_reaches_the_tool_as_a_result_not_a_crash(inv, no_network):
    with pytest.raises(hosts.HostError):
        inv({"LABHAND_HOSTS": "a"})
    r = shell.run_cmd("id", host="a")
    assert r.ok is False and "A_HOST" in r.summary


# ------------------------------------------------------------------ unknown host: refused before connecting
CALLS = [
    ("run_cmd", lambda h: shell.run_cmd("id", host=h)),
    ("run_script", lambda h: shell.run_script("id", host=h)),
    ("read_file", lambda h: shell.read_file("x", host=h)),
    ("upload_file", lambda h: shell.upload_file(__file__, host=h)),
    ("job_start", lambda h: jobs.job_start("j", command="id", host=h)),
    ("job_poll", lambda h: jobs.job_poll("j", host=h)),
    ("linux_gdb", lambda h: linux.linux_gdb("a.out", "run()", host=h)),
    ("linux_gui_run", lambda h: linux.linux_gui_run("xterm", host=h)),
    ("win_windows", lambda h: windows.win_windows(host=h)),
    ("win_screenshot", lambda h: windows.win_screenshot(host=h)),
    ("win_frida", lambda h: windows.win_frida("send(1)", program="a.exe", host=h)),
    ("win_ui_tree", lambda h: windows.win_ui_tree(host=h)),
    ("win_key", lambda h: windows.win_key("a", host=h)),
]


@pytest.mark.parametrize("tool, call", CALLS, ids=[c[0] for c in CALLS])
def test_unknown_host_is_refused_naming_the_known_ones(tool, call, no_network):
    r = call("kaly")                                      # a typo no longer lands on Kali
    assert isinstance(r, VMResult) and r.ok is False
    assert r.summary == f"{tool}: unknown host 'kaly'; known hosts: kali, windows"


@pytest.mark.parametrize("call, msg", [
    (lambda: windows.win_screenshot(host="kali"), "win_screenshot: host 'kali' runs linux, this tool needs a windows host (windows)"),
    (lambda: linux.linux_gdb("a", "run()", host="windows"), "linux_gdb: host 'windows' runs windows, this tool needs a linux host (kali)"),
])
def test_os_specific_tools_refuse_a_host_of_the_other_os(call, msg, no_network):
    r = call()
    assert r.ok is False and r.summary == msg


def test_low_level_calls_refuse_unknown_hosts_too(no_network):
    with pytest.raises(hosts.HostError, match="known hosts: kali, windows"):
        remote.ssh_exec("id", host="nope")
    with pytest.raises(hosts.HostError):
        remote.write_remote("samples/x", "y", host="nope")


# ------------------------------------------------------------------ OS, not name
def test_run_cmd_branches_on_os_not_name(inv, monkeypatch):
    # a windows box called "kali-like" and a linux box called "windows-ish"
    inv({"LABHAND_HOSTS": "kalilike,windowsish",
         "KALILIKE_HOST": "a", "KALILIKE_USER": "u", "KALILIKE_OS": "windows",
         "WINDOWSISH_HOST": "b", "WINDOWSISH_USER": "u", "WINDOWSISH_OS": "linux"})
    seen = []
    monkeypatch.setattr(remote, "win_run_bounded",
                        lambda cmd, t, host=None: seen.append(("win", host)) or ("x\n__RC=0", ""))
    monkeypatch.setattr(remote, "ssh_exec",
                        lambda cmd, host=None, read_timeout=None: seen.append(("ssh", host, "timeout -k" in cmd))
                        or ("x\n__RC=0", ""))
    monkeypatch.setattr(shell, "_spill", lambda host, text, stem: (text, None))
    assert shell.run_cmd("echo x", host="kalilike").ok
    assert shell.run_cmd("echo x", host="windowsish").ok
    assert seen == [("win", "kalilike"), ("ssh", "windowsish", True)]


def test_run_script_uses_the_hosts_own_python_and_default_kind(inv, monkeypatch):
    inv(THREE)
    sent = []
    monkeypatch.setattr(remote, "write_remote", lambda p, c, host=None: None)
    monkeypatch.setattr(shell, "_run_and_wrap",
                        lambda invoke, host, timeout, label, grep_shape=None: sent.append((host, invoke))
                        or VMResult(True, "ok"))
    shell.run_script("print(1)", host="winbox", kind="python3")
    shell.run_script("echo", host="win-2")
    shell.run_script("print(1)", kind="python3")              # default host = first = lab1
    assert sent[0] == ("winbox", r'"C:\Py\python.exe" _script.py')
    assert sent[1][1].startswith("powershell ")                # windows default kind
    assert sent[2] == ("lab1", "python3 _script.py")


def test_default_host_is_the_first_of_the_inventory(inv):
    inv(THREE)
    assert hosts.default() == "lab1"
    assert hosts.default("windows") == "winbox"
    assert hosts.get(None, "windows").name == "winbox"
    assert hosts.get("WINBOX").name == "winbox"                 # names are case-insensitive


def test_read_file_falls_back_over_every_other_host_in_order(inv, monkeypatch):
    inv(THREE)
    asked = []

    def one(path, offset=0, length=4000, host=None):
        asked.append(host)
        if host == "win-2":
            return VMResult(True, "read x", known=["read_file x[0:1]"])
        return VMResult(False, f"read_file: {path} does not exist on {host} (nothing was saved there yet).")
    monkeypatch.setattr(shell, "_read_file_one", one)
    r = shell.read_file("x", host="lab1")
    assert asked == ["lab1", "winbox", "win-2"]
    assert r.ok and "is NOT on lab1 but IS on win-2" in r.summary
    asked.clear()
    monkeypatch.setattr(shell, "_read_file_one",
                        lambda path, offset=0, length=4000, host=None: asked.append(host) or
                        VMResult(False, f"read_file: {path} does not exist on {host} (x)"))
    r = shell.read_file("x", host="winbox")
    assert asked == ["winbox", "lab1", "win-2"]
    assert r.ok is False and "exists on none of winbox, lab1, win-2" in r.summary


def test_pool_key_is_the_inventory_name(inv, monkeypatch):
    inv(THREE)
    made = []

    class FakeT:
        def is_active(self):
            return True

        def set_keepalive(self, n):
            pass

    class FakeC:
        def get_transport(self):
            return FakeT()

        def close(self):
            pass
    monkeypatch.setattr(remote, "_POOL", {})
    monkeypatch.setattr(remote, "_client", lambda addr, user: made.append((addr, user)) or FakeC())
    a = remote._get("WinBox")
    b = remote._get("winbox")
    assert a is b and made == [("10.0.0.2", "u2")] and list(remote._POOL) == ["winbox"]
