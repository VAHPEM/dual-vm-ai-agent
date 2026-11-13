# -*- coding: utf-8 -*-
"""beacon.doctor — check a machine and a project before a run (`beacon doctor`).

Each check prints OK / WARN / FAIL with what to do. Exit code 1 if anything FAILs.
Nothing is changed and no secret is printed.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from .project import HOOK_EVENTS, MARK_BEGIN, REPO

MIN_CLAUDE = (2, 1, 78)          # StopFailure hook event
PROMPTS_FLAG = (2, 1, 259)       # --permission-prompts none (watchdog --no-prompts)


class Report:
    def __init__(self):
        self.lines, self.failed = [], False

    def ok(self, msg):
        self.lines.append(f"[OK]   {msg}")

    def warn(self, msg):
        self.lines.append(f"[WARN] {msg}")

    def info(self, msg):
        self.lines.append(f"[INFO] {msg}")

    def fail(self, msg):
        self.failed = True
        self.lines.append(f"[FAIL] {msg}")

    def text(self):
        return "\n".join(self.lines)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, None
    except (ValueError, OSError) as e:
        return None, str(e)


def _beacon_hooks(settings):
    """event -> list of beacon hook commands in a settings dict."""
    out = {}
    for event, arg, _ in HOOK_EVENTS:
        for g in ((settings or {}).get("hooks", {}) or {}).get(event, []) or []:
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
                if f"beacon.hooks {arg}" in cmd:
                    out.setdefault(event, []).append(cmd)
    return out


def _python_of(cmd):
    m = re.search(r'"([^"]+)"\s+-m\s+beacon\.hooks', cmd) or re.search(r"(\S+)\s+-m\s+beacon\.hooks", cmd)
    return m.group(1) if m else None


def _run(argv, env=None, timeout=20):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def check_python(rep):
    v = sys.version_info
    (rep.ok if v >= (3, 10) else rep.fail)(f"python {v.major}.{v.minor}.{v.micro} at {sys.executable}"
                                           + ("" if v >= (3, 10) else " (beacon needs >= 3.10)"))


def check_claude(rep, claude=None):
    exe = claude or shutil.which("claude")
    if not exe:
        rep.warn("claude CLI not found on PATH (needed on the machine that runs the worker)")
        return
    code, out = _run([exe, "--version"])
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    if code == -1:
        rep.fail(f"cannot run {exe}: {out[:200]}")
        return
    if code != 0 or not m:
        # Some wrapped builds only accept `-p` (all the watchdog uses): unknown version, not a failure.
        rep.warn(f"could not read the claude version from `{exe} --version`: {out[:200]}")
        return
    ver = tuple(int(x) for x in m.groups())
    if ver < MIN_CLAUDE:
        rep.fail(f"claude {'.'.join(map(str, ver))}: needs >= {'.'.join(map(str, MIN_CLAUDE))} (StopFailure hook)")
    elif ver < PROMPTS_FLAG:
        rep.warn(f"claude {'.'.join(map(str, ver))}: `beacon watch --no-prompts` needs >= "
                 f"{'.'.join(map(str, PROMPTS_FLAG))}")
    else:
        rep.ok(f"claude {'.'.join(map(str, ver))} at {exe}")
    rep.warn("Claude Code runs user hooks only after it has read your organization's settings at startup "
             "(measured on 2.1.322). If a session reports the hooks off, `beacon watch` stops with "
             "hooks_disabled; check the network to api.anthropic.com and start again.")


def check_run(rep, run):
    from .ledger import Ledger, LedgerError
    try:
        L = Ledger.open(run)
    except LedgerError as e:
        rep.fail(f"run: {e}")
        return
    try:
        ok, why = L.verify_integrity()
        (rep.ok if ok else rep.fail)(f"run ledger {L.path}: integrity {why}")
        probs = L.problems()
        open_n = len(L.open_problems())
        rep.ok(f"run: {len(probs)} problem(s), {open_n} open; lighthouse model {L.config()['lighthouse_model']}")
        mode = L.config().get("permission_mode") or ""
        if mode == "bypassPermissions":
            rep.warn("run permission_mode is bypassPermissions: `beacon claude` / `beacon watch` start the worker "
                     "with no permission prompts on this machine (Claude Code documents the mode for isolated "
                     "containers and VMs; it gives no protection against prompt injection). Deny rules still block.")
        else:
            rep.ok(f"run permission_mode: {mode or 'Claude Code default'}")
        if not any(p.get("verify_cmd") for p in probs):
            rep.warn("no problem has a verify command: every candidate will wait for `beacon verify`")
    finally:
        L.close()


def check_project(rep, project):
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        rep.fail(f"project {project} does not exist")
        return
    found, pythons = {}, set()
    for name in ("settings.json", "settings.local.json"):
        path = os.path.join(project, ".claude", name)
        data, err = _read_json(path)
        if err:
            rep.fail(f".claude/{name} is not valid JSON: {err}")
            continue
        for event, cmds in _beacon_hooks(data).items():
            for c in cmds:
                found.setdefault(event, []).append(name)
                py = _python_of(c)
                if py:
                    pythons.add(py)
        if data and "mcp__beacon" in ((data.get("permissions") or {}).get("allow") or []):
            found.setdefault("_allow", []).append(name)
    for event, _, _ in HOOK_EVENTS:
        where = found.get(event, [])
        if not where:
            rep.fail(f"hook {event} is not installed (run `beacon setup-project {project}`)")
        elif len(where) > 1:
            rep.fail(f"hook {event} is installed {len(where)} times ({', '.join(where)}): it would run twice")
        else:
            rep.ok(f"hook {event} in .claude/{where[0]}")
    wrapper = os.path.join(project, ".claude", "beacon")
    (rep.ok if os.access(wrapper, os.X_OK) else rep.warn)(
        ".claude/beacon (in-chat `! .claude/beacon log`)" + ("" if os.access(wrapper, os.X_OK) else
                                                            f": missing, run `beacon setup-project {project}`"))
    for py in sorted(pythons):
        if not os.access(py, os.X_OK):
            rep.fail(f"hook python {py} is not executable")
    if not os.path.isdir(os.path.join(REPO, "beacon")):
        rep.fail(f"beacon package not found under {REPO}")
    (rep.ok if found.get("_allow") else rep.warn)(
        "mcp__beacon is pre-approved" if found.get("_allow") else
        "mcp__beacon is not in permissions.allow: headless runs will deny the beacon tools")
    mcp, err = _read_json(os.path.join(project, ".mcp.json"))
    entry = ((mcp or {}).get("mcpServers") or {}).get("beacon")
    if err:
        rep.fail(f".mcp.json is not valid JSON: {err}")
    elif not entry:
        rep.fail("no beacon server in .mcp.json")
    else:
        py = entry.get("command", "")
        env = dict(os.environ, PYTHONPATH=REPO)
        code, out = _run([py, "-c", "import mcp, beacon.mcp_server"], env=env)
        last = (out.strip().splitlines() or [""])[-1][:300]
        (rep.ok if code == 0 else rep.fail)(
            f"MCP server python {py} imports mcp and beacon.mcp_server" if code == 0 else
            f"MCP server python {py} cannot import mcp/beacon.mcp_server: {last}")
        if entry.get("env", {}).get("BEACON_RUN") != "${BEACON_RUN}":
            rep.warn(".mcp.json beacon server does not pass BEACON_RUN through")
        from .decider import key_reaches_mcp
        if not key_reaches_mcp(entry):
            rep.warn(".mcp.json beacon server does not pass an API key variable through: the decider (Opus at "
                     f"branches) cannot run there. Re-run `beacon setup-project {project}`")
    try:
        with open(os.path.join(project, "CLAUDE.md"), encoding="utf-8") as fh:
            has_rules = MARK_BEGIN in fh.read()
    except OSError:
        has_rules = False
    (rep.ok if has_rules else rep.warn)("worker rules present in CLAUDE.md" if has_rules else
                                        "worker rules (beacon/WORKER.md) not in the project's CLAUDE.md")


def check_keys(rep):
    from .lighthouse import ca_source, load_key
    rep.info("the key checks below read THIS shell's environment; Claude Code's hooks and beacon MCP server get "
             "the environment of the shell that starts `claude`")
    src = ca_source()[0]
    (rep.warn if src.startswith("none found") else rep.ok)(f"TLS certificates for the API: {src}")
    beacon_key = bool(os.environ.get("BEACON_API_KEY", "").strip() or os.environ.get("BEACON_API_KEY_FILE", "").strip())
    if load_key() and beacon_key:
        rep.ok("BEACON_API_KEY / BEACON_API_KEY_FILE found (not printed): lighthouse, classifier and decider")
    elif load_key():
        rep.warn("only ANTHROPIC_API_KEY is set: the hooks (lighthouse, classifier, follow-ups) and the beacon MCP "
                 "server (decider) get it only while Claude Code does not scrub subprocess credentials "
                 "(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1, CI, cloud sessions remove it). Set BEACON_API_KEY: it works "
                 "in every case and keeps Claude Code itself on your subscription instead of API billing")
    else:
        rep.warn("no BEACON_API_KEY / ANTHROPIC_API_KEY / BEACON_API_KEY_FILE: every Opus part is off -- a stall "
                 "goes straight to you (no lighthouse), a branch keeps the worker's order (no decider), a question "
                 "left as prose is sorted by keywords, and a list of unfinished work is not read (the worker is "
                 "told to record it itself)")


def check_dualvm(rep):
    env = dict(os.environ, PYTHONPATH=REPO)
    code, out = _run([sys.executable, "-c", "import dualvm.shell"], env=env)
    last = (out.strip().splitlines() or [""])[-1][:200]
    (rep.ok if code == 0 else rep.warn)("dualvm importable (vm_check can reach the VMs)" if code == 0 else
                                        f"dualvm not importable here; vm_check will fail: {last}")


def doctor(run=None, project=None, claude=None):
    rep = Report()
    check_python(rep)
    check_claude(rep, claude)
    if run:
        check_run(rep, run)
    if project:
        check_project(rep, project)
    check_keys(rep)
    check_dualvm(rep)
    return rep
