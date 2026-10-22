# dual-vm-ai-agent (`dualvm`)

**The Muscle.** A deterministic, `$0` library that lets an AI (the Brain) *operate*
two virtual machines — a Kali Linux VM and a Windows VM — over SSH: run commands,
read output, drive GUIs, take and read screenshots, script a debugger (gdb / Frida),
and run background jobs. It **never interprets** results and has no notion of
"flag", "challenge" or "task": every function runs something on a VM and returns the
raw output as a plain `VMResult`. The decision-making Brain lives elsewhere
(e.g. the `ctf-brain` agent), which imports this package.

This is the *Muscle* half of the former `ctf-brain` monorepo: the "what to do"
(reasoning, the API-calling Brain) stayed in `ctf-brain`; the "how to act on the
machines" moved here so it can be reused on its own — directly, or over MCP.

## Install

```bash
pip install -e .            # from a checkout
# deps: paramiko, python-dotenv, pillow ; MCP server: pip install "mcp>=1.2"
```

## Configure

Two VMs reachable over SSH with one key. Point the package at your `.env` and key:

| Environment variable | Meaning | Default |
|---|---|---|
| `DUALVM_ENV` | path to the `.env` holding the host/user vars | `<pkg>/../.env` |
| `DUALVM_SSH_KEY` | path to the private SSH key for both VMs | `<pkg>/../.ssh_keys/ctf_orchestrator_ed25519` |

`.env` keys: `KALI_HOST`, `KALI_USER`, `WIN_HOST`, `WIN_USER`, and optionally
`WIN_PYTHON_EXE`, `KALI_PYTHON`. The key is never printed or committed.

## Use as a library

```python
import dualvm
from dualvm import shell, windows, linux, jobs

r = shell.run_cmd("uname -a")                 # VMResult
print(r.ok, r.summary, r.detail)

shot = windows.win_screenshot()               # r.image = {"media_type","data"(b64)}
txt  = windows.win_windows(match="Error")     # read the desktop as text
```

### The `VMResult` contract

Every tool returns the same plain dataclass — no interpretation, no side vocabulary:

```
ok: bool        summary: str      detail: str       known: list[str]
flag: str|None  info: str|None    image: dict|None  waiting: bool
```

`flag` is only ever filled if the Brain injected a matcher (see below); on its own
the Muscle leaves it `None`.

### Extension point: `set_matcher`

The library does not know what a "flag"/win-string looks like. A Brain that cares
teaches it once:

```python
import re, dualvm
dualvm.set_matcher(lambda t: (re.search(r"...@flare-on\.com", t) or [None])[0])
```

Thereafter tools fill `VMResult.flag` when the pattern appears in raw output. This is
dependency inversion: the Muscle stays generic; the Brain supplies the only
task-specific bit.

## Tools (capabilities preserved from the original agent)

| Area | Tools |
|---|---|
| Run & read output | `shell.run_cmd`, `shell.run_script`, `shell.read_file` (paged; auto-serves from the other VM) |
| Windows: read screen | `windows.win_windows` (titles + UI-Automation tree + inner text) |
| Windows: see screen | `windows.win_screenshot` |
| Windows: run GUI | `windows.win_gui_run` |
| Windows: act on GUI | `windows.win_ui_tree`, `win_ui_click`, `win_ui_type`, `win_key`, `win_ui_seq` |
| Windows: instrument | `windows.win_frida` (spawn / attach / launch-a-.bat, hook + drive the UI) |
| Linux: run GUI headless | `linux.linux_gui_run` (Xvfb + screenshot) |
| Linux: debug | `linux.linux_gdb` (gdb-python, ASLR off, fixed PIE base) |
| Background jobs | `jobs.job_start`, `jobs.job_poll` |

Three tools take an extra parameter that the Brain fills (the only coupling points
that were refactored out of the tool bodies):

- `shell.read_file(..., pinned_docs=())` — names the Brain already has in context.
- `linux.linux_gdb(prog, ...)` — the Brain resolves/uploads the target and passes its
  path.
- `windows.win_frida(..., bat_text="", bat_names=())` — the Brain passes a launcher
  `.bat`'s contents and the known artifact names.
- `jobs.job_start(..., truncated=False)` — the Brain says whether the script it just
  generated was cut off.

## Infrastructure notes (why the code is shaped this way)

- **One pooled SSH connection per host**, reused; a dead transport is detected and
  replaced. A network blip is retried here, not surfaced as a failure.
- **Real, tree-killing timeouts on both VMs** (GNU `timeout -k` on Kali; a Python
  launcher with `taskkill /T` on Windows) so a killed call never leaves a process
  running on the VM.
- **Windows GUI work runs in the interactive session (session 1)** via a scheduled
  task — an SSH login lands in session 0, which has no visible desktop.
- **Large output spills to a per-call file on the VM** and is paged back with
  `read_file`, so nothing is lost to truncation.

## MCP server

Expose the Muscle to any MCP client (Claude Desktop / Claude Code):

```bash
DUALVM_ENV=~/ctf-brain/.env DUALVM_SSH_KEY=~/ctf-brain/.ssh_keys/ctf_orchestrator_ed25519 \
    python -m dualvm.mcp_server
```

Each tool above is published as an MCP tool; screenshots come back as image content.

## License / scope

Built for authorized reverse-engineering and CTF work on VMs you own. The command
filter (`dualvm.safety`) refuses security-/system-config changes that are never
needed for analysis.
