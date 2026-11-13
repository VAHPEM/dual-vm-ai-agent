# -*- coding: utf-8 -*-
"""beacon CLI — the human's side of a run.

    python3 -m beacon init RUN --checklist checklist.md      create a run
    python3 -m beacon status | report | graph | check        look at it
    python3 -m beacon answer QID "text"                      answer a question
    python3 -m beacon verify P1 [--reject "why"]             accept / refute a candidate
    python3 -m beacon reopen NODE                            undo a closed/blocked node
    python3 -m beacon config [key=value ...]                 show / change budgets
    python3 -m beacon hooks-config                           settings.json + .mcp.json snippets
    python3 -m beacon lighthouse [--dry-run]                 call (or preview) the reviewer
    python3 -m beacon watch --cwd PROJECT                    run the worker unattended (watchdog)
    python3 -m beacon setup-project DIR                      wire a worker project (hooks, MCP, rules)
    python3 -m beacon log [-n 10] [-f]                       each stop: what Claude said + what beacon replied
    python3 -m beacon transcript [-n 40] [-f]                Claude Code's full session log, readable
    python3 -m beacon graph                                  the visual graph in the browser (live)
    python3 -m beacon demo [DIR] [--auto]                    a practice run: manual steps, or a live smoke test
    python3 -m beacon calibrate-refusals                     check the refusal heuristic on real output
    python3 -m beacon eval SUITE --out DIR                   baseline vs gate vs gate+lighthouse
    python3 -m beacon doctor [--project DIR] [--run RUN]     check the machine and a project first
    python3 -m beacon claude [--run RUN] [claude args]       open Claude Code supervised by the run

Commands other than init take --run RUN, default $BEACON_RUN.
"""
import argparse
import json
import os
import re
import shlex
import sys

from .ledger import DEFAULT_CONFIG, Ledger, LedgerError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_checklist(text):
    """Markdown: an optional '# Title', then top-level items '- [ ] problem'
    (or '- problem'; '- [x]' = already done). Indented lines under an item:
    'verify: <shell command>' (exit 0 = accepted; the candidate arrives in
    $BEACON_CANDIDATE), anything else is appended to its rationale."""
    title, problems, cur = None, [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^#\s+(.+)$", line)
        if m and title is None and not problems:
            title = m.group(1).strip()
            continue
        m = re.match(r"^[-*]\s+(?:\[( |x|X)\]\s+)?(.+)$", line)
        if m:
            cur = {"title": m.group(2).strip(), "status": "done" if (m.group(1) or "").lower() == "x" else "todo",
                   "rationale": ""}
            problems.append(cur)
            continue
        if cur is None or not raw[:1].isspace():
            continue
        m = re.match(r"^\s+(verify|note|rationale)\s*:\s*(.*)$", line, re.I)
        if m and m.group(1).lower() == "verify":
            cur["verify_cmd"] = m.group(2).strip()
        else:
            txt = m.group(2).strip() if m else line.strip().lstrip("-* ").strip()
            cur["rationale"] = (cur["rationale"] + "\n" + txt).strip()
    return title, problems


def load_checklist(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        obj = json.loads(text)
        return obj.get("title"), obj.get("problems", [])
    return parse_checklist(text)


def _coerce(value):
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _require_terminal(action):
    """Commands that act as the human must be typed by one. Claude Code's Bash tool runs
    commands without a terminal, so the worker cannot answer its own questions, verify its
    own candidate or change the run's config (notify_cmd is a shell command the hooks run)."""
    if not sys.stdin.isatty():
        raise LedgerError(f"`beacon {action}` acts as the human and must be run from a terminal "
                          "(stdin is not a TTY). It cannot be run by the worker or from a pipe.")


def _open(args):
    run = args.run or os.environ.get("BEACON_RUN", "").strip()
    if not run:
        raise LedgerError("no run: pass --run RUN or set BEACON_RUN")
    return Ledger.open(run)


def cmd_init(args):
    title, problems = load_checklist(args.checklist)
    cfg = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in DEFAULT_CONFIG:
            raise LedgerError(f"unknown config key {k!r}")
        cfg[k] = _coerce(v)
    L = Ledger.create(args.run, args.title or title or os.path.basename(os.path.abspath(args.run)),
                      problems, config=cfg,
                      workdir=args.workdir or os.path.dirname(os.path.abspath(args.checklist)))
    print(f"created {L.path} with {len(problems)} problem(s)")
    print(L.render_tree(evidence=False))
    print(f"\nLaunch the worker with:  BEACON_RUN={L.dir} claude")


def cmd_status(args):
    L = _open(args)
    print(f"Run: {L.title()}  ({L.dir})")
    print(L.render_tree(max_evidence=args.evidence))
    g = L.gate_state()
    print(f"\ngate: {g['stops']} stops, {g['forced']} forced, stall {g['stall']}, idle {g['idle']}, "
          f"lighthouse {g['lighthouse_calls']} calls ({g['lighthouse_dry']} dry), last {g['last_decision'] or '-'}")


def cmd_report(args):
    from .report import render_report
    print(render_report(_open(args)))


def cmd_graph(args):
    L = _open(args)
    if args.format == "board":
        from .board import render_board, write_board
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(render_board(L))
            path = os.path.abspath(args.output)
        else:
            path = write_board(L)      # <run>/graph.html: the hooks keep this one up to date
        print(f"graph: file://{path}")
        if not args.no_open:
            import webbrowser
            webbrowser.open("file://" + path)
        return
    from .report import render_html, render_mermaid
    out = render_html(L) if args.format == "html" else render_mermaid(L)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"wrote {args.output}")
    else:
        print(out)


def cmd_demo(args):
    from .demo import make_kit, run_auto
    base = args.dir or os.path.expanduser("~/beacon-demo")
    try:
        kit = make_kit(base, mcp_python=args.mcp_python, force=args.force)
        print(f"built the demo kit in {kit['base']}")
    except FileExistsError:
        b = os.path.abspath(os.path.expanduser(base))
        kit = {"base": b, "project": os.path.join(b, "project"), "run": os.path.join(b, "run"),
               "testing": os.path.join(b, "TESTING.md")}
        print(f"using the demo kit already in {b}")
        if args.auto:     # the smoke test needs an untouched run
            L = Ledger.open(kit["run"])
            try:
                used = L.gate_state()["stops"] or any(p["status"] != "todo" for p in L.problems())
            finally:
                L.close()
            if used:
                raise LedgerError(f"{b} has been used already: `beacon demo {b} --auto --force` rebuilds it")
    if not args.auto:
        print(f"\nManual test: open {kit['testing']} and follow it. In short:\n"
              f"  cd {kit['project']}\n  .claude/beacon claude --run {kit['run']}\n"
              f"Automatic live test (headless Claude Code, your login, budget-capped):\n"
              f"  {kit['project']}/.claude/beacon demo {kit['base']} --auto")
        return 0
    print("\nlive smoke test: real Claude Code, headless, in", kit["project"])
    res, _ = run_auto(kit, claude=args.claude, model=args.model, max_budget_usd=args.max_budget_usd)
    return 1 if res.failed else 0


def cmd_check(args):
    ok, why = _open(args).verify_integrity()
    print(("OK " if ok else "FAILED ") + why)
    return 0 if ok else 1


def cmd_answer(args):
    _require_terminal("answer")
    r = _open(args).answer(args.qid, args.text, actor="human")
    print(f"answered #{args.qid}" + (f"; reopened {', '.join(r['reopened'])}" if r["reopened"] else ""))


def cmd_verify(args):
    _require_terminal("verify")
    accept = args.reject is None
    L = _open(args)
    r = L.verify(args.problem, accept, args.reject if not accept else (args.note or "verified by human"),
                 actor="human") or {}
    from .chat import dropped_note
    print(f"{args.problem} " + ("accepted" if accept else "rejected") + (dropped_note(L, r.get("dropped")) if accept else ""))


def cmd_reopen(args):
    _require_terminal("reopen")
    r = _open(args).reopen(args.node, args.note or "", actor="human")
    print(f"{args.node}: {r['from']} -> {r['to']}")


def cmd_config(args):
    if args.pairs:
        _require_terminal("config")
    L = _open(args)
    for kv in args.pairs or []:
        k, _, v = kv.partition("=")
        L.set_config(k, _coerce(v), actor="human")
    print(json.dumps(L.config(), indent=2))


def cmd_hooks_config(args):
    py = args.python or sys.executable
    mpy = args.mcp_python or py

    from .project import HOOK_EVENTS, hook_command
    settings = {"hooks": {event: [{"hooks": [{"type": "command", "timeout": timeout,
                                              "command": hook_command(py, arg)}]}]
                          for event, arg, timeout in HOOK_EVENTS}}
    from .project import merge_mcp
    mcp, _ = merge_mcp({}, mpy)
    print("# Merge into <project>/.claude/settings.json (or settings.local.json):")
    print(json.dumps(settings, indent=2))
    print("\n# Merge into <project>/.mcp.json  (the MCP python needs the `mcp` package):")
    print(json.dumps(mcp, indent=2))
    print(f"\n# Add the worker rules to the project's CLAUDE.md:  see {os.path.join(REPO, 'beacon', 'WORKER.md')}")
    print("# Then launch:  BEACON_RUN=<run dir> claude")


def cmd_lighthouse(args):
    from .lighthouse import SYSTEM, build_prompt, run_lighthouse
    L = _open(args)
    if args.dry_run:
        print("=== system ===\n" + SYSTEM + "\n\n=== user ===\n" + build_prompt(L, args.task))
        return
    _require_terminal("lighthouse")
    print(json.dumps(run_lighthouse(L, task=args.task), indent=2, ensure_ascii=False, default=str))


def cmd_watch(args):
    from .watchdog import FRESH_PROMPT, CliRunner, watch
    L = _open(args)
    run = L.dir
    mode = args.permission_mode or L.config().get("permission_mode") or None
    L.close()
    runner = CliRunner(cwd=os.path.abspath(os.path.expanduser(args.cwd)), run=run, claude=args.claude,
                       permission_mode=mode, no_prompts=args.no_prompts, model=args.model,
                       max_budget_usd=args.session_budget_usd,
                       timeout_s=int(args.session_timeout_min * 60) if args.session_timeout_min else None,
                       extra_args=args.extra or [])
    if args.dry_run:
        print("cwd:", runner.cwd)
        print("env: BEACON_RUN=%s BEACON_WATCHDOG=1" % run)
        print(" ".join(shlex.quote(c) for c in runner.command(FRESH_PROMPT)))
        return 0
    r = watch(run, runner, max_sessions=args.max_sessions, max_hours=args.max_hours,
              max_cost_usd=args.max_cost_usd,
              max_wait_s=args.max_wait_hours * 3600 if args.max_wait_hours else None)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


def cmd_setup_project(args):
    from .project import setup_project
    py = args.python or sys.executable
    changes = setup_project(args.project, py, args.mcp_python or py, shared=args.shared,
                            allow_dualvm=args.allow_dualvm, dry_run=args.dry_run, quiet_shell=args.quiet_shell)
    print(("Would change:" if args.dry_run else "Changed:") if changes else "Already set up; nothing to change.")
    for c in changes:
        print("  " + c)
    if changes and not args.dry_run:
        print("Backups of files that existed: <file>.beacon-bak")
        print("Next: beacon init <run> --checklist <file>, then BEACON_RUN=<run> claude  (or beacon watch)")
    return 0


def cmd_log(args):
    from .logview import follow_log, stop_log
    L = _open(args)
    width = None if args.full else args.width
    print(stop_log(L.dir, last=args.n, width=width))
    if args.follow:
        print("\n-- following; Ctrl-C to stop --\n")
        try:
            follow_log(L.dir, width=width)
        except KeyboardInterrupt:
            pass


def cmd_transcript(args):
    from .logview import follow_transcript, pick_session, render_transcript, sessions
    L = _open(args)
    if args.list:
        for s in sessions(L.dir):
            more = f"  (+{len(s['paths']) - 1} resumed)" if len(s["paths"]) > 1 else ""
            print(f"{s['session']}  {s['kind']:10s}  {s['path']}{more}")
        return 0
    s = pick_session(L.dir, args.session)
    if s is None:
        print("no session recorded yet" + (f" matching {args.session!r}" if args.session else ""))
        return 1
    width = None if args.full else args.width
    print(f"== session {s['session']} ({s['kind']}: {s['path']})")
    print(render_transcript(s["paths"], last=args.n, width=width))
    if args.follow:
        print("\n-- following; Ctrl-C to stop --")
        try:
            follow_transcript(s["paths"][-1], width=width, start_at_end=True)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_calibrate(args):
    from .report import calibrate_refusals
    L = _open(args)
    print(calibrate_refusals(L.dir))


def cmd_eval(args):
    from .evalkit import load_suite, plan, run_eval
    from .watchdog import CliRunner
    tasks = load_suite(args.suite)
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    if args.dry_run:
        for t, c, rep in plan(tasks, configs, args.reps, args.seed):
            print(f"{t['id']} / {c} / rep{rep}")
        return 0
    py = args.python or sys.executable

    def factory(cwd, run, beacon_env):
        return CliRunner(cwd=cwd, run=run, claude=args.claude, permission_mode=args.permission_mode,
                         no_prompts=args.no_prompts, model=args.model, max_budget_usd=args.session_budget_usd,
                         timeout_s=int(args.session_timeout_min * 60), beacon_env=beacon_env)

    base = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in DEFAULT_CONFIG:
            raise LedgerError(f"unknown config key {k!r}")
        base[k] = _coerce(v)
    summary = run_eval(tasks, os.path.abspath(os.path.expanduser(args.out)), configs, args.reps, factory, py,
                       args.mcp_python or py, alt_model=args.alt_lighthouse_model,
                       watch_kwargs={"max_sessions": args.max_sessions, "max_hours": args.max_hours_per_run,
                                     "max_cost_usd": args.max_cost_per_run,
                                     "max_wait_s": args.max_wait_hours * 3600},
                       base_config=base, seed=args.seed)
    print(summary)
    return 0


def cmd_doctor(args):
    from .doctor import doctor
    run = args.run or os.environ.get("BEACON_RUN", "").strip() or None
    rep = doctor(run=run, project=args.project, claude=args.claude)
    print(rep.text())
    return 1 if rep.failed else 0


def launch_argv(run_dir, mode, claude="claude", extra=()):
    """The command and environment `beacon claude` starts."""
    argv = [claude] + (["--permission-mode", mode] if mode else []) + list(extra)
    env = dict(os.environ, BEACON_RUN=run_dir)
    env.pop("BEACON_WATCHDOG", None)      # an interactive session is never rotated
    return argv, env


def cmd_claude(args):
    L = _open(args)
    run, mode = L.dir, L.config().get("permission_mode") or ""
    L.close()
    argv, env = launch_argv(run, mode, args.claude, args.extra or [])
    if mode == "bypassPermissions":
        print("[beacon] starting Claude Code in bypassPermissions: no permission prompts at all. Deny rules "
              "(setup-project adds ones protecting the supervisor) still block.", file=sys.stderr)
    os.execvpe(argv[0], argv, env)


def build_parser():
    ap = argparse.ArgumentParser(prog="beacon", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_run(p):
        p.add_argument("--run", help="run directory (default: $BEACON_RUN)")
        return p

    p = sub.add_parser("init", help="create a run from a checklist")
    p.add_argument("run")
    p.add_argument("--checklist", required=True)
    p.add_argument("--title")
    p.add_argument("--workdir", help="cwd for verify commands (default: the checklist's directory)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="config override")
    p.set_defaults(fn=cmd_init)
    p = with_run(sub.add_parser("status"))
    p.add_argument("--evidence", type=int, default=2)
    p.set_defaults(fn=cmd_status)
    with_run(sub.add_parser("report")).set_defaults(fn=cmd_report)
    p = with_run(sub.add_parser("graph", help="the visual graph (<run>/graph.html, kept up to date) or mermaid"))
    p.add_argument("--format", choices=("board", "mermaid", "html"), default="board",
                   help="board: the interactive page (default); mermaid: text for a markdown file; "
                        "html: mermaid rendered from a CDN")
    p.add_argument("-o", "--output")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    p.set_defaults(fn=cmd_graph)
    with_run(sub.add_parser("check")).set_defaults(fn=cmd_check)
    p = sub.add_parser("demo", help="a practice run to test beacon end to end (manual steps or --auto)")
    p.add_argument("dir", nargs="?", help="where to build the kit (default ~/beacon-demo)")
    p.add_argument("--auto", action="store_true", help="run the live smoke test with the real Claude Code")
    p.add_argument("--force", action="store_true", help="rebuild the kit (a fresh ledger)")
    p.add_argument("--mcp-python", help="python with the `mcp` package for the beacon MCP server")
    p.add_argument("--claude", default="claude", help="the claude CLI")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--max-budget-usd", type=float, default=1.0, help="per Claude Code session")
    p.set_defaults(fn=cmd_demo)
    p = with_run(sub.add_parser("answer"))
    p.add_argument("qid", type=int)
    p.add_argument("text")
    p.set_defaults(fn=cmd_answer)
    p = with_run(sub.add_parser("verify"))
    p.add_argument("problem")
    p.add_argument("--reject", metavar="WHY")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_verify)
    p = with_run(sub.add_parser("reopen"))
    p.add_argument("node")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_reopen)
    p = with_run(sub.add_parser("config"))
    p.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser("hooks-config")
    p.add_argument("--python", help="python for the hooks (stdlib only; default: this interpreter)")
    p.add_argument("--mcp-python", help="python with the `mcp` package for the MCP server")
    p.set_defaults(fn=cmd_hooks_config)
    p = with_run(sub.add_parser("lighthouse"))
    p.add_argument("--task", choices=("stall", "idle", "audit_near_done"), default="stall")
    p.add_argument("--dry-run", action="store_true", help="print the prompt instead of calling the API")
    p.set_defaults(fn=cmd_lighthouse)
    p = with_run(sub.add_parser("watch", help="run the worker headless and keep the run alive"))
    p.add_argument("--cwd", required=True, help="the worker project directory (its .claude/ and .mcp.json)")
    p.add_argument("--claude", default="claude", help="path to the claude CLI")
    p.add_argument("--permission-mode", help="passed to claude (e.g. auto, acceptEdits, bypassPermissions); "
                   "default: the run's permission_mode, else Manual: unapproved tools are denied headless")
    p.add_argument("--no-prompts", action="store_true", help="pass --permission-prompts none (v2.1.259+)")
    p.add_argument("--model")
    # No time or cost limit by default: each flag below sets one only if you give it.
    p.add_argument("--session-budget-usd", type=float, help="claude --max-budget-usd per session (default: none)")
    p.add_argument("--session-timeout-min", type=float, help="kill and resume a session after this long (default: none)")
    p.add_argument("--max-sessions", type=int, default=20)
    p.add_argument("--max-hours", type=float, help="stop the run after this many hours (default: none)")
    p.add_argument("--max-cost-usd", type=float, help="stop once the estimated total reaches this (default: none)")
    p.add_argument("--max-wait-hours", type=float,
                   help="give up a rate-limit wait longer than this (default: none, wait for the announced reset)")
    p.add_argument("--extra", nargs=argparse.REMAINDER, help="further arguments for claude (put last)")
    p.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("setup-project", help="wire a worker project to beacon")
    p.add_argument("project")
    p.add_argument("--python", help="python for the hooks (stdlib only; default: this interpreter)")
    p.add_argument("--mcp-python", help="python with the `mcp` package (default: --python)")
    p.add_argument("--shared", action="store_true", help="write .claude/settings.json instead of settings.local.json")
    p.add_argument("--allow-dualvm", action="store_true",
                   help="also pre-approve every dualvm MCP tool (they act on the VMs; your call)")
    p.add_argument("--quiet-shell", action="store_true",
                   help="set respondToBashCommands=false: `! .claude/beacon log` output gets no model reply")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_setup_project)
    p = with_run(sub.add_parser("log", help="each stop: what Claude Code said and what beacon replied"))
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--width", type=int, default=600)
    p.add_argument("--full", action="store_true", help="do not shorten texts")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new stops")
    p.set_defaults(fn=cmd_log)
    p = with_run(sub.add_parser("transcript", help="Claude Code's full log of a session, readable"))
    p.add_argument("--session", help="session id (prefix); default: the latest")
    p.add_argument("--list", action="store_true", help="list the sessions of this run")
    p.add_argument("-n", type=int, help="only the last N lines")
    p.add_argument("--width", type=int, default=600)
    p.add_argument("--full", action="store_true")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing as the session goes on")
    p.set_defaults(fn=cmd_transcript)
    with_run(sub.add_parser("calibrate-refusals")).set_defaults(fn=cmd_calibrate)
    p = sub.add_parser("eval", help="run a suite under several configurations and compare")
    p.add_argument("suite")
    p.add_argument("--out", required=True)
    p.add_argument("--configs", default="baseline,gate,gate_lh",
                   help="comma list of baseline,gate,gate_lh,gate_lh_alt")
    p.add_argument("--alt-lighthouse-model", help="Claude model id for gate_lh_alt (e.g. claude-sonnet-5-5)")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="ledger config for every beacon run")
    p.add_argument("--python")
    p.add_argument("--mcp-python")
    p.add_argument("--claude", default="claude")
    p.add_argument("--permission-mode")
    p.add_argument("--no-prompts", action="store_true")
    p.add_argument("--model", help="worker model for every configuration")
    p.add_argument("--session-budget-usd", type=float)
    p.add_argument("--session-timeout-min", type=float, default=120)
    p.add_argument("--max-sessions", type=int, default=8)
    p.add_argument("--max-hours-per-run", type=float, default=3)
    p.add_argument("--max-cost-per-run", type=float)
    p.add_argument("--max-wait-hours", type=float, default=6)
    p.add_argument("--dry-run", action="store_true", help="print the run order only")
    p.set_defaults(fn=cmd_eval)
    p = with_run(sub.add_parser("claude", help="open Claude Code supervised by the run (its permission_mode)"))
    p.add_argument("--claude", default="claude", help="path to the claude CLI")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="further arguments for claude")
    p.set_defaults(fn=cmd_claude)
    p = with_run(sub.add_parser("doctor", help="check python, claude CLI, project wiring, keys, dualvm"))
    p.add_argument("--project", help="worker project directory to check")
    p.add_argument("--claude", help="path to the claude CLI (default: PATH)")
    p.set_defaults(fn=cmd_doctor)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
