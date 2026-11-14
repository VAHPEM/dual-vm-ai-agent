# CLAUDE.md — labhand

> Read this first. It is the authoritative, stable entry point for any AI working
> with this repo: what the package is, how to drive the lab machines, and the full list
> of tools available (run commands, read output, screenshots, GUI automation, Frida,
> gdb, background jobs). Prefer reading this one file over re-reading all of `labhand/`.

## What this is

**The Muscle.** A deterministic, `$0` Python library (no model calls) that lets an AI
**operate** the machines of a lab over SSH — today a **Kali Linux** VM and a **Windows** VM,
more hosts later (see the host inventory below). The checkout is called `labhand/`
here; the folder and the GitHub repo keep their old name `dual-vm-ai-agent`.
Every function runs something on a VM and returns the **raw** output as a plain
`VMResult`. It **never interprets** results and has no concept of "flag", "challenge"
or "task". The deciding Brain lives elsewhere — any agent that imports this package.

## Configure (once)

`labhand` reads, by default, a `.env` and `.ssh_keys/` **next to the package**
(repo root). Override with env vars `LABHAND_ENV` / `LABHAND_SSH_KEY` (the pre-rename
`DUALVM_ENV` / `DUALVM_SSH_KEY` still work as a fallback).

- **Hosts are a named inventory** (`labhand/hosts.py`, example in `.env.example`):
  `LABHAND_HOSTS=kali,windows` and per host `<NAME>_HOST`, `<NAME>_USER`, `<NAME>_OS`
  (`linux` | `windows`), optional `<NAME>_PYTHON`, `<NAME>_UTM_VM`. Without
  `LABHAND_HOSTS` the old keys still work unchanged: `kali` from `KALI_HOST` /
  `KALI_USER` (+ `KALI_PYTHON`), `windows` from `WIN_HOST` / `WIN_USER` (+ `WIN_PYTHON_EXE`).
- `.env`, `.env.*`, `.ssh_keys/`, and any private key are **git-ignored — never commit
  them.** This repo is public.

## Use it (for an AI)

```python
import labhand
from labhand import shell, windows, linux, jobs

r = shell.run_cmd("uname -a")          # -> VMResult
print(r.ok, r.summary, r.detail)

windows.win_screenshot()               # r.image = {"media_type","data"(base64)}
windows.win_windows(match="Error")     # read the Windows desktop as text (cheap)
```

### `VMResult` (what every tool returns)

| field | meaning |
|---|---|
| `ok` | did the call succeed |
| `summary` | one short line (headline) |
| `detail` | the raw output (large output spills to a VM file; page it with `read_file`) |
| `known` | measured facts worth keeping |
| `flag` | a win-string, only if a matcher was injected (see below); else `None` |
| `info` | the text actually shown, for novelty accounting |
| `image` | `{media_type, data}` for screenshot/picture tools, else `None` |
| `waiting` | the step is legitimately waiting on a running job |

### `set_matcher` — inject what a "win string" looks like

The library does not know what a flag is. A Brain that cares teaches it once:

```python
import re, labhand
labhand.set_matcher(lambda t: (re.search(r"...@flare-on\.com", t) or [None])[0])
```

Afterwards tools fill `VMResult.flag` when the pattern appears in raw output.

## Tools — everything you can do on the VMs

All are `$0`, deterministic, and return raw output. `host` is a name from the
inventory; omitted, it is the first host (`win_*`: the first windows host, `linux_*`: the
first linux host). Every tool branches on the host's **OS**, never its name, and an
unknown name comes back as `VMResult(ok=False)` listing the known hosts, before any
connection. Every `win_*` / `linux_*` call below also takes `host=None`.

### Run commands & read output — `labhand.shell`
| call | does |
|---|---|
| `run_cmd(command, host=None, timeout=20)` | run one shell line in `samples/` (linux=bash, windows=cmd.exe); raw output. Real tree-killing timeout. |
| `run_script(script, host=None, kind=None, timeout=20, python=None)` | write & run a multi-line script (`kind`: bash/python3 on linux; powershell/cmd on windows). |
| `read_file(path, offset=0, length=4000, host=None, pinned_docs=())` | page a file on a host; if it is not on `host`, tries every other inventory host in order and says where it was found. |
| `upload_file(local_path, name=None, host=None)` | copy a local file into the host's `samples/`. |

### Windows desktop — read / see / run — `labhand.windows`
| call | does |
|---|---|
| `win_windows(match=None, timeout=40)` | list open windows (process, title, UI-Automation class/control type) as **text** — the cheap way to read a GUI; pass `match` to dump one window's inner text. |
| `win_screenshot(timeout=40)` | capture the interactive desktop as an **image** (`r.image`). |
| `win_gui_run(program, args="", wait=6, kill=True, screenshot=True, timeout=60)` | run a program on the real desktop, wait, report windows + screenshot. |

### Windows GUI automation (act, not just look) — `labhand.windows`
| call | does |
|---|---|
| `win_ui_tree(match=None, limit=250, timeout=45)` | dump the control tree (UI Automation + Win32) of a window. |
| `win_ui_click(hwnd=None, target=None, window=None, x=None, y=None, after=1, timeout=45)` | click a control by handle / name / coordinates. |
| `win_ui_type(text, hwnd=None, target=None, window=None, enter=False, sendkeys=False, after=1, timeout=45)` | type into a control / focused window. |
| `win_key(keys, window=None, after=1, timeout=45)` | send keystrokes (SendKeys syntax). |
| `win_ui_seq(clicks, window=None, pause=250, after=2, limit=250, timeout=120)` | click a sequence of named controls in one pass. |

### Windows instrumentation — `labhand.windows`
| call | does |
|---|---|
| `win_frida(script, program=None, pid=None, args="", wait=8, kill=True, desktop=False, max_lines=800, timeout=90, drive=None, launch=None, bat_text="", bat_names=())` | run a **Frida** JavaScript hook: spawn by `program`, attach by `pid`, or `launch` a `.bat` GUI app; returns every `send()`/`log()` line. `drive=` presses UI buttons while hooked. |

### Linux — headless GUI & debugger — `labhand.linux`
| call | does |
|---|---|
| `linux_gui_run(command, wait=5, screen="1280x800x24", timeout=60)` | run a GUI program on Kali under **Xvfb** and return a screenshot. |
| `linux_gdb(prog, script, args="", gui=True, screen="1280x800x24", max_lines=800, timeout=120)` | run a **gdb-python** script against an ELF (ASLR off, PIE base fixed at `0x555555554000`); returns whatever it `log()`s. Helpers injected: `rd/wr/u64/u32/f64/reg/cstr/at/hook/starti/cont/run`. |

### Background jobs — `labhand.jobs`
| call | does |
|---|---|
| `job_start(name, command=None, script=None, host=None, python=None, truncated=False)` | start work in the background, return immediately with a pid. |
| `job_poll(name=None, tail=40, grep=None, stop=False, host=None)` | check / tail / `grep` / `stop` a job (omit `name` to list all). |

## MCP server — expose the Muscle to any MCP client

```bash
pip install -e ".[mcp]"        # mcp >= 1.7; SDK 1.x (FastMCP) and 2.x (MCPServer) both work
LABHAND_ENV=... LABHAND_SSH_KEY=... labhand-mcp      # stdio; = python -m labhand.mcp_server
```
Each tool above is published as an MCP tool; screenshots come back as image content.
Every tool declares all four MCP hints (readOnly / destructive / idempotent /
openWorld): `read_file`, `win_windows`, `win_screenshot`, `win_ui_tree` are read-only;
everything else is destructive. Keep this when adding a tool (`tests/` enforces it).
The server registers as `labhand`. See `labhand/mcp_server.py`.

## Design invariants (do not break)

- **No interpretation.** Tools return raw output; the Brain concludes.
- **Real, tree-killing timeouts** on every host — a killed call never leaves a process
  running on the VM.
- **Windows background jobs are created through WMI** (`Win32_Process.Create`) and run
  by a per-job launcher: a `Start-Process` child of the SSH session dies ~1 s after the
  call returns, and a one-line `& echo %errorlevel%` is expanded before the command
  runs (it logged `exit 3` as 0). `job_poll(stop=True)` kills the whole tree. Measured
  2026-11-10; `tests/test_jobs.py`.
- **Windows GUI work runs in the interactive session (session 1)** via a scheduled
  task; an SSH login lands in session 0 which has no visible desktop.
- **Large output spills to a per-call file** on the VM; page it back with `read_file`.
- **One pooled SSH connection per host**, reused; a network blip is retried here, not
  surfaced as a failure.
- **The lab is free-action; network isolation is the boundary.** Security/system
  configuration on the VMs (Defender, firewall, registry, services, ACLs, accounts,
  `cipher`, a root shell) may be changed freely. The `labhand.safety` filter refuses
  exactly three disk-wrecking commands: recursive delete of `/`, `mkfs` / `diskpart`,
  `format X:`. What keeps a sample in the lab is the UTM network setup, documented in
  `docs/LAB-ISOLATION.md` and applied by the human.

## Map of the package

`labhand/`: `hosts.py` (the host inventory), `remote.py` (SSH/SFTP pool), `shell.py` (run/read), `windows.py`,
`linux.py`, `jobs.py`, `images.py`, `safety.py`, `_result.py` (`VMResult`,
`set_matcher`), `config.py`, `mcp_server.py`.

`dualvm/`: the deprecated pre-rename name, kept as a thin alias — `import dualvm.X` is
the same module object as `labhand.X` (one DeprecationWarning), and `python -m
dualvm.mcp_server` / the `dualvm-mcp` script still start the server. `research/` and
existing MCP configs rely on it; do not add code there.

`tests/`: VM-free unit tests (`pytest`). License: Apache-2.0.

## Research workspace

For the feasibility-evidence study, read `research/CLAUDE.md` and `research/STATUS.md` first. API costs belong to the separate research harness; the deterministic `labhand` library still makes no model calls.

