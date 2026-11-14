# -*- coding: utf-8 -*-
"""download_file: streamed in chunks to a temp file then renamed, no overwrite by default,
size cap, disk-space check, timeout, never into a git checkout. No VM: a fake SFTP server."""
import hashlib
import os
import socket
import stat

import pytest

from labhand import remote, shell


class FakeFile:
    def __init__(self, data, log, shrink=False, stall=None):
        self.data, self.log, self.shrink, self.stall = data, log, shrink, stall

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.log.append("closed")

    def readv(self, chunks):
        chunks = list(chunks)
        self.log.append(("readv", len(chunks), sum(n for _, n in chunks)))
        for off, n in chunks:
            if self.stall is not None and off >= self.stall:
                raise socket.timeout("timed out")
            if self.shrink and off >= len(self.data) // 2:
                return                         # the file was cut short while being copied
            yield self.data[off:off + n]


class FakeSFTP:
    def __init__(self, files, log, **kw):
        self.files, self.log, self.kw = files, log, kw

    def stat(self, path):
        self.log.append(("stat", path))
        if path not in self.files:
            raise IOError(2, "No such file")
        v = self.files[path]
        mode = stat.S_IFDIR if v is None else stat.S_IFREG

        class St:
            st_mode, st_size = mode, (0 if v is None else len(v))
        return St()

    def get_channel(self):
        class Ch:
            def settimeout(_, t):
                self.log.append(("settimeout", t))
        return Ch()

    def open(self, path, mode):
        self.log.append(("open", path, mode))
        return FakeFile(self.files[path], self.log, **self.kw)


@pytest.fixture
def lab(monkeypatch, tmp_path):
    """A fake host filesystem; downloads go to tmp_path/dl (outside any git checkout)."""
    state = {"files": {}, "log": [], "kw": {}, "dropped": []}
    monkeypatch.setattr(remote, "_sftp_op",
                        lambda host, fn: fn(FakeSFTP(state["files"], state["log"], **state["kw"])))
    monkeypatch.setattr(remote, "_drop", lambda host: state["dropped"].append(host))
    monkeypatch.setenv("LABHAND_DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setattr(shell, "_git_checkout_of", lambda p: None)
    return state


def _parts(folder):
    return [p for p in os.listdir(folder) if p.endswith(".part")]


def test_streams_to_the_default_dir_with_size_and_sha256(lab, tmp_path):
    data = os.urandom(20 * (1 << 20) + 123)          # 3 windows of 8 MiB
    lab["files"]["samples/dump.raw"] = data
    r = shell.download_file("dump.raw", host="kali")
    target = tmp_path / "dl" / "kali" / "dump.raw"
    assert r.ok, r.summary
    assert target.read_bytes() == data
    digest = hashlib.sha256(data).hexdigest()
    assert f"sha256 {digest}" in r.known and f"size {len(data)} bytes" in r.known
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600          # never executable
    assert _parts(target.parent) == []
    windows = [e[2] for e in lab["log"] if isinstance(e, tuple) and e[0] == "readv"]
    assert len(windows) == 1 + len(data) // (8 << 20) and max(windows) == 8 << 20  # bounded
    assert sum(windows) == len(data)


def test_existing_file_is_kept_unless_overwrite(lab, tmp_path):
    lab["files"]["samples/a.bin"] = b"new"
    out = tmp_path / "dl" / "kali"
    out.mkdir(parents=True)
    (out / "a.bin").write_bytes(b"old")
    r = shell.download_file("a.bin", host="kali")
    assert not r.ok and "already exists" in r.summary and (out / "a.bin").read_bytes() == b"old"
    assert not any(isinstance(e, tuple) and e[0] == "open" for e in lab["log"])  # nothing fetched
    r = shell.download_file("a.bin", host="kali", overwrite=True)
    assert r.ok and (out / "a.bin").read_bytes() == b"new" and _parts(out) == []


def test_explicit_dest_file_and_directory(lab, tmp_path):
    lab["files"]["/var/log/x.log"] = b"log"
    d = tmp_path / "here"
    d.mkdir()
    assert shell.download_file("/var/log/x.log", dest=str(d), host="kali").ok
    assert (d / "x.log").read_bytes() == b"log"
    assert shell.download_file("/var/log/x.log", dest=str(tmp_path / "new" / "y.log"), host="kali").ok
    assert (tmp_path / "new" / "y.log").read_bytes() == b"log"
    assert shell.download_file("/var/log/x.log", dest=str(tmp_path / "newdir") + "/", host="kali").ok
    assert (tmp_path / "newdir" / "x.log").read_bytes() == b"log"


def test_size_cap_is_checked_before_the_first_byte(lab, tmp_path):
    lab["files"]["samples/big"] = b"x" * 100
    r = shell.download_file("big", host="kali", max_bytes=99)
    assert not r.ok and "over the 99-byte cap" in r.summary
    assert not any(isinstance(e, tuple) and e[0] == "open" for e in lab["log"])
    assert not (tmp_path / "dl" / "kali" / "big").exists()
    assert _parts(tmp_path / "dl" / "kali") == []


def test_a_file_cut_short_while_copied_fails_and_leaves_nothing(lab, tmp_path):
    lab["files"]["samples/cut"] = b"x" * (1 << 20)
    lab["kw"] = {"shrink": True}
    r = shell.download_file("cut", host="kali")
    assert not r.ok and "changed while it was copied" in r.summary
    folder = tmp_path / "dl" / "kali"
    assert not (folder / "cut").exists() and _parts(folder) == []


def test_not_enough_disk_space_is_refused(lab, monkeypatch, tmp_path):
    lab["files"]["samples/d"] = b"x" * 50
    monkeypatch.setattr(shell.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    r = shell.download_file("d", host="kali")
    assert not r.ok and "only 10 bytes are free" in r.summary
    assert _parts(tmp_path / "dl" / "kali") == []


def test_missing_file_and_directory(lab):
    r = shell.download_file("nope.bin", host="kali")
    assert not r.ok and "nope.bin does not exist on kali" in r.summary
    lab["files"]["samples/dir"] = None
    r = shell.download_file("dir", host="kali")
    assert not r.ok and "is a directory" in r.summary


def test_a_broken_transfer_drops_the_connection_and_the_temp_file(lab, tmp_path):
    lab["files"]["samples/s"] = b"y" * (3 << 20)
    lab["kw"] = {"stall": 1 << 20}
    r = shell.download_file("s", host="kali")
    assert not r.ok and "stalled" in r.summary
    assert lab["dropped"] == ["kali"]
    assert _parts(tmp_path / "dl" / "kali") == [] and not (tmp_path / "dl" / "kali" / "s").exists()


def test_overall_timeout(lab, monkeypatch, tmp_path):
    lab["files"]["samples/t"] = b"z" * (40 << 20)
    clock = iter(range(0, 10_000, 7))
    monkeypatch.setattr(remote._time, "monotonic", lambda: next(clock))
    r = shell.download_file("t", host="kali", timeout=10)
    assert not r.ok and "timed out after 10s" in r.summary
    assert lab["dropped"] == ["kali"]                               # cut mid-window: replies in flight
    assert _parts(tmp_path / "dl" / "kali") == []


def test_inside_a_git_checkout_is_refused_even_through_a_symlink(lab, monkeypatch, tmp_path):
    monkeypatch.undo()                                             # the real _git_checkout_of
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    lab_files = {"samples/m.exe": b"MZ"}
    monkeypatch.setattr(remote, "_sftp_op", lambda host, fn: fn(FakeSFTP(lab_files, [])))
    r = shell.download_file("m.exe", dest=str(repo / "samples"), host="windows")
    assert not r.ok and "inside the git checkout" in r.summary
    assert not (repo / "samples").exists()
    link = tmp_path / "innocent"
    link.symlink_to(repo)
    r = shell.download_file("m.exe", dest=str(link) + "/", host="windows")
    assert not r.ok and "inside the git checkout" in r.summary


def test_windows_paths_and_unknown_hosts(lab, tmp_path):
    lab["files"]["/C:/Windows/x.dll"] = b"dll"
    lab["files"]["samples/sub/a.bin"] = b"a"
    assert shell.download_file(r"C:\Windows\x.dll", host="windows").ok
    assert (tmp_path / "dl" / "windows" / "x.dll").read_bytes() == b"dll"
    assert shell.download_file(r"sub\a.bin", host="windows").ok
    r = shell.download_file("a", host="kaly")
    assert not r.ok and r.summary == "download_file: unknown host 'kaly'; known hosts: kali, windows"


def test_default_dir_is_outside_this_repo(monkeypatch):
    monkeypatch.delenv("LABHAND_DOWNLOAD_DIR", raising=False)
    d = shell.download_dir("kali")
    assert d == os.path.join(os.path.expanduser("~"), "labhand-downloads", "kali")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not d.startswith(root + os.sep)
