# -*- coding: utf-8 -*-
"""
labhand MCP server — expose the Muscle (lab machine control) over the Model Context
Protocol so any external Brain (Claude Desktop, Claude Code, ...) can drive the
Kali + Windows VMs. The server never interprets: each tool runs on a VM and
returns raw output, exactly like the in-process library.

Run:
    LABHAND_ENV=/path/to/.env LABHAND_SSH_KEY=/path/to/key \
        python -m labhand.mcp_server            # stdio transport

Needs the `mcp` package:  pip install "labhand[mcp]"
Works with both SDK lines: mcp 1.x (>= 1.7, `FastMCP`) and mcp 2.x (`MCPServer`).

Tool annotations
----------------
Every tool declares all four MCP behaviour hints so a client can decide what to
auto-approve and what to confirm with the user:

* readOnlyHint    - the tool only observes (lists windows, reads a file,
                    screenshots). Incidental bookkeeping files the library writes
                    on the VM (output spill files, the screenshot PNG) are not
                    counted as modifying the environment.
* destructiveHint - the tool can change or destroy state on the VM. Anything that
                    runs an arbitrary command / program, injects into a process or
                    clicks/types into a GUI is marked destructive, because what it
                    does depends entirely on its arguments.
* idempotentHint  - repeating the same call has no further effect.
* openWorldHint   - the tool can reach beyond the lab hosts (an arbitrary command or
                    program may use the network).

`host` is a name from the lab inventory (LABHAND_HOSTS in .env, or the pre-inventory
kali / windows); omitted, it is the first host (for win_* the first windows host, for
linux_* the first linux host). An unknown name is refused before anything connects.

These are hints for the client UI, not a security boundary. `labhand.safety` only
refuses three disk-wrecking commands (recursive delete of /, mkfs / diskpart,
`format X:`); security/system configuration (Defender, firewall, registry, services,
accounts, ...) may be changed freely on the lab VMs. The boundary is the lab's network
isolation (docs/LAB-ISOLATION.md).
"""
import base64

try:  # mcp 1.x: FastMCP. ToolAnnotations (and tool(annotations=...)) exist from 1.7.
    from mcp.server.fastmcp import FastMCP as _Server, Image
    from mcp.types import ToolAnnotations
except ImportError as _e1:
    try:  # mcp 2.x: FastMCP was renamed MCPServer; protocol types live in mcp_types.
        from mcp.server.mcpserver import MCPServer as _Server, Image
        from mcp_types import ToolAnnotations
    except ImportError as _e2:
        raise SystemExit(
            "labhand.mcp_server needs the MCP SDK (mcp >= 1.7). Install it with:\n"
            "    pip install \"labhand[mcp]\"\n"
            f"(mcp 1.x import error: {_e1}; mcp 2.x import error: {_e2})")

from . import shell, windows, linux, jobs, utm

mcp = _Server("labhand")


def _observe(title):
    """Annotations for a tool that only looks: no state change, safe to repeat,
    stays inside the VMs."""
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)


def _act(title, *, idempotent=False, open_world=True):
    """Annotations for a tool that can change state on a VM."""
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=True,
                           idempotentHint=idempotent, openWorldHint=open_world)


def _emit(r):
    """VMResult -> MCP content: a text block, plus an image block if the tool
    produced a picture (screenshot, rendered page)."""
    text = r.summary + (("\n\n" + r.detail) if r.detail else "")
    if r.match:
        text += f"\n\nMATCH: {r.match}"
    if r.image and r.image.get("data"):
        try:
            return [text, Image(data=base64.b64decode(r.image["data"]),
                                format=r.image["media_type"].split("/")[-1])]
        except Exception:  # noqa: BLE001
            pass
    return text


# --- run commands / read output ---------------------------------------------
@mcp.tool(annotations=_act("Run shell command on a VM"))
def run_cmd(command: str, host: str | None = None, timeout: int = 20):
    """Run a one-line shell command on a VM (bash on a linux host, cmd.exe on a windows host), in the
    samples/ working dir, and return its raw output. Security/system settings
    (Defender, firewall, registry, services, accounts) may be changed freely on the lab
    VMs; only rm -rf /, mkfs/diskpart and `format X:` are refused."""
    return _emit(shell.run_cmd(command, host=host, timeout=timeout))


@mcp.tool(annotations=_act("Run script on a VM"))
def run_script(script: str, host: str | None = None, kind: str | None = None,
               timeout: int = 20, python: str | None = None):
    """Write a multi-line script to the VM and run it (bash/python3 on a linux host;
    powershell/cmd on a windows host). Returns combined output. Same filter as run_cmd:
    only rm -rf /, mkfs/diskpart and `format X:` are refused."""
    return _emit(shell.run_script(script, host=host, kind=kind, timeout=timeout,
                                  python=python))


@mcp.tool(annotations=_act("Upload a local file to a VM", idempotent=True, open_world=False))
def upload_file(local_path: str, name: str | None = None, host: str | None = None):
    """Copy a file from this machine (where the labhand MCP server runs) into the VM's samples/ dir, the
    working dir of run_cmd and run_script. `name` is the path inside samples/ (sub-dirs allowed); default:
    the file's own name. An existing file of that name is replaced."""
    return _emit(shell.upload_file(local_path, name=name, host=host))


# Writes only on this machine, never on the VM; overwrite=True can replace a local file,
# so it is not read-only. Repeating the call gives the same file.
@mcp.tool(annotations=_act("Download a file from a VM", idempotent=True, open_world=False))
def download_file(path: str, dest: str | None = None, host: str | None = None,
                  overwrite: bool = False, max_bytes: int | None = None, timeout: int = 600):
    """Copy a file from a VM to this machine (where the labhand MCP server runs), streamed
    to disk. `path` is relative to the VM's samples/ dir, or absolute. Default destination:
    $LABHAND_DOWNLOAD_DIR/<host>/ or ~/labhand-downloads/<host>/ (never inside a git
    checkout). An existing local file is kept unless overwrite=true. Returns size and
    sha256; the downloaded file is never executed."""
    return _emit(shell.download_file(path, dest=dest, host=host, overwrite=overwrite,
                                     max_bytes=max_bytes, timeout=timeout))


@mcp.tool(annotations=_observe("Read file on a VM"))
def read_file(path: str, offset: int = 0, length: int = 4000, host: str | None = None):
    """Read a byte slice of a file that lives on a VM (pages large files)."""
    return _emit(shell.read_file(path, offset=offset, length=length, host=host))


# --- Windows desktop: read / screenshot / run a GUI program --------------------
@mcp.tool(annotations=_observe("List Windows desktop windows"))
def win_windows(match: str | None = None, timeout: int = 40, host: str | None = None):
    """List the open windows on the Windows VM's interactive desktop as text
    (process, title, UI-Automation class/control type). Pass `match` to dump the
    inner text of one window."""
    return _emit(windows.win_windows(match=match, timeout=timeout, host=host))


@mcp.tool(annotations=_observe("Screenshot Windows desktop"))
def win_screenshot(timeout: int = 40, host: str | None = None):
    """Capture the Windows VM's interactive desktop as an image."""
    return _emit(windows.win_screenshot(timeout=timeout, host=host))


@mcp.tool(annotations=_act("Run GUI program on Windows"))
def win_gui_run(program: str, args: str = "", wait: int = 6, kill: bool = True,
                screenshot: bool = True, timeout: int = 60, host: str | None = None):
    """Run a program on the Windows desktop, wait, then report windows + a
    screenshot."""
    return _emit(windows.win_gui_run(program, args=args, wait=wait, kill=kill,
                                     screenshot=screenshot, timeout=timeout, host=host))


@mcp.tool(annotations=_act("Frida-instrument a Windows process"))
def win_frida(script: str, program: str | None = None, args: str = "",
              pid: int | None = None, wait: int = 8, kill: bool = True, desktop: bool = False,
              max_lines: int = 800, timeout: int = 90, host: str | None = None):
    """Instrument a Windows process with a Frida JavaScript script (spawn by
    `program` or attach by `pid`); returns every send()/log() line."""
    return _emit(windows.win_frida(script, program=program, args=args, pid=pid,
                                   wait=wait, kill=kill, desktop=desktop,
                                   max_lines=max_lines, timeout=timeout, host=host))


@mcp.tool(annotations=_observe("Dump Windows UI control tree"))
def win_ui_tree(match: str | None = None, limit: int = 250, timeout: int = 45, host: str | None = None):
    """Dump the control tree (UI Automation + Win32) of a Windows window."""
    return _emit(windows.win_ui_tree(match=match, limit=limit, timeout=timeout, host=host))


@mcp.tool(annotations=_act("Click Windows UI control"))
def win_ui_click(hwnd: int | None = None, target: str | None = None, window: str | None = None,
                 x: int | None = None, y: int | None = None, after: int = 1, timeout: int = 45, host: str | None = None):
    """Click a control (by handle, name, or coordinates) on the Windows desktop."""
    return _emit(windows.win_ui_click(hwnd=hwnd, target=target, window=window,
                                      x=x, y=y, after=after, timeout=timeout, host=host))


@mcp.tool(annotations=_act("Type text into Windows UI"))
def win_ui_type(text: str, hwnd: int | None = None, target: str | None = None, window: str | None = None,
                enter: bool = False, sendkeys: bool = False, after: int = 1,
                timeout: int = 45, host: str | None = None):
    """Type text into a control / the focused window on the Windows desktop."""
    return _emit(windows.win_ui_type(text, hwnd=hwnd, target=target, window=window,
                                     enter=enter, sendkeys=sendkeys, after=after,
                                     timeout=timeout, host=host))


@mcp.tool(annotations=_act("Send keystrokes on Windows"))
def win_key(keys: str, window: str | None = None, after: int = 1, timeout: int = 45, host: str | None = None):
    """Send keystrokes (SendKeys syntax) to a window on the Windows desktop."""
    return _emit(windows.win_key(keys, window=window, after=after, timeout=timeout, host=host))


@mcp.tool(annotations=_act("Click sequence of Windows UI controls"))
def win_ui_seq(clicks: str, window: str | None = None, pause: int = 250, after: int = 2,
               limit: int = 250, timeout: int = 120, host: str | None = None):
    """Click a sequence of named controls in one pass on the Windows desktop."""
    return _emit(windows.win_ui_seq(clicks, window=window, pause=pause, after=after,
                                    limit=limit, timeout=timeout, host=host))


# --- Linux: GUI headless / debugger -----------------------------------------
@mcp.tool(annotations=_act("Run GUI program on Kali (Xvfb)"))
def linux_gui_run(command: str, wait: int = 5, screen: str = "1280x800x24",
                  timeout: int = 60, host: str | None = None):
    """Run a GUI program on Kali under Xvfb and return a screenshot."""
    return _emit(linux.linux_gui_run(command, wait=wait, screen=screen,
                                     timeout=timeout, host=host))


@mcp.tool(annotations=_act("Run gdb-python script on Kali"))
def linux_gdb(prog: str, script: str, args: str = "", gui: bool = True,
              screen: str = "1280x800x24", max_lines: int = 800, timeout: int = 120, host: str | None = None):
    """Run a gdb-python script against an ELF on Kali (ASLR off, PIE base fixed);
    returns whatever the script log()s / prints."""
    return _emit(linux.linux_gdb(prog, script, args=args, gui=gui, screen=screen,
                                 max_lines=max_lines, timeout=timeout, host=host))


# --- background jobs --------------------------------------------------------
@mcp.tool(annotations=_act("Start background job on a VM"))
def job_start(name: str, command: str | None = None, script: str | None = None,
              host: str | None = None, python: str | None = None, truncated: bool = False):
    """Start a background job on a VM and return immediately with its pid."""
    return _emit(jobs.job_start(name, command=command, script=script, host=host,
                                python=python, truncated=truncated))


# Not read-only: stop=True kills the job. Repeating a poll or a stop changes
# nothing further, and it only touches jobs this library started on the VM.
@mcp.tool(annotations=_act("Poll / stop background job", idempotent=True,
                           open_world=False))
def job_poll(name: str | None = None, tail: int = 40, grep: str | None = None,
             stop: bool = False, host: str | None = None):
    """Check / tail / stop a background job (omit `name` to list all jobs)."""
    return _emit(jobs.job_poll(name=name, tail=tail, grep=grep, stop=stop, host=host))


# --- UTM power / disposable sessions (runs utmctl on THIS machine, the UTM host) -----
@mcp.tool(annotations=_observe("UTM state of a lab VM"))
def vm_status(host: str | None = None):
    """The UTM state (started / stopped / suspended) of a lab host's VM (<NAME>_UTM_VM)."""
    return _emit(utm.vm_status(host))


@mcp.tool(annotations=_act("Start a lab VM", open_world=False))
def vm_start(host: str | None = None, disposable: bool = False, timeout: int = 300):
    """Start a lab host's VM in UTM and wait until SSH answers. disposable=true runs it as a
    snapshot: every disk write of the session is discarded when it stops."""
    return _emit(utm.vm_start(host, disposable=disposable, timeout=timeout))


@mcp.tool(annotations=_act("Stop a lab VM", idempotent=True, open_world=False))
def vm_stop(host: str | None = None, how: str = "request", timeout: int = 180):
    """Stop a lab host's VM: how=request (clean shutdown), force (power off) or kill.
    Refused while a labhand background job is still running on the host."""
    return _emit(utm.vm_stop(host, how=how, timeout=timeout))


@mcp.tool(annotations=_act("Revert a lab VM to its saved disk", open_world=False))
def vm_revert(host: str | None = None, timeout: int = 300):
    """Stop the VM and start a fresh disposable session on its saved disk (all changes of
    a disposable session are gone); waits for SSH."""
    return _emit(utm.vm_revert(host, timeout=timeout))


@mcp.tool(annotations=_act("Snapshot a lab VM", open_world=False))
def vm_snapshot(action: str, host: str | None = None, name: str | None = None):
    """list / create / restore / delete UTM snapshots (utmctl >= UTM 5.0.6; QEMU VMs need
    macOS 26). Refused with the reason where utmctl has no snapshot command. restore and
    delete only act on snapshots labhand created."""
    return _emit(utm.vm_snapshot(action, host=host, name=name))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
