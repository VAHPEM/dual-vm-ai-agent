# -*- coding: utf-8 -*-
"""labhand.hosts — the named inventory of lab hosts.

Read from the environment (the `.env` labhand loads):

    LABHAND_HOSTS=kali,windows,win2         # names, in order; the first is the default host
    KALI_HOST=192.168.64.5                  # per host <NAME>_HOST, <NAME>_USER, <NAME>_OS
    KALI_USER=vahpem
    KALI_OS=linux                           # linux | windows
    KALI_PYTHON=/home/vahpem/venv/bin/python3   # optional: interpreter for run_script / jobs
    KALI_UTM_VM=Kali Linux                  # optional: the UTM VM name (snapshots)

<NAME> is the host name upper-cased, with `-` turned into `_` (`win-2` -> `WIN_2_HOST`).

Without LABHAND_HOSTS the inventory is built from the pre-inventory keys, so an existing
`.env` works unchanged: `kali` from KALI_HOST / KALI_USER (linux, KALI_PYTHON) and, when
WIN_HOST is set, `windows` from WIN_HOST / WIN_USER (windows, WIN_PYTHON_EXE).

Every tool branches on a host's OS, never on its name. An unknown name is refused
before any connection is made, naming the hosts that exist.
"""
import os
import re
from typing import NamedTuple

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))


def env_path(new, old, default):
    """LABHAND_* wins; the pre-rename DUALVM_* name still works as a fallback."""
    return os.environ.get(new) or os.environ.get(old) or default


# The .env is loaded here, the first module that reads the environment.
load_dotenv(env_path("LABHAND_ENV", "DUALVM_ENV", os.path.join(_HERE, "..", ".env")))

OSES = ("linux", "windows")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Interpreter defaults of the original two-VM lab (used only for the fallback inventory).
LEGACY_KALI_PYTHON = "/home/vahpem/analysis-venv/bin/python3"
LEGACY_WIN_PYTHON = r"C:\Users\vahpem\AppData\Local\Programs\Python\Python314\python.exe"


class Host(NamedTuple):
    name: str
    address: str
    user: str
    os: str            # "linux" | "windows"
    python: str        # interpreter for run_script / job scripts on this host
    utm_vm: str = ""   # UTM VM name, "" if not given

    @property
    def is_windows(self):
        return self.os == "windows"


class HostError(ValueError):
    """An unknown host name or a broken inventory; the message is meant for the caller."""


_WD_OK = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def workdir():
    """The working directory of every tool on every host, relative to the SSH user's home:
    $LABHAND_WORKDIR, default `samples` (both OSes). Letters, digits, `._-` and `/` only, so
    it needs no quoting in bash, cmd.exe or PowerShell. Raises HostError if invalid."""
    wd = os.environ.get("LABHAND_WORKDIR", "").strip().replace("\\", "/").rstrip("/") or "samples"
    if not _WD_OK.match(wd) or any(part in (".", "..") for part in wd.split("/")):
        raise HostError(f"LABHAND_WORKDIR={wd!r}: must be a relative path inside the home "
                        "directory, made of letters, digits, '.', '_', '-' and '/' (no '..')")
    return wd


def workdir_win():
    """workdir() with Windows separators."""
    return workdir().replace("/", "\\")


def prefix(name):
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def load(env=None):
    """Build the inventory {name: Host} (insertion order = LABHAND_HOSTS order) from `env`."""
    env = os.environ if env is None else env
    names = env.get("LABHAND_HOSTS", "").strip()
    if not names:
        return _legacy(env)
    inv = {}
    for raw in names.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if not _NAME.match(name):
            raise HostError(f"LABHAND_HOSTS: bad host name {raw.strip()!r} "
                            "(letters, digits, '-' and '_', starting with a letter or digit)")
        if name in inv:
            raise HostError(f"LABHAND_HOSTS names {name!r} twice")
        p = prefix(name)
        vals = {k: env.get(f"{p}_{k}", "").strip() for k in ("HOST", "USER", "OS", "PYTHON", "UTM_VM")}
        missing = [f"{p}_{k}" for k in ("HOST", "USER", "OS") if not vals[k]]
        if missing:
            raise HostError(f"host {name!r} is listed in LABHAND_HOSTS but {', '.join(missing)} "
                            "is not set")
        osname = vals["OS"].lower()
        if osname not in OSES:
            raise HostError(f"{p}_OS={vals['OS']!r}: must be one of {', '.join(OSES)}")
        inv[name] = Host(name, vals["HOST"], vals["USER"], osname,
                         vals["PYTHON"] or ("python" if osname == "windows" else "python3"),
                         vals["UTM_VM"])
    if not inv:
        raise HostError("LABHAND_HOSTS is set but names no host")
    return inv


def _legacy(env):
    inv = {}
    if env.get("KALI_HOST", "").strip():
        inv["kali"] = Host("kali", env["KALI_HOST"].strip(), env.get("KALI_USER", "").strip(),
                           "linux", env.get("KALI_PYTHON", "").strip() or LEGACY_KALI_PYTHON,
                           env.get("KALI_UTM_VM", "").strip())
    if env.get("WIN_HOST", "").strip():
        inv["windows"] = Host("windows", env["WIN_HOST"].strip(), env.get("WIN_USER", "").strip(),
                              "windows", env.get("WIN_PYTHON_EXE", "").strip() or LEGACY_WIN_PYTHON,
                              env.get("WIN_UTM_VM", "").strip())
    return inv


_CACHE = {}


def inventory():
    """The inventory, read once from os.environ (reload() re-reads it). Raises HostError if
    the configuration is broken."""
    if "inv" not in _CACHE:
        try:
            _CACHE["inv"] = load()
        except HostError as e:
            _CACHE["inv"] = e
    inv = _CACHE["inv"]
    if isinstance(inv, HostError):
        raise inv
    return inv


def reload(env=None):
    """Re-read the inventory (tests, or after the environment changed)."""
    _CACHE.clear()
    if env is not None:
        try:
            _CACHE["inv"] = load(env)
        except HostError as e:
            _CACHE["inv"] = e        # tools now report it, as they would for a broken .env
    return inventory()


def names():
    return list(inventory())


def _known():
    try:
        inv = inventory()
    except HostError as e:
        return f"(inventory broken: {e})"
    return ", ".join(inv) or "(none configured: set LABHAND_HOSTS or KALI_HOST / WIN_HOST in .env)"


def default(os_name=None):
    """Name of the default host: the first in the inventory, or the first with OS `os_name`."""
    for h in inventory().values():
        if os_name is None or h.os == os_name:
            return h.name
    raise HostError(f"no {os_name or ''} host in the inventory; known hosts: {_known()}".replace("  ", " "))


def get(name=None, os_name=None):
    """The Host called `name` (None: the default host, of OS `os_name` if given). Raises
    HostError naming the known hosts when `name` is not one of them, or has the wrong OS."""
    inv = inventory()
    if name is None or name == "":
        name = default(os_name)
    key = str(name).strip().lower()
    h = inv.get(key)
    if h is None:
        raise HostError(f"unknown host {name!r}; known hosts: {_known()}")
    if os_name is not None and h.os != os_name:
        others = [x.name for x in inv.values() if x.os == os_name]
        raise HostError(f"host {h.name!r} runs {h.os}, this tool needs a {os_name} host"
                        + (f" ({', '.join(others)})" if others else " and there is none"))
    return h


def check(name, tool, os_name=None):
    """(Host, None) when `name` resolves, else (None, VMResult(ok=False)) for the tool to
    return as is. Nothing connects to anything here."""
    from ._result import VMResult
    try:
        workdir()                     # a broken LABHAND_WORKDIR is refused here, too
        return get(name, os_name), None
    except HostError as e:
        return None, VMResult(False, f"{tool}: {e}")
