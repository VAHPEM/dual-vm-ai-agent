# -*- coding: utf-8 -*-
"""beacon.evalkit — measure before believing (`beacon eval`).

Runs every task of a suite under several configurations and records whether it
was solved, what it cost and how the supervisor behaved:

  baseline     plain headless Claude Code: one session, no beacon at all
  gate         beacon hooks + ledger, lighthouse off (a stall ends the run)
  gate_lh      beacon + lighthouse (Claude API, the run's lighthouse_model)
  gate_lh_alt  beacon + lighthouse on another Claude model (--alt-lighthouse-model)

Every run gets its own copy of the task files and its own ledger; web tools are
denied in every configuration (old challenges have public write-ups). Success is
decided by the task's verify command, never by the model: the ledger's verified
candidate, or else the answer the worker wrote to ANSWER.txt.

Suite (JSON):
  {"tasks": [{"id": "fo13-5", "title": "Recover the flag from challenge 5",
              "files": "/path/to/challenge5", "verify": "python3 check.py",
              "notes": "optional context for the worker"}]}

Results go to <out>/results.jsonl (one line per run; a re-run skips runs already
recorded, so an interrupted evaluation resumes) and <out>/summary.md.
"""
import json
import os
import random
import re
import shutil
import time

from .ledger import Ledger, run_verify

CONFIGS = ("baseline", "gate", "gate_lh", "gate_lh_alt")
ANSWER_FILE = "ANSWER.txt"
BASELINE_PROMPT = ("Solve the task described in TASK.md in this directory. When you have the final answer, "
                   f"write it alone to {ANSWER_FILE} in this directory.")


def load_suite(path):
    with open(path, encoding="utf-8") as fh:
        suite = json.load(fh)
    tasks = suite.get("tasks") if isinstance(suite, dict) else None
    if not tasks:
        raise ValueError("suite needs a non-empty 'tasks' list")
    seen = set()
    for t in tasks:
        tid = str(t.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tid):
            raise ValueError(f"task id {tid!r}: use letters, digits, '.', '_' or '-' (it becomes a directory name)")
        for key in ("id", "title", "verify"):
            if not str(t.get(key) or "").strip():
                raise ValueError(f"task {t.get('id', '?')} needs '{key}'")
        if t["id"] in seen:
            raise ValueError(f"duplicate task id {t['id']}")
        seen.add(t["id"])
        if t.get("files") and not os.path.isdir(os.path.expanduser(t["files"])):
            raise ValueError(f"task {t['id']}: files directory not found: {t['files']}")
    return tasks


def config_overrides(name, alt_model=None):
    if name == "gate":
        return {"max_lighthouse_calls": 0}
    if name == "gate_lh_alt":
        if not alt_model:
            raise ValueError("gate_lh_alt needs --alt-lighthouse-model (a Claude model id)")
        return {"lighthouse_model": alt_model}
    return {}


def prepare(task, config, rep, out, python, mcp_python, alt_model=None, base_config=None):
    from .project import setup_project
    root = os.path.join(out, task["id"], config, f"rep{rep}")
    if os.path.exists(root):
        shutil.rmtree(root)       # a half-finished earlier attempt of this exact run
    proj = os.path.join(root, "proj")
    if task.get("files"):
        shutil.copytree(os.path.expanduser(task["files"]), proj)
    else:
        os.makedirs(proj)
    with open(os.path.join(proj, "TASK.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {task['title']}\n\n{task.get('notes', '')}\n\n"
                 f"When you have the final answer, also write it alone to {ANSWER_FILE} in this directory.\n")
    os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
    settings = {"permissions": {"deny": ["WebSearch", "WebFetch"]}}
    with open(os.path.join(proj, ".claude", "settings.local.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    paths = {"root": root, "proj": proj, "run": None}
    if config != "baseline":
        cfg = dict(base_config or {})
        cfg.update({"no_web": True})
        cfg.update(config_overrides(config, alt_model))
        L = Ledger.create(os.path.join(root, "run"), f"eval {task['id']} {config} rep{rep}",
                          [{"title": task["title"], "rationale": task.get("notes", ""),
                            "verify_cmd": task["verify"]}], config=cfg, workdir=proj)
        paths["run"] = L.dir
        L.close()
        setup_project(proj, python, mcp_python)
    return paths


def _answer(proj):
    try:
        with open(os.path.join(proj, ANSWER_FILE), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def measure(task, config, rep, paths, outcome, seconds):
    """Collect the metrics of one finished run."""
    row = {"task": task["id"], "config": config, "rep": rep, "seconds": round(seconds, 1),
           "cost_usd": round(float(outcome.get("cost_usd") or 0.0), 4),
           "sessions": outcome.get("sessions", 1), "stop": outcome.get("why", ""),
           "solved": False, "solved_via": "", "forced": 0, "lighthouse_calls": 0,
           "lighthouse_added": 0, "lighthouse_nodes_with_evidence": 0, "rejected_candidates": 0}
    if paths["run"]:
        L = Ledger.open(paths["run"])
        try:
            g = L.gate_state()
            row.update(forced=g["forced"], lighthouse_calls=g["lighthouse_calls"])
            lh_nodes = [n for n in L.nodes() if n["source"] == "lighthouse"]
            row["lighthouse_added"] = len(lh_nodes)
            row["lighthouse_nodes_with_evidence"] = sum(1 for n in lh_nodes if L.evidence(n["id"], unique_only=True))
            row["rejected_candidates"] = sum(1 for e in L.events(etype="verify")
                                             if not json.loads(e["payload"]).get("accepted"))
            row["integrity"] = L.verify_integrity()[0]
            if L.node("P1")["status"] == "done":
                row.update(solved=True, solved_via="ledger")
        finally:
            L.close()
    if not row["solved"]:
        ans = _answer(paths["proj"])
        if ans:
            ok, _ = run_verify(task["verify"], ans, paths["proj"])
            if ok:
                row.update(solved=True, solved_via="answer_file")
    return row


def plan(tasks, configs, reps, seed=0):
    """Interleave configurations so time-of-day effects (rate limits, load) do not
    land on one configuration."""
    rng = random.Random(seed)
    order = []
    for rep in range(1, reps + 1):
        for t in tasks:
            cs = list(configs)
            rng.shuffle(cs)
            order += [(t, c, rep) for c in cs]
    return order


def done_keys(out):
    keys = set()
    try:
        with open(os.path.join(out, "results.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    keys.add((r["task"], r["config"], r["rep"]))
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return keys


def summarize(out):
    rows = []
    try:
        with open(os.path.join(out, "results.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
    except OSError:
        pass
    by = {}
    for r in rows:
        by.setdefault(r["config"], []).append(r)
    lines = ["| config | runs | solved | solve rate | mean cost $ | mean minutes | mean forced | "
             "lighthouse calls | lighthouse nodes used | rejected candidates |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for c in [c for c in CONFIGS if c in by] + sorted(set(by) - set(CONFIGS)):
        rs = by[c]
        n = len(rs)
        solved = sum(r["solved"] for r in rs)
        lines.append(
            f"| {c} | {n} | {solved} | {solved / n:.0%} | {sum(r['cost_usd'] for r in rs) / n:.2f} | "
            f"{sum(r['seconds'] for r in rs) / n / 60:.1f} | {sum(r['forced'] for r in rs) / n:.1f} | "
            f"{sum(r['lighthouse_calls'] for r in rs)} | {sum(r['lighthouse_nodes_with_evidence'] for r in rs)} | "
            f"{sum(r['rejected_candidates'] for r in rs)} |")
    per_task = {}
    for r in rows:
        per_task.setdefault(r["task"], {}).setdefault(r["config"], []).append(r["solved"])
    lines += ["", "| task | " + " | ".join(c for c in CONFIGS if c in by) + " |",
              "|---|" + "---|" * len([c for c in CONFIGS if c in by])]
    for t, cs in sorted(per_task.items()):
        cells = []
        for c in [c for c in CONFIGS if c in by]:
            v = cs.get(c, [])
            cells.append(f"{sum(v)}/{len(v)}" if v else "-")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    lines += ["", f"{len(rows)} runs. Small samples: read differences between configurations as hints, "
              "not results, until the same direction repeats across tasks and repetitions."]
    text = "\n".join(lines)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return text


def run_eval(tasks, out, configs, reps, runner_factory, python, mcp_python, alt_model=None,
             watch_kwargs=None, base_config=None, seed=0, clock=time.time, log=print):
    from .watchdog import watch
    os.makedirs(out, exist_ok=True)
    for c in configs:
        if c not in CONFIGS:
            raise ValueError(f"unknown configuration {c!r}; choose from {CONFIGS}")
        config_overrides(c, alt_model)       # fail early on a missing alt model
    skip = done_keys(out)
    for task, config, rep in plan(tasks, configs, reps, seed):
        if (task["id"], config, rep) in skip:
            continue
        log(f"[beacon eval] {task['id']} / {config} / rep{rep}")
        paths = prepare(task, config, rep, out, python, mcp_python, alt_model, base_config)
        t0 = clock()
        if config == "baseline":
            runner = runner_factory(cwd=paths["proj"], run=paths["root"], beacon_env=False)
            res = runner.run(BASELINE_PROMPT)
            outcome = {"why": res.error_kind or "ended", "cost_usd": res.cost_usd, "sessions": 1}
        else:
            runner = runner_factory(cwd=paths["proj"], run=paths["run"], beacon_env=True)
            outcome = watch(paths["run"], runner, log=lambda *_: None, **(watch_kwargs or {}))
        row = measure(task, config, rep, paths, outcome, clock() - t0)
        with open(os.path.join(out, "results.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        log(f"[beacon eval]   solved={row['solved']} ({row['solved_via'] or '-'}) cost=${row['cost_usd']} "
            f"stop={row['stop']}")
    return summarize(out)
