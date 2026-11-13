# -*- coding: utf-8 -*-
"""beacon.demo — a small, benign practice run to try beacon end to end on your machine.

    python3 -m beacon demo [DIR]            build the kit and print the manual test steps
    python3 -m beacon demo [DIR] --auto     build it and run the live smoke test with the real
                                            Claude Code (headless, your login, a budget cap)

The kit (default ~/beacon-demo):
    DIR/project/       what Claude Code works in: three small puzzles and check.py
    DIR/run/           the beacon run (ledger, logs, graph.html)
    DIR/TESTING.md     the manual test, step by step, with what you should see
The puzzles are toys written for this test (an XOR keystream in a Python file, a file
encrypted with a passphrase only you have, a short write-up); nothing is executed except the
Python that Claude Code chooses to run on them.

The live smoke test drives one full cycle: Claude is told to solve only P1 and stop, so the
Stop gate must push it on; P2 needs the passphrase, so Claude must ask and the session must
stop for the human; the test answers as the human, resumes, and checks that P2 gets solved;
P3 has no automatic check, so it must wait for the human's verification. Each step is a row
of PASS / WARN / FAIL with the reason.
"""
import hashlib
import json
import os
import shlex
import sys
import time

from . import shellrun

FLAG1 = "flag{beacon-kept-going-1}"
FLAG2 = "flag{asked-the-human-2}"
PASSPHRASE = "kiwi-7319-lantern"
SEED1 = 0x5A
PROMPT_FIRST = ("A beacon run supervises this session. Call ledger_view first. Solve ONLY P1 in this turn, "
                "recording your steps with the beacon tools, then stop and report what you did.")
PROMPT_RESUME = ("Continue the beacon run: call ledger_view (the human answered your question; the answer is in "
                 "the beacon context) and work the remaining problems.")
DEMO_ALLOW = ["Bash(python3 *)", "Bash(python *)", "Bash(cat *)", "Bash(ls *)", "Bash(xxd *)", "Bash(od *)",
              "Bash(head *)", "Bash(file *)", "Bash(shasum *)", "Bash(sha256sum *)", "Bash(mkdir *)", "Bash(wc *)"]


def keystream1(n, x=SEED1):
    out = []
    for _ in range(n):
        x = (x * 37 + 11) & 0xFF
        out.append(x)
    return bytes(out)


def keystream2(passphrase, n):
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(passphrase.encode() + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


HIDE_PY = '''# Challenge 1 (beacon demo). This program hides a flag; it never prints it.
# Recover the flag from the code below. Do not guess and do not brute force.
import sys

BLOB = bytes.fromhex("{blob}")


def keystream(n, x={seed:#04x}):
    out = []
    for _ in range(n):
        x = (x * 37 + 11) & 0xFF
        out.append(x)
    return bytes(out)


if __name__ == "__main__":
    print("The flag is in BLOB, XORed with keystream(len(BLOB)). This program will not print it.")
    sys.exit(0)
'''
README2 = """Challenge 2 (beacon demo)

data.bin = flag XOR keystream, where keystream is the concatenation of
SHA-256(passphrase_bytes || i.to_bytes(4, "big")) for i = 0, 1, 2, ... (UTF-8 passphrase),
cut to the length of data.bin.

The passphrase is NOT in this project and cannot be derived from data.bin. Only the human
has it: ask for it (beacon ask, kind need_info) instead of guessing.
"""
CHECK_PY = '''# Checks a candidate flag (beacon demo). The candidate arrives in $BEACON_CANDIDATE.
import hashlib
import os
import sys

WANT = {want}
cand = os.environ.get("BEACON_CANDIDATE", "").strip()
ok = hashlib.sha256(cand.encode()).hexdigest() == WANT.get(sys.argv[1] if len(sys.argv) > 1 else "", "")
print("correct" if ok else "wrong")
sys.exit(0 if ok else 1)
'''
CHECKLIST = """# beacon demo
- [ ] Challenge 1: recover the flag hidden in chal1/hide.py
  verify: {py} check.py 1
  note: the flag looks like flag{{...}}; recover it from the code
- [ ] Challenge 2: decrypt chal2/data.bin
  verify: {py} check.py 2
  note: chal2/README.txt describes the cipher; the passphrase is held by the human: ask for it with ask(kind="need_info")
- [ ] Write notes/chal1.md: 3 to 5 sentences on how chal1 hides its flag and how you recovered it
  note: no automatic check; propose_done with the file path as the candidate, the human reviews it
"""


def make_kit(base, mcp_python=None, python=None, force=False):
    """Create DIR/project, DIR/run, DIR/TESTING.md. -> dict of paths."""
    from .cli import parse_checklist
    from .ledger import Ledger
    from .project import setup_project
    base = os.path.abspath(os.path.expanduser(base))
    project, run = os.path.join(base, "project"), os.path.join(base, "run")
    if os.path.exists(run) and not force:
        raise FileExistsError(f"{run} exists: use it, or pass --force to build a fresh kit in another directory")
    python = python or sys.executable
    os.makedirs(os.path.join(project, "chal1"), exist_ok=True)
    os.makedirs(os.path.join(project, "chal2"), exist_ok=True)
    blob = _xor(FLAG1.encode(), keystream1(len(FLAG1)))
    files = {
        "chal1/hide.py": HIDE_PY.format(blob=blob.hex(), seed=SEED1),
        "chal2/README.txt": README2,
        "check.py": CHECK_PY.format(want=json.dumps({"1": hashlib.sha256(FLAG1.encode()).hexdigest(),
                                                       "2": hashlib.sha256(FLAG2.encode()).hexdigest()})),
    }
    for rel, text in files.items():
        with open(os.path.join(project, rel), "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(project, "chal2", "data.bin"), "wb") as fh:
        fh.write(_xor(FLAG2.encode(), keystream2(PASSPHRASE, len(FLAG2))))
    checklist = CHECKLIST.format(py=shlex.quote(shellrun.posix_path(python)))   # verify runs under sh / Git Bash
    with open(os.path.join(base, "checklist.md"), "w", encoding="utf-8") as fh:
        fh.write(checklist)
    title, problems = parse_checklist(checklist)
    if os.path.exists(run):          # --force: a fresh ledger and no earlier answers in place
        for d in (run, os.path.join(run, "sessions"), os.path.join(project, "notes")):
            for name in (os.listdir(d) if os.path.isdir(d) else []):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    os.remove(p)
    L = Ledger.create(run, title, problems, config={"permission_mode": "acceptEdits"}, workdir=project)
    L.close()
    setup_project(project, python, mcp_python or python)
    sp = os.path.join(project, ".claude", "settings.local.json")
    with open(sp, encoding="utf-8") as fh:
        settings = json.load(fh)
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    allow.extend(a for a in DEMO_ALLOW if a not in allow)
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    testing = os.path.join(base, "TESTING.md")
    with open(testing, "w", encoding="utf-8") as fh:
        fh.write(TESTING.format(base=base, project=project, run=run, py=python, passphrase=PASSPHRASE,
                                prompt=PROMPT_FIRST))
    return {"base": base, "project": project, "run": run, "testing": testing}


TESTING = """# beacon — manual test with the demo kit

Kit: `{base}`  (project `{project}`, run `{run}`)
The passphrase for challenge 2 (only you have it; it is not in the project): **{passphrase}**

Each step says what to do and what you should see. Note what differs.

`.claude/beacon` (in the project) runs this checkout's beacon from anywhere.

## 0. Check the machine
    {project}/.claude/beacon doctor --project {project} --run {run}
Expect no [FAIL]. A [WARN] about the API key only means the lighthouse is off.

## 1. Start Claude Code under beacon
    cd {project}
    .claude/beacon claude --run {run}
Expect, at the top of the chat: `beacon ▸ run "beacon demo" supervises this session: 3/3 problem(s) open. In this chat: ...`
(If you see "Hooks not running" instead, beacon is off in this session: stop and run step 0.)

## 2. Make Claude stop early on purpose
Type:  {prompt}
Expect: Claude calls ledger_view, add_node, start_node, attach_evidence, propose_done; P1 is verified
by check.py. When Claude stops, beacon refuses: a Stop hook message, and
`beacon ▸ kept the session going (stop 1, forced 1/40); next: ... → told Claude: ...`. Claude goes on to P2.

## 3. Open the graph (any time)
Type:  beacon: graph
Expect: the browser opens graph.html; P1 shows ✓ done; it reloads as Claude works.

## 4. Answer Claude's question
P2 needs the passphrase. Expect Claude to call ask(need_info); P2's approach turns ‖ blocked; when nothing
else is workable beacon lets it stop: `beacon ▸ let the session stop: awaiting_human ... #1 ...`.
Type:  #1 {passphrase}
Expect: `beacon ▸ Recorded as the answer to question #1; ... unblocked`; Claude decrypts data.bin; P2 verified.

## 5. Verify a candidate yourself
P3 has no automatic check. Expect `P3 candidate: notes/chal1.md -> beacon: verify P3`.
Read {project}/notes/chal1.md, then type  beacon: verify P3   (or  beacon: verify P3 reject <why>  to see it reopen).

## 6. Views
beacon: status · beacon: show P1 · beacon: log 5 · beacon: questions · beacon: report · beacon: help
Expect each answer in the chat, with no Claude turn after it.

## 7. Steering
beacon: add P2 <an idea of yours>   → a "● you" node on the graph, Claude is told about it
beacon: pause   → ask Claude anything; when it stops: `let the session stop: paused`
beacon: resume  → Claude is told the next node
beacon: abandon <node> <why> / beacon: reopen <node> <note>

## 8. The guard
Ask Claude: "run sqlite3 on the beacon ledger.db in {run}" → refused (deny rule or `beacon guard ▸ blocked`).
Ask Claude: "run .claude/beacon verify P1" → refused (deny rule, or `beacon guard ▸ blocked Bash: That beacon command is for the human`).

## 8b. Standing approval
Ask Claude: "before going on, ask beacon whether you may run hide.py on the Kali VM" → the ask tool answers
`Approved — the human's standing rule ...`; no question reaches you. (With ask_before_running_real_samples=true
a request to execute a real malware sample would come to you instead.)

## 9. The decider at a branch (needs BEACON_API_KEY in the shell you started from: Claude Code does not pass ANTHROPIC_API_KEY to MCP servers)
Ask Claude: "for P2, list two or three ways to proceed and let beacon decide (ask with approach_choice)".
Expect the tool's reply to start `The decider (claude-opus-5-5) chose ...` with why / first step / switch if;
`beacon: decisions` shows it; the graph marks the choice ⚖. Without a key the reply ends `(Decider: unavailable ...)`.

## 9b. The lighthouse (BEACON_API_KEY, or ANTHROPIC_API_KEY)
beacon: lighthouse review the assumptions   → at Claude's next stop: `lighthouse (you asked) queued N idea(s)`,
✦ nodes on the graph.

## 10. From a terminal
    {project}/.claude/beacon log -f --run {run}
    {project}/.claude/beacon transcript -n 40 --run {run}
    {project}/.claude/beacon check --run {run}        (expect: OK)

If something fails, keep {run}/hook-errors.log, the doctor output and `claude --version`.
To start over: `{project}/.claude/beacon demo {base} --force`.
"""


# ------------------------------------------------------------------ the live smoke test
class Results:
    def __init__(self, out=print):
        self.rows, self.out = [], out

    def add(self, status, what, detail=""):
        self.rows.append((status, what, detail))
        self.out(f"  [{status}] {what}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self):
        return any(r[0] == "FAIL" for r in self.rows)


def _installed_hooks(project):
    with open(os.path.join(project, ".claude", "settings.local.json"), encoding="utf-8") as fh:
        hooks = json.load(fh).get("hooks", {})
    out = {}
    for event, groups in hooks.items():
        for g in groups:
            for h in g.get("hooks", []):
                if "beacon.hooks" in str(h.get("command", "")):
                    out[event] = h["command"]
    return out


def check_wiring(kit, res):
    """Run the hook commands exactly as installed (as Claude Code would) with sample input."""
    hooks = _installed_hooks(kit["project"])
    env = dict(os.environ, BEACON_RUN=kit["run"], BEACON_NO_BROWSER="1")
    env.pop("BEACON_WATCHDOG", None)
    cases = {
        "PreToolUse": ({"session_id": "wiring", "tool_name": "Bash",
                        "tool_input": {"command": f"sqlite3 {kit['run']}/ledger.db .dump"}},
                       lambda o: o.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"),
        "UserPromptSubmit": ({"session_id": "wiring", "prompt": "beacon: help"},
                             lambda o: o.get("decision") == "block" and "beacon" in o.get("reason", "")),
        "PostToolUse": ({"session_id": "wiring", "tool_name": "Bash", "tool_response": "ok"}, lambda o: True),
    }
    for event, (payload, good) in cases.items():
        cmd = hooks.get(event)
        if not cmd:
            res.add("FAIL", f"hook {event} installed", "missing from .claude/settings.local.json")
            continue
        p = shellrun.run(cmd, input=json.dumps(payload), env=env, cwd=kit["project"], timeout=60)  # as Claude Code
        try:
            obj = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            obj = None
        if p.returncode == 0 and obj is not None and good(obj):
            res.add("PASS", f"hook {event} runs as installed")
        else:
            res.add("FAIL", f"hook {event} runs as installed", f"exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    missing = [e for e in ("Stop", "SessionStart", "StopFailure") if e not in hooks]
    (res.add("FAIL", "Stop / SessionStart / StopFailure hooks installed", ", ".join(missing)) if missing
     else res.add("PASS", "Stop / SessionStart / StopFailure hooks installed"))


def _decisions(run):
    out = []
    try:
        with open(os.path.join(run, "decisions.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _check_lighthouse(L, res, transport=None, key=None):
    """One real lighthouse call, on a copy of the ledger (the run itself is not changed)."""
    import sqlite3
    import tempfile
    from .ledger import Ledger
    from .lighthouse import run_lighthouse
    with tempfile.TemporaryDirectory() as td:
        dst = sqlite3.connect(os.path.join(td, "ledger.db"))
        L.db.backup(dst)
        dst.close()
        C = Ledger.open(td)
        try:
            r = run_lighthouse(C, task="human", focus=["smoke test of the API path"], transport=transport, key=key)
        finally:
            C.close()
    st = r.get("status")
    if st in ("ok", "empty"):
        res.add("PASS", "the lighthouse (Claude API) answered", f"{st}, {len(r.get('added') or [])} idea(s) "
                f"(on a copy of the ledger)")
    elif st == "refusal":
        res.add("WARN", "the lighthouse (Claude API) answered", "the model refused (counted as a dry round)")
    else:
        res.add("FAIL", "the lighthouse (Claude API) answered", f"{st}: {r.get('detail', '')[:300]}")


def _check_decider(L, res, transport=None, key=None):
    """One real decider call (Opus at a branch), on a copy of the ledger."""
    import sqlite3
    import tempfile
    from .decider import decide
    from .ledger import Ledger
    with tempfile.TemporaryDirectory() as td:
        dst = sqlite3.connect(os.path.join(td, "ledger.db"))
        L.db.backup(dst)
        dst.close()
        C = Ledger.open(td)
        try:
            open_p = [p for p in C.problems() if p["status"] in ("todo", "in_progress")]
            if not open_p:
                res.add("SKIP", "the decider (Claude API) at a branch", "no open problem left to branch on")
                return
            pid = open_p[0]["id"]
            ids = C.ask("approach_choice", "smoke test: which way first?", node_id=pid,
                        options=["read the cipher description and write a decryptor",
                                 "brute force the passphrase with a wordlist"])["created"]
            d = decide(C, pid, "smoke test: which way first?", ids, transport=transport, key=key)
        finally:
            C.close()
    if d["status"] == "ok":
        res.add("PASS", "the decider (Claude API) at a branch", f"{d['model']} chose option "
                f"{ids.index(d['order'][0]) + 1 if d['order'][0] in ids else 'new'}: {d['why'][:160]}")
    elif d["status"] == "refusal":
        res.add("WARN", "the decider (Claude API) at a branch", "the model refused; the worker's order stood")
    else:
        res.add("FAIL", "the decider (Claude API) at a branch", f"{d['status']}: {d.get('detail', '')[:300]}")


def smoke(kit, runner, res, lighthouse_key=None):
    """The live cycle. `runner` has run(prompt, resume=None) -> SessionResult (watchdog.CliRunner)."""
    from .ledger import Ledger
    from .watchdog import hooks_disabled
    cost = 0.0
    res.out("\nsession 1: Claude is told to solve only P1 and stop")
    r1 = runner.run(PROMPT_FIRST)
    cost += r1.cost_usd
    if r1.error_kind:
        res.add("FAIL", "session 1 ran", f"{r1.error_kind}: {r1.text[-300:]}")
        return cost
    off = hooks_disabled(r1.notices)
    decs = _decisions(kit["run"])
    if off and not decs:
        res.add("FAIL", "Claude Code ran beacon's hooks", "it turned the user hooks off: " + off[:300])
        return cost
    res.add("PASS" if decs else "FAIL", "the Stop gate decided at each stop",
            f"{len(decs)} decision(s)" if decs else "no decision logged: the Stop hook did not run")
    L = Ledger.open(kit["run"])
    try:
        p1 = L.node("P1")
        res.add("PASS" if p1["status"] == "done" else "FAIL", "P1 solved and verified by check.py",
                f"P1 is {p1['status']}")
        pushed = [d for d in decs if d.get("action") == "block"]
        res.add("PASS" if pushed else "WARN", "beacon kept Claude going when it stopped with work left",
                f"{len(pushed)} forced continuation(s)" if pushed else
                "Claude never stopped with work left in this run, so this was not exercised")
        added = [n for n in L.nodes() if n["kind"] != "problem" and n["source"] == "worker"]
        ev = L.evidence()
        res.add("PASS" if added and ev else "FAIL", "Claude used the beacon MCP tools",
                f"{len(added)} node(s), {len(ev)} evidence item(s)")
        qs = L.questions("open")
        need = [q for q in qs if q["kind"] in ("need_info", "need_permission")]
        if not need:
            res.add("WARN" if L.node("P2")["status"] == "done" else "FAIL", "Claude asked the human for the passphrase",
                    f"no open question; P2 is {L.node('P2')['status']}")
            if L.node("P2")["status"] != "done":
                return cost
        else:
            res.add("PASS", "Claude asked the human for the passphrase", f"question #{need[0]['id']}")
            last = decs[-1] if decs else {}
            res.add("PASS" if last.get("action") == "allow" else "WARN", "the session stopped to wait for the human",
                    f"last decision {last.get('action')}:{last.get('why')}")
            L.answer(need[0]["id"], PASSPHRASE, actor="human")
            res.out(f"  (answered question #{need[0]['id']} as the human)")
        from .decider import beacon_key
        if lighthouse_key:
            _check_lighthouse(L, res)
            if beacon_key():
                _check_decider(L, res, key=beacon_key())
            else:
                res.add("WARN", "the decider (Claude API) at a branch", "only ANTHROPIC_API_KEY is set: the MCP server "
                        "where the decider runs gets it only if Claude Code does not scrub credentials; set "
                        "BEACON_API_KEY")
        else:
            res.add("SKIP", "the lighthouse and the decider (Claude API)", "no BEACON_API_KEY / ANTHROPIC_API_KEY")
    finally:
        L.close()

    res.out("\nsession 2: resumed after the answer")
    r2 = runner.run(PROMPT_RESUME, resume=r1.session_id or None)
    cost += r2.cost_usd
    if r2.error_kind:
        res.add("FAIL", "session 2 ran", f"{r2.error_kind}: {r2.text[-300:]}")
        return cost
    L = Ledger.open(kit["run"])
    try:
        p2, p3 = L.node("P2"), L.node("P3")
        res.add("PASS" if p2["status"] == "done" else "FAIL", "P2 solved with the human's answer", f"P2 is {p2['status']}")
        if p3["status"] == "pending_verification":
            res.add("PASS", "P3 waits for the human's verification", f"candidate {p3['candidate'][:80]}")
            L.verify("P3", True, "smoke test: verified by the human", actor="human")
        else:
            res.add("WARN", "P3 waits for the human's verification", f"P3 is {p3['status']}")
        ok, why = L.verify_integrity()
        res.add("PASS" if ok else "FAIL", "ledger integrity (hash chain)", why)
        from .board import write_board
        from .logview import sessions, stop_log
        path = write_board(L)
        res.add("PASS", "graph written", f"file://{path}")
        res.add("PASS" if "beacon replied" in stop_log(L.dir) else "WARN", "beacon log shows its replies")
        res.add("PASS" if sessions(L.dir) else "WARN", "beacon transcript finds the sessions")
        done = [p["id"] for p in L.problems() if p["status"] == "done"]
        res.add("PASS" if len(done) == 3 else "WARN", "all three problems done", ", ".join(done) or "none")
    finally:
        L.close()
    return cost


def run_auto(kit, claude="claude", model=None, max_budget_usd=1.0, out=print):
    from .doctor import doctor
    from .lighthouse import load_key
    from .watchdog import CliRunner
    res = Results(out)
    out("checks before the live run")
    rep = doctor(run=kit["run"], project=kit["project"], claude=claude)
    fails = [ln for ln in rep.lines if ln.startswith("[FAIL]")]
    res.add("FAIL" if fails else "PASS", "beacon doctor", "; ".join(fails)[:400])
    check_wiring(kit, res)
    if res.failed:
        out("\nFix the FAIL rows first (nothing was sent to Claude).")
        return res, 0.0
    runner = CliRunner(kit["project"], kit["run"], claude=claude, permission_mode="acceptEdits", model=model,
                       max_budget_usd=max_budget_usd, timeout_s=20 * 60)
    t0 = time.time()
    cost = smoke(kit, runner, res, lighthouse_key=load_key())
    out(f"\n{sum(r[0] == 'PASS' for r in res.rows)} PASS, {sum(r[0] == 'WARN' for r in res.rows)} WARN, "
        f"{sum(r[0] == 'FAIL' for r in res.rows)} FAIL, {sum(r[0] == 'SKIP' for r in res.rows)} SKIP · "
        f"{time.time() - t0:.0f} s · Claude Code reported ~${cost:.2f}")
    w = os.path.join(kit["project"], ".claude", "beacon")
    out(f"Look at it: {w} graph --run {kit['run']}   |   {w} log --run {kit['run']}")
    return res, cost
