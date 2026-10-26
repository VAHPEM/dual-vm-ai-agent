# -*- coding: utf-8 -*-
"""MCP server: tool registration, behaviour annotations, result conversion, and
the real stdio wire format. Runs against whichever mcp SDK is installed (1.x or
2.x); skipped entirely when mcp is not installed. No VM is contacted: the
library calls are monkeypatched."""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import threading

import pytest

pytest.importorskip("mcp")

from dualvm import mcp_server, shell, windows  # noqa: E402
from dualvm._result import VMResult  # noqa: E402

EXPECTED_TOOLS = {
    "run_cmd", "run_script", "read_file", "job_start", "job_poll",
    "win_windows", "win_screenshot", "win_gui_run", "win_frida", "win_ui_tree",
    "win_ui_click", "win_ui_type", "win_key", "win_ui_seq",
    "linux_gui_run", "linux_gdb",
}
READ_ONLY = {"read_file", "win_windows", "win_screenshot", "win_ui_tree"}
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def _attr(obj, camel):
    """Read a protocol field by its wire (camelCase) name. mcp 1.x models expose
    camelCase attributes; mcp 2.x exposes snake_case (camelCase stays the wire alias)."""
    if hasattr(obj, camel):
        return getattr(obj, camel)
    return getattr(obj, re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower())


def _tools():
    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def _blocks(result):
    """Normalise call_tool's return across SDK versions to a list of content blocks."""
    if hasattr(result, "content"):          # mcp 2.x: CallToolResult
        return list(result.content)
    if isinstance(result, tuple):           # mcp 1.x with structured output
        return list(result[0])
    return list(result)


# --- registration + annotations ---------------------------------------------
def test_exactly_the_documented_tools_are_published():
    assert set(_tools()) == EXPECTED_TOOLS


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_every_tool_declares_all_four_hints_and_a_title(name):
    ann = _tools()[name].annotations
    assert ann is not None, f"{name} has no annotations"
    for hint in HINTS:
        assert isinstance(_attr(ann, hint), bool), f"{name}.{hint} not set"
    assert ann.title


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_hints_are_consistent(name):
    ann = _tools()[name].annotations
    assert _attr(ann, "readOnlyHint") == (name in READ_ONLY)
    if _attr(ann, "readOnlyHint"):
        assert _attr(ann, "destructiveHint") is False and _attr(ann, "idempotentHint") is True
        assert _attr(ann, "openWorldHint") is False
    else:
        assert _attr(ann, "destructiveHint") is True   # anything that acts can change the VM


def test_command_runners_are_open_world():
    tools = _tools()
    for name in ("run_cmd", "run_script", "job_start", "win_gui_run", "linux_gui_run"):
        assert _attr(tools[name].annotations, "openWorldHint") is True


def test_job_poll_is_idempotent_and_closed_world():
    ann = _tools()["job_poll"].annotations
    assert _attr(ann, "idempotentHint") is True and _attr(ann, "openWorldHint") is False


# --- _emit: VMResult -> MCP content -----------------------------------------
def test_emit_summary_only():
    assert mcp_server._emit(VMResult(True, "done")) == "done"


def test_emit_summary_and_detail():
    assert mcp_server._emit(VMResult(True, "head", detail="raw")) == "head\n\nraw"


def test_emit_appends_match_line():
    out = mcp_server._emit(VMResult(True, "head", flag="x@y.z"))
    assert out.endswith("\n\nMATCH: x@y.z")


def test_emit_image_becomes_second_block():
    img = {"media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    out = mcp_server._emit(VMResult(True, "shot", image=img))
    assert isinstance(out, list) and out[0] == "shot"
    assert isinstance(out[1], mcp_server.Image)


def test_emit_bad_image_falls_back_to_text():
    img = {"media_type": "image/png", "data": "!!! not base64 !!!"}
    assert mcp_server._emit(VMResult(True, "shot", image=img)) == "shot"


def test_emit_empty_image_data_is_text():
    out = mcp_server._emit(VMResult(True, "shot", image={"media_type": "image/png", "data": ""}))
    assert out == "shot"


# --- call_tool through the server (arguments pass through untouched) ---------
def test_call_tool_passes_arguments_and_returns_text(monkeypatch):
    seen = {}

    def fake_run_cmd(command, host="kali", timeout=20):
        seen.update(command=command, host=host, timeout=timeout)
        return VMResult(True, "rc=0", detail="Linux kali")

    monkeypatch.setattr(shell, "run_cmd", fake_run_cmd)
    res = asyncio.run(mcp_server.mcp.call_tool(
        "run_cmd", {"command": "uname -a", "host": "windows", "timeout": 5}))
    assert seen == {"command": "uname -a", "host": "windows", "timeout": 5}
    texts = [b.text for b in _blocks(res) if getattr(b, "type", "") == "text"]
    assert "rc=0\n\nLinux kali" in texts


def test_call_tool_screenshot_returns_image_content(monkeypatch):
    img = {"media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    monkeypatch.setattr(windows, "win_screenshot",
                        lambda timeout=40: VMResult(True, "shot", image=img))
    blocks = _blocks(asyncio.run(mcp_server.mcp.call_tool("win_screenshot", {})))
    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    assert len(images) == 1
    assert _attr(images[0], "mimeType") == "image/png"
    assert base64.b64decode(images[0].data) == PNG


def test_optional_params_accept_explicit_null(monkeypatch):
    """Clients (LLMs especially) often send `"kind": null` for an unused optional
    argument. Optional params must be typed `X | None`, or the SDK's schema
    validation rejects the call before it reaches the library."""
    seen = []
    monkeypatch.setattr(shell, "run_script",
                        lambda script, host="kali", kind=None, timeout=20, python=None:
                        (seen.append((kind, python)), VMResult(True, "ok"))[1])
    asyncio.run(mcp_server.mcp.call_tool(
        "run_script", {"script": "echo hi", "kind": None, "python": None}))
    assert seen == [(None, None)]


def test_no_optional_param_schema_rejects_null():
    for name, tool in _tools().items():
        schema = _attr(tool, "inputSchema")
        for pname, prop in schema.get("properties", {}).items():
            if prop.get("default", "<none>") is None:
                types = {p.get("type") for p in prop.get("anyOf", [prop])}
                assert "null" in types, f"{name}({pname}) defaults to None but rejects null"


# --- real stdio wire format (raw JSON-RPC, independent of the client SDK) -----
def _rpc(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _read_response(proc, want_id):
    while True:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout; stderr:\n" + proc.stderr.read())
        msg = json.loads(line)
        if msg.get("id") == want_id:
            return msg


def test_stdio_tools_list_carries_annotations():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-m", "dualvm.mcp_server"], cwd=root, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    # readline() has no timeout: if the server hangs, kill it so the test fails
    # instead of hanging the whole suite.
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "dualvm-tests", "version": "0"}}})
        init = _read_response(proc, 1)
        assert init["result"]["serverInfo"]["name"] == "dualvm"
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = {t["name"]: t for t in _read_response(proc, 2)["result"]["tools"]}
    finally:
        watchdog.cancel()
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        ann = tool.get("annotations") or {}
        for hint in HINTS:
            assert isinstance(ann.get(hint), bool), f"{name}.{hint} missing on the wire"
