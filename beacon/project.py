# -*- coding: utf-8 -*-
"""beacon.project — wire a worker project to beacon (`beacon setup-project DIR`).

Writes, inside DIR only, merging with whatever is already there:
  .claude/settings.local.json   the five hooks + an allow rule for the beacon MCP tools
                                (settings.json instead with --shared; the hook commands
                                carry absolute paths of this machine, hence "local")
  .mcp.json                     the beacon MCP server entry
  CLAUDE.md                     the worker rules (beacon/WORKER.md) between markers,
                                replaced in place on a re-run

Existing files are copied to <file>.beacon-bak once (an earlier backup is kept).
Nothing is removed from an existing file: other hooks, servers, rules and text stay.
"""
import json
import os
import shutil

from .shellrun import posix_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_EVENTS = (("Stop", "stop", 300), ("SessionStart", "session-start", 30),
               ("PreToolUse", "pre-tool-use", 10), ("PostToolUse", "post-tool-use", 10),
               ("StopFailure", "stop-failure", 30), ("UserPromptSubmit", "user-prompt-submit", 30))
# Deny rules block in every permission mode, bypassPermissions included, and they do not
# depend on the hooks running: a second line of defence for the supervisor's own files.
# (Edit rules cover every built-in file-editing tool.)
PROTECT_DENY = ("Edit(**/ledger.db*)", "Bash(sqlite3 *)", "Edit(**/.claude/settings.json)",
                "Edit(**/.claude/settings.local.json)", "Edit(**/.mcp.json)", "Edit(**/.claude/beacon)") + tuple(
    f"Bash(.claude/beacon {c} *)" for c in ("answer", "verify", "reopen", "config", "lighthouse", "init",
                                            "setup-project"))
WRAPPER = """#!/bin/sh
# beacon, for `! .claude/beacon ...` in the Claude Code chat (and any terminal in this project).
# Written by `beacon setup-project`. The commands that act as the human (answer, verify, ...) need a terminal.
PYTHONPATH="{repo}${{PYTHONPATH:+{sep}$PYTHONPATH}}" exec "{python}" -m beacon "$@"
"""
MARK_BEGIN = "<!-- beacon:worker-rules:begin -->"
MARK_END = "<!-- beacon:worker-rules:end -->"


def hook_command(python, arg, repo=REPO):
    # Claude Code runs hook commands with sh (POSIX) or Git Bash (Windows): forward slashes, or bash eats the
    # backslashes of a Windows path and the hook silently never runs.
    return f'PYTHONPATH="{posix_path(repo)}" "{posix_path(python)}" -m beacon.hooks {arg}'


def beacon_events(settings):
    """Events for which a settings dict already runs a beacon hook."""
    out = set()
    for event, arg, _ in HOOK_EVENTS:
        for g in ((settings or {}).get("hooks", {}) or {}).get(event, []) or []:
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                if isinstance(h, dict) and f"beacon.hooks {arg}" in str(h.get("command", "")):
                    out.add(event)
    return out


def merge_settings(settings, python, repo=REPO, allow=("mcp__beacon",), skip_events=(), deny=PROTECT_DENY):
    """Return (new_settings, changes). Replaces an existing beacon hook command for
    the same event (paths may have moved) instead of adding a second one. Events in
    skip_events already run beacon from the other settings file and are left alone
    (Claude Code merges both files: a second copy would run every hook twice)."""
    s = json.loads(json.dumps(settings or {}))
    changes = []
    hooks = s.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' is not an object")
    for event, arg, timeout in HOOK_EVENTS:
        if event in skip_events:
            changes.append(f"left {event} alone: the other settings file already runs beacon for it")
            continue
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings hooks.{event} is not a list")
        cmd = hook_command(python, arg, repo)
        found = False
        for g in groups:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                if isinstance(h, dict) and f"beacon.hooks {arg}" in str(h.get("command", "")):
                    found = True
                    if h.get("command") != cmd or h.get("timeout") != timeout:
                        h.update({"type": "command", "command": cmd, "timeout": timeout})
                        changes.append(f"updated {event} hook")
        if not found:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
            changes.append(f"added {event} hook")
    perms = s.setdefault("permissions", {})
    allow_list = perms.setdefault("allow", []) if isinstance(perms, dict) else None
    if not isinstance(allow_list, list):
        raise ValueError("settings permissions.allow is not a list")
    for rule in allow:
        if rule not in allow_list:
            allow_list.append(rule)
            changes.append(f"allowed {rule}")
    deny_list = perms.setdefault("deny", [])
    if not isinstance(deny_list, list):
        raise ValueError("settings permissions.deny is not a list")
    for rule in deny:
        if rule not in deny_list:
            deny_list.append(rule)
            changes.append(f"denied {rule}")
    return s, changes


def merge_mcp(cfg, mcp_python, repo=REPO):
    c = json.loads(json.dumps(cfg or {}))
    servers = c.setdefault("mcpServers", {})
    # The decider calls the Claude API from the MCP server. An MCP server inherits Claude Code's
    # environment and `${VAR:-}` expands, but ANTHROPIC_API_KEY is removed whenever Claude Code scrubs
    # subprocess credentials (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1, CI, cloud sessions; measured on 2.1.322:
    # blank in a cloud sandbox, passed through on plain Linux). So the decider's key is BEACON_API_KEY (or
    # a file named by BEACON_API_KEY_FILE); they are named here explicitly for clients that do not pass the
    # environment on.
    want = {"command": mcp_python, "args": ["-m", "beacon.mcp_server"],
            "env": {"PYTHONPATH": repo, "BEACON_RUN": "${BEACON_RUN}", "BEACON_API_KEY": "${BEACON_API_KEY:-}",
                    "BEACON_API_KEY_FILE": "${BEACON_API_KEY_FILE:-}",
                    "BEACON_API_URL": "${BEACON_API_URL:-https://api.anthropic.com/v1/messages}"}}
    if servers.get("beacon") == want:
        return c, []
    change = "updated beacon MCP server" if "beacon" in servers else "added beacon MCP server"
    servers["beacon"] = want
    return c, [change]


def merge_claude_md(text, rules):
    block = f"{MARK_BEGIN}\n{rules.strip()}\n{MARK_END}"
    text = text or ""
    if MARK_BEGIN in text and MARK_END in text and text.index(MARK_BEGIN) < text.index(MARK_END):
        head, rest = text.split(MARK_BEGIN, 1)
        _, tail = rest.split(MARK_END, 1)
        new = head + block + tail
        return new, ([] if new == text else ["updated worker rules in CLAUDE.md"])
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block + "\n", ["added worker rules to CLAUDE.md"]


def _read_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    obj = json.loads(text)        # a malformed file is an error for the human, not something to overwrite
    if not isinstance(obj, dict):
        raise ValueError(f"{path} is not a JSON object")
    return obj


def _write(path, content, dry_run):
    if dry_run:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not os.path.exists(path + ".beacon-bak"):
        shutil.copy2(path, path + ".beacon-bak")
    tmp = path + ".beacon-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def setup_project(project, python, mcp_python, shared=False, allow_labhand=False, dry_run=False,
                  quiet_shell=False, allow_dualvm=False):
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        raise FileNotFoundError(f"no such project directory: {project}")
    changes = []
    # The lab's MCP server is registered as "labhand" now, "dualvm" in configs from before the rename:
    # pre-approving the lab means both prefixes. allow_dualvm is the old name of allow_labhand.
    allow = ("mcp__beacon", "mcp__labhand", "mcp__dualvm") if (allow_labhand or allow_dualvm) else ("mcp__beacon",)

    sp = os.path.join(project, ".claude", "settings.json" if shared else "settings.local.json")
    other = os.path.join(project, ".claude", "settings.local.json" if shared else "settings.json")
    new, ch = merge_settings(_read_json(sp), python, allow=allow, skip_events=beacon_events(_read_json(other)))
    if quiet_shell and new.get("respondToBashCommands") is not False:
        new["respondToBashCommands"] = False      # `! cmd` output lands in the chat without a model reply
        ch.append("set respondToBashCommands=false (shell-mode output gets no automatic reply)")
    ch_real = [c for c in ch if not c.startswith("left ")]
    notes = [c for c in ch if c.startswith("left ")]
    changes += [f"{os.path.relpath(sp, project)}: {c}" for c in notes]
    ch = ch_real
    if ch:
        _write(sp, json.dumps(new, indent=2) + "\n", dry_run)
        changes += [f"{os.path.relpath(sp, project)}: {c}" for c in ch]

    mp = os.path.join(project, ".mcp.json")
    new, ch = merge_mcp(_read_json(mp), mcp_python)
    if ch:
        _write(mp, json.dumps(new, indent=2) + "\n", dry_run)
        changes += [f".mcp.json: {c}" for c in ch]

    wp = os.path.join(project, ".claude", "beacon")
    # the wrapper runs under sh / Git Bash; the separator is the one Python on this OS splits PYTHONPATH on
    wrapper = WRAPPER.format(repo=posix_path(REPO), python=posix_path(python), sep=os.pathsep)
    try:
        with open(wp, encoding="utf-8") as fh:
            same = fh.read() == wrapper
    except OSError:
        same = False
    if not same:
        if not dry_run:
            _write(wp, wrapper, dry_run)
            os.chmod(wp, 0o755)
        changes.append(".claude/beacon: wrote the in-chat command (`! .claude/beacon log`)")

    cp = os.path.join(project, "CLAUDE.md")
    with open(os.path.join(REPO, "beacon", "WORKER.md"), encoding="utf-8") as fh:
        rules = fh.read()
    old = ""
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            old = fh.read()
    new_text, ch = merge_claude_md(old, rules)
    if ch:
        _write(cp, new_text, dry_run)
        changes += [f"CLAUDE.md: {c}" for c in ch]
    return changes
