# CLAUDE.md — dual-vm-ai-agent (`dualvm`)

> Read this first. It is the authoritative, stable entry point for any AI working
> with this repo: what the package is, how to drive the two VMs, and the full list
> of tools available (run commands, read output, screenshots, GUI automation, Frida,
> gdb, background jobs). Prefer reading this one file over re-reading all of `dualvm/`.

## What this is

**The Muscle.** A deterministic, `$0` Python library (no model calls) that lets an AI
**operate** two virtual machines — a **Kali Linux** VM and a **Windows** VM — over SSH.
Every function runs something on a VM and returns the **raw** output as a plain
`VMResult`. It **never interprets** results and has no concept of "flag", "challenge"
or "task". The deciding Brain lives elsewhere — any agent that imports this package.

## Configure (once)

`dualvm` reads, by default, a `.env` and `.ssh_keys/` **next to the package**
(repo root). Override with env vars `DUALVM_ENV` / `DUALVM_SSH_KEY`.

- `.env` keys used: `KALI_HOST`, `KALI_USER`, `WIN_HOST`, `WIN_USER`, and optionally
  `WIN_PYTHON_EXE`, `KALI_PYTHON`.
- `.env`, `.env.*`, `.ssh_keys/`, and any private key are **git-ignored — never commit
  them.** This repo is public.

## Use it (for an AI)

```python
import dualvm
from dualvm import shell, windows, linux, jobs

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
import re, dualvm
dualvm.set_matcher(lambda t: (re.search(r"...@flare-on\.com", t) or [None])[0])
```

Afterwards tools fill `VMResult.flag` when the pattern appears in raw output.

## Tools — everything you can do on the VMs

All are `$0`, deterministic, and return raw output. Host is `"kali"` or `"windows"`.

### Run commands & read output — `dualvm.shell`
| call | does |
|---|---|
| `run_cmd(command, host="kali", timeout=20)` | run one shell line in `samples/` (kali=bash, windows=cmd.exe); raw output. Real tree-killing timeout. |
| `run_script(script, host="kali", kind=None, timeout=20, python=None)` | write & run a multi-line script (`kind`: bash/python3 on kali; powershell/cmd on windows). |
| `read_file(path, offset=0, length=4000, host="kali", pinned_docs=())` | page a file on a VM; if not on `host` but on the other VM, serves it from there. |

### Windows desktop — read / see / run — `dualvm.windows`
| call | does |
|---|---|
| `win_windows(match=None, timeout=40)` | list open windows (process, title, UI-Automation class/control type) as **text** — the cheap way to read a GUI; pass `match` to dump one window's inner text. |
| `win_screenshot(timeout=40)` | capture the interactive desktop as an **image** (`r.image`). |
| `win_gui_run(program, args="", wait=6, kill=True, screenshot=True, timeout=60)` | run a program on the real desktop, wait, report windows + screenshot. |

### Windows GUI automation (act, not just look) — `dualvm.windows`
| call | does |
|---|---|
| `win_ui_tree(match=None, limit=250, timeout=45)` | dump the control tree (UI Automation + Win32) of a window. |
| `win_ui_click(hwnd=None, target=None, window=None, x=None, y=None, after=1, timeout=45)` | click a control by handle / name / coordinates. |
| `win_ui_type(text, hwnd=None, target=None, window=None, enter=False, sendkeys=False, after=1, timeout=45)` | type into a control / focused window. |
| `win_key(keys, window=None, after=1, timeout=45)` | send keystrokes (SendKeys syntax). |
| `win_ui_seq(clicks, window=None, pause=250, after=2, limit=250, timeout=120)` | click a sequence of named controls in one pass. |

### Windows instrumentation — `dualvm.windows`
| call | does |
|---|---|
| `win_frida(script, program=None, pid=None, args="", wait=8, kill=True, desktop=False, max_lines=800, timeout=90, drive=None, launch=None, bat_text="", bat_names=())` | run a **Frida** JavaScript hook: spawn by `program`, attach by `pid`, or `launch` a `.bat` GUI app; returns every `send()`/`log()` line. `drive=` presses UI buttons while hooked. |

### Linux — headless GUI & debugger — `dualvm.linux`
| call | does |
|---|---|
| `linux_gui_run(command, wait=5, screen="1280x800x24", timeout=60)` | run a GUI program on Kali under **Xvfb** and return a screenshot. |
| `linux_gdb(prog, script, args="", gui=True, screen="1280x800x24", max_lines=800, timeout=120)` | run a **gdb-python** script against an ELF (ASLR off, PIE base fixed at `0x555555554000`); returns whatever it `log()`s. Helpers injected: `rd/wr/u64/u32/f64/reg/cstr/at/hook/starti/cont/run`. |

### Background jobs — `dualvm.jobs`
| call | does |
|---|---|
| `job_start(name, command=None, script=None, host="kali", python=None, truncated=False)` | start work in the background, return immediately with a pid. |
| `job_poll(name=None, tail=40, grep=None, stop=False, host="kali")` | check / tail / `grep` / `stop` a job (omit `name` to list all). |

## MCP server — expose the Muscle to any MCP client

```bash
pip install "mcp>=1.2"
DUALVM_ENV=... DUALVM_SSH_KEY=... python -m dualvm.mcp_server      # stdio
```
Each tool above is published as an MCP tool; screenshots come back as image content.
See `dualvm/mcp_server.py`.

## Design invariants (do not break)

- **No interpretation.** Tools return raw output; the Brain concludes.
- **Real, tree-killing timeouts** on both VMs — a killed call never leaves a process
  running on the VM.
- **Windows GUI work runs in the interactive session (session 1)** via a scheduled
  task; an SSH login lands in session 0 which has no visible desktop.
- **Large output spills to a per-call file** on the VM; page it back with `read_file`.
- **One pooled SSH connection per host**, reused; a network blip is retried here, not
  surfaced as a failure.
- The `dualvm.safety` command filter refuses security-/system-config changes (never
  needed for analysis).

## Map of the package

`dualvm/`: `remote.py` (SSH/SFTP pool), `shell.py` (run/read), `windows.py`,
`linux.py`, `jobs.py`, `images.py`, `safety.py`, `_result.py` (`VMResult`,
`set_matcher`), `config.py`, `mcp_server.py`.
