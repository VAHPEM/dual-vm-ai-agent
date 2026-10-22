# -*- coding: utf-8 -*-
"""
dualvm MCP server — expose the Muscle (2-VM control) over the Model Context
Protocol so any external Brain (Claude Desktop, Claude Code, ...) can drive the
Kali + Windows VMs. The server never interprets: each tool runs on a VM and
returns raw output, exactly like the in-process library.

Run:
    DUALVM_ENV=/path/to/.env DUALVM_SSH_KEY=/path/to/key \
        python -m dualvm.mcp_server            # stdio transport

Needs the `mcp` package:  pip install "dual-vm-ai-agent[mcp]"
"""
import base64

try:
    from mcp.server.fastmcp import FastMCP, Image
except Exception as e:  # noqa: BLE001
    raise SystemExit(
        "dualvm.mcp_server needs the MCP SDK. Install it with:\n"
        "    pip install \"mcp>=1.2\"\n"
        f"(import error: {e})")

from . import shell, windows, linux, jobs

mcp = FastMCP("dualvm")


def _emit(r):
    """VMResult -> MCP content: a text block, plus an image block if the tool
    produced a picture (screenshot, rendered page)."""
    text = r.summary + (("\n\n" + r.detail) if r.detail else "")
    if r.flag:
        text += f"\n\nMATCH: {r.flag}"
    if r.image and r.image.get("data"):
        try:
            return [text, Image(data=base64.b64decode(r.image["data"]),
                                format=r.image["media_type"].split("/")[-1])]
        except Exception:  # noqa: BLE001
            pass
    return text


# --- chay lenh / doc output -------------------------------------------------
@mcp.tool()
def run_cmd(command: str, host: str = "kali", timeout: int = 20):
    """Run a one-line shell command on a VM (kali=bash, windows=cmd.exe), in the
    samples/ working dir, and return its raw output."""
    return _emit(shell.run_cmd(command, host=host, timeout=timeout))


@mcp.tool()
def run_script(script: str, host: str = "kali", kind: str = None,
               timeout: int = 20, python: str = None):
    """Write a multi-line script to the VM and run it (bash/python3 on kali;
    powershell/cmd on windows). Returns combined output."""
    return _emit(shell.run_script(script, host=host, kind=kind, timeout=timeout,
                                  python=python))


@mcp.tool()
def read_file(path: str, offset: int = 0, length: int = 4000, host: str = "kali"):
    """Read a byte slice of a file that lives on a VM (pages large files)."""
    return _emit(shell.read_file(path, offset=offset, length=length, host=host))


# --- Windows desktop: doc man hinh / chup man hinh / chay GUI ---------------
@mcp.tool()
def win_windows(match: str = None, timeout: int = 40):
    """List the open windows on the Windows VM's interactive desktop as text
    (process, title, UI-Automation class/control type). Pass `match` to dump the
    inner text of one window."""
    return _emit(windows.win_windows(match=match, timeout=timeout))


@mcp.tool()
def win_screenshot(timeout: int = 40):
    """Capture the Windows VM's interactive desktop as an image."""
    return _emit(windows.win_screenshot(timeout=timeout))


@mcp.tool()
def win_gui_run(program: str, args: str = "", wait: int = 6, kill: bool = True,
                screenshot: bool = True, timeout: int = 60):
    """Run a program on the Windows desktop, wait, then report windows + a
    screenshot."""
    return _emit(windows.win_gui_run(program, args=args, wait=wait, kill=kill,
                                     screenshot=screenshot, timeout=timeout))


@mcp.tool()
def win_frida(script: str, program: str = None, args: str = "", pid: int = None,
              wait: int = 8, kill: bool = True, desktop: bool = False,
              max_lines: int = 800, timeout: int = 90):
    """Instrument a Windows process with a Frida JavaScript script (spawn by
    `program` or attach by `pid`); returns every send()/log() line."""
    return _emit(windows.win_frida(script, program=program, args=args, pid=pid,
                                   wait=wait, kill=kill, desktop=desktop,
                                   max_lines=max_lines, timeout=timeout))


@mcp.tool()
def win_ui_tree(match: str = None, limit: int = 250, timeout: int = 45):
    """Dump the control tree (UI Automation + Win32) of a Windows window."""
    return _emit(windows.win_ui_tree(match=match, limit=limit, timeout=timeout))


@mcp.tool()
def win_ui_click(hwnd: int = None, target: str = None, window: str = None,
                 x: int = None, y: int = None, after: int = 1, timeout: int = 45):
    """Click a control (by handle, name, or coordinates) on the Windows desktop."""
    return _emit(windows.win_ui_click(hwnd=hwnd, target=target, window=window,
                                      x=x, y=y, after=after, timeout=timeout))


@mcp.tool()
def win_ui_type(text: str, hwnd: int = None, target: str = None, window: str = None,
                enter: bool = False, sendkeys: bool = False, after: int = 1,
                timeout: int = 45):
    """Type text into a control / the focused window on the Windows desktop."""
    return _emit(windows.win_ui_type(text, hwnd=hwnd, target=target, window=window,
                                     enter=enter, sendkeys=sendkeys, after=after,
                                     timeout=timeout))


@mcp.tool()
def win_key(keys: str, window: str = None, after: int = 1, timeout: int = 45):
    """Send keystrokes (SendKeys syntax) to a window on the Windows desktop."""
    return _emit(windows.win_key(keys, window=window, after=after, timeout=timeout))


@mcp.tool()
def win_ui_seq(clicks: str, window: str = None, pause: int = 250, after: int = 2,
               limit: int = 250, timeout: int = 120):
    """Click a sequence of named controls in one pass on the Windows desktop."""
    return _emit(windows.win_ui_seq(clicks, window=window, pause=pause, after=after,
                                    limit=limit, timeout=timeout))


# --- Linux: GUI headless / debugger -----------------------------------------
@mcp.tool()
def linux_gui_run(command: str, wait: int = 5, screen: str = "1280x800x24",
                  timeout: int = 60):
    """Run a GUI program on Kali under Xvfb and return a screenshot."""
    return _emit(linux.linux_gui_run(command, wait=wait, screen=screen,
                                     timeout=timeout))


@mcp.tool()
def linux_gdb(prog: str, script: str, args: str = "", gui: bool = True,
              screen: str = "1280x800x24", max_lines: int = 800, timeout: int = 120):
    """Run a gdb-python script against an ELF on Kali (ASLR off, PIE base fixed);
    returns whatever the script log()s / prints."""
    return _emit(linux.linux_gdb(prog, script, args=args, gui=gui, screen=screen,
                                 max_lines=max_lines, timeout=timeout))


# --- viec nen ----------------------------------------------------------------
@mcp.tool()
def job_start(name: str, command: str = None, script: str = None,
              host: str = "kali", python: str = None, truncated: bool = False):
    """Start a background job on a VM and return immediately with its pid."""
    return _emit(jobs.job_start(name, command=command, script=script, host=host,
                                python=python, truncated=truncated))


@mcp.tool()
def job_poll(name: str = None, tail: int = 40, grep: str = None,
             stop: bool = False, host: str = "kali"):
    """Check / tail / stop a background job (omit `name` to list all jobs)."""
    return _emit(jobs.job_poll(name=name, tail=tail, grep=grep, stop=stop, host=host))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
