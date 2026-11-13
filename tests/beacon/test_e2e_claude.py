"""End to end with the REAL Claude Code CLI against a scripted Messages API (tests/beacon/e2e/mock_api.py).

Opt-in:  BEACON_E2E_CLAUDE=/path/to/claude python3 -m pytest tests/beacon/test_e2e_claude.py

No model, no account, no network: Claude Code talks to a local mock with a dummy key, in an
isolated HOME. What this measures and what it cannot:
  * the beacon MCP server as Claude Code actually calls it, the watchdog's runner on Claude
    Code's real stream-json, and --resume;
  * the hooks: Claude Code (measured on 2.1.322) only runs user hooks once it has read the
    organization's settings from api.anthropic.com, which a mock cannot answer. So the hook
    test is adaptive: where Claude Code reports the hooks off, it checks that the watchdog
    names that precisely ("hooks_disabled"); where the hooks do run, it checks the whole
    gate flow (SessionStart context, guard denial, activity, Stop block -> continue -> allow).
"""
import glob
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "e2e"))

from mock_api import MockAPI  # noqa: E402

from beacon.ledger import Ledger  # noqa: E402
from beacon.project import setup_project  # noqa: E402
from beacon.watchdog import FRESH_PROMPT, RESUME_PROMPT, CliRunner, watch  # noqa: E402

CLAUDE = os.environ.get("BEACON_E2E_CLAUDE", "")
pytestmark = pytest.mark.skipif(not (CLAUDE and os.access(CLAUDE, os.X_OK)),
                                reason="set BEACON_E2E_CLAUDE to a claude CLI to run the end-to-end tests")


def scenario(ledger_path):
    return [
        {"text": "Looking at the ledger.", "tool": {"name": "mcp__beacon__ledger_view", "input": {}}},
        {"text": "Adding an approach.", "tool": {"name": "mcp__beacon__add_node", "input": {
            "parent": "P1", "title": "emulate the decrypt routine", "kind": "approach"}}},
        {"text": "Starting it.", "tool": {"name": "mcp__beacon__start_node", "input": {"node_id": "P1.A1"}}},
        {"text": "Running it.", "tool": {"name": "Bash", "input": {
            "command": "echo FLAG-CANDIDATE flag{e2e}", "description": "print the candidate"}}},
        {"text": "Recording it.", "tool": {"name": "mcp__beacon__attach_evidence", "input": {
            "node_id": "P1.A1", "summary": "echo output shows the candidate",
            "content": "FLAG-CANDIDATE flag{e2e}"}}},
        {"text": "Peeking at the ledger file.", "tool": {"name": "Bash", "input": {
            "command": f"sqlite3 {ledger_path} 'select 1'", "description": "peek"}}},
        {"text": "Pausing here. Shall I continue?"},
        {"text": "Proposing the answer.", "tool": {"name": "mcp__beacon__propose_done", "input": {
            "problem_id": "P1", "candidate": "flag{e2e}", "evidence_ids": [1]}}},
        {"text": "Done, the flag is verified."},
    ]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    L = Ledger.create(str(tmp_path / "run"), "e2e", [{
        "title": "Recover the e2e flag", "verify_cmd": 'test "$BEACON_CANDIDATE" = "flag{e2e}"'}],
        workdir=str(proj))
    api = MockAPI(scenario(L.path))
    url = api.start()
    for k in ("BEACON_RUN", "BEACON_WATCHDOG", "BEACON_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    # HOME moves to a temp dir below; keep Python's user site (pip --user packages such as mcp) where it is,
    # or the beacon MCP server started by Claude Code cannot import them.
    import site
    monkeypatch.setenv("PYTHONUSERBASE", os.environ.get("PYTHONUSERBASE") or site.getuserbase())
    for k, v in {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                 "ANTHROPIC_API_KEY": "sk-ant-mock", "ANTHROPIC_BASE_URL": url,
                 "BEACON_API_URL": url + "/v1/messages", "NO_PROXY": "127.0.0.1,localhost",
                 "no_proxy": "127.0.0.1,localhost", "DISABLE_AUTOUPDATER": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("beacon.lighthouse.API_URL", url + "/v1/messages")
    yield {"L": L, "api": api, "proj": str(proj), "run": L.dir}
    api.stop()
    L.close()


def runner(lab, **kw):
    return CliRunner(cwd=lab["proj"], run=lab["run"], claude=CLAUDE, timeout_s=300,
                     extra_args=["--allowedTools", "Bash(echo *)"], **kw)


def test_mcp_tools_runner_and_resume(lab):
    setup_project(lab["proj"], sys.executable, sys.executable)
    sp = os.path.join(lab["proj"], ".claude", "settings.local.json")
    cfg = json.load(open(sp, encoding="utf-8"))
    cfg["hooks"] = {}                       # MCP path only
    json.dump(cfg, open(sp, "w", encoding="utf-8"))
    r = runner(lab)
    res = r.run(FRESH_PROMPT)
    assert res.error_kind == "" and res.subtype == "success" and res.session_id and res.num_turns >= 6
    L = lab["L"]
    assert L.node("P1.A1")["status"] == "in_progress"
    assert L.evidence("P1.A1")[0]["summary"] == "echo output shows the candidate"
    assert "[step 6]" in res.text
    res2 = r.run(RESUME_PROMPT, resume=res.session_id)    # the script continues from the history
    assert res2.error_kind == "", res2
    assert L.node("P1")["status"] == "done" and L.node("P1")["candidate"] == "flag{e2e}"
    assert L.verify_integrity()[0]


def _tool_results(run):
    out = []
    for f in sorted(glob.glob(os.path.join(run, "sessions", "*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            e = json.loads(line)
            c = (e.get("message") or {}).get("content") if e.get("type") == "user" else None
            for b in c if isinstance(c, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append(json.dumps(b.get("content")))
    return out


def test_decider_runs_inside_the_mcp_server(lab, monkeypatch):
    """Claude Code accepts the ${VAR:-} entries in .mcp.json; BEACON_API_KEY and the URL reach the beacon
    MCP server (ANTHROPIC_API_KEY does not: Claude Code blanks it there); a branch comes back decided."""
    monkeypatch.setenv("BEACON_API_KEY", "sk-beacon-mock")
    setup_project(lab["proj"], sys.executable, sys.executable)
    sp = os.path.join(lab["proj"], ".claude", "settings.local.json")
    cfg = json.load(open(sp, encoding="utf-8"))
    cfg["hooks"] = {}
    json.dump(cfg, open(sp, "w", encoding="utf-8"))
    lab["api"].steps = [
        {"text": "Branching.", "tool": {"name": "mcp__beacon__ask", "input": {
            "kind": "approach_choice", "question": "emulate or hook first?", "node_id": "P1",
            "options": ["emulate the decrypt routine", "hook it with Frida"],
            "context": "evidence: none yet"}}},
        {"text": "Following the decision."},
    ]
    res = runner(lab).run(FRESH_PROMPT)
    assert res.error_kind == "", res
    from beacon.decider import recent
    L = lab["L"]
    decided = recent(L)
    assert decided and decided[0]["order"] == ["P1.A2", "P1.A1"] and decided[0]["why"] == "mock decision"
    assert L.next_node()["id"] == "P1.A2"
    sent = [(h, b) for _, h, b in lab["api"].requests if "You are the decider" in json.dumps(b.get("system", ""))]
    assert sent and "evidence: none yet" in sent[0][1]["messages"][0]["content"]
    assert {k.lower(): v for k, v in sent[0][0].items()}.get("x-api-key") == "sk-beacon-mock"
    assert any("The decider (claude-opus-5-5) chose P1.A2" in t for t in _tool_results(lab["run"]))


@pytest.mark.parametrize("scrub", ["", "1"])
def test_anthropic_api_key_reaches_the_mcp_server_unless_scrubbed(lab, monkeypatch, scrub):
    """Measured behaviour the key design rests on. With CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 Claude Code removes
    ANTHROPIC_API_KEY from MCP servers: the decider has no key, says so, and the worker's order stands. Without
    it the server inherits the key (plain Linux, measured) -- unless the environment scrubs on its own, as
    Claude's cloud sessions do (measured: blank there). BEACON_API_KEY is the key that works in every case."""
    if scrub:
        monkeypatch.setenv("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", scrub)
    else:
        monkeypatch.delenv("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", raising=False)
    setup_project(lab["proj"], sys.executable, sys.executable)
    sp = os.path.join(lab["proj"], ".claude", "settings.local.json")
    cfg = json.load(open(sp, encoding="utf-8"))
    cfg["hooks"] = {}
    json.dump(cfg, open(sp, "w", encoding="utf-8"))
    lab["api"].steps = [
        {"text": "Branching.", "tool": {"name": "mcp__beacon__ask", "input": {
            "kind": "approach_choice", "question": "emulate or hook first?", "node_id": "P1",
            "options": ["emulate the decrypt routine", "hook it with Frida"]}}},
        {"text": "Taking the first."},
    ]
    res = runner(lab).run(FRESH_PROMPT)
    if scrub and res.error_kind == "config" and "bubblewrap" in res.text:
        pytest.skip("Claude Code needs bubblewrap for credential scrubbing on Linux (not installed here)")
    assert res.error_kind == "", res
    unavailable = any("Decider: unavailable" in t for t in _tool_results(lab["run"]))
    sent = [h for _, h, b in lab["api"].requests if "You are the decider" in json.dumps(b.get("system", ""))]
    if scrub or os.environ.get("CLAUDE_CODE_REMOTE"):
        assert unavailable and not sent
    else:
        assert sent and {k.lower(): v for k, v in sent[0].items()}.get("x-api-key") == "sk-ant-mock"


def test_hooks_gate_flow_or_precise_hooks_off(lab):
    setup_project(lab["proj"], sys.executable, sys.executable)
    out = watch(lab["run"], runner(lab), max_sessions=3, log=lambda *_: None)
    L, run = lab["L"], lab["run"]
    if out["why"] == "hooks_disabled":
        # Claude Code could not read the organization's settings: no hooks, hook-checked calls denied.
        assert "Hooks not running" in out["detail"] or "aren't running" in out["detail"]
        assert L.node("P1")["status"] == "todo"
        assert not os.path.exists(os.path.join(run, "decisions.jsonl"))
        pytest.skip("Claude Code ran with hooks off here (organization settings unreadable); "
                    "watchdog reported it as hooks_disabled")
    assert out["why"] == "gate_no_open_problems", out
    decisions = [json.loads(x) for x in open(os.path.join(run, "decisions.jsonl"), encoding="utf-8")]
    assert [d["action"] for d in decisions][:1] == ["block"] and decisions[-1]["why"] == "no_open_problems"
    assert "supervises this session" in lab["api"].main_text(0)            # SessionStart context
    assert "Beacon ledger" in lab["api"].all_main_text()                   # Stop-gate feedback came back
    assert os.path.exists(os.path.join(run, "denials.jsonl"))              # guard stopped the sqlite3 peek
    assert any(json.loads(x)["tool"] == "Bash" for x in open(os.path.join(run, "activity.jsonl"), encoding="utf-8"))
    assert L.node("P1")["status"] == "done"


DENY_STEPS = lambda ledger, proj, settings: [  # noqa: E731
    {"text": "peek", "tool": {"name": "Bash", "input": {"command": f"sqlite3 {ledger} 'select 1'",
                                                         "description": "peek"}}},
    {"text": "rewire", "tool": {"name": "Write", "input": {"file_path": os.path.join(proj, ".mcp.json"),
                                                           "content": "{}"}}},
    {"text": "unhook", "tool": {"name": "Write", "input": {"file_path": settings, "content": "{}"}}},
    {"text": "notes", "tool": {"name": "Write", "input": {"file_path": os.path.join(proj, "notes.txt"),
                                                          "content": "fine"}}},
    {"text": "echo", "tool": {"name": "Bash", "input": {"command": "echo still-allowed", "description": "echo"}}},
    {"text": "done"},
]


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
def test_deny_rules_protect_the_supervisor_in_every_mode(lab, mode, monkeypatch):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        monkeypatch.setenv("IS_SANDBOX", "1")     # Claude Code refuses bypass as root outside a sandbox
    proj = lab["proj"]
    setup_project(proj, sys.executable, sys.executable)
    sp = os.path.join(proj, ".claude", "settings.local.json")
    cfg = json.load(open(sp, encoding="utf-8"))
    cfg["hooks"] = {}                             # measure the deny rules alone
    json.dump(cfg, open(sp, "w", encoding="utf-8"))
    mcp_before = open(os.path.join(proj, ".mcp.json"), encoding="utf-8").read()
    lab["api"].steps = DENY_STEPS(lab["L"].path, proj, sp)
    extra = [] if mode == "bypassPermissions" else ["--allowedTools", "Bash"]
    res = CliRunner(cwd=proj, run=lab["run"], claude=CLAUDE, timeout_s=300, permission_mode=mode,
                    extra_args=extra).run(FRESH_PROMPT)
    assert res.error_kind == "", res
    assert open(os.path.join(proj, ".mcp.json"), encoding="utf-8").read() == mcp_before
    assert "mcp__beacon" in open(sp, encoding="utf-8").read()
    assert open(os.path.join(proj, "notes.txt"), encoding="utf-8").read() == "fine"        # ordinary work is not blocked
    stream = "".join(open(os.path.join(lab["run"], "sessions", f), encoding="utf-8").read()
                     for f in os.listdir(os.path.join(lab["run"], "sessions")))
    assert f'"permissionMode": "{mode}"' in stream or f'"permissionMode":"{mode}"' in stream
    assert "still-allowed" in stream and "has been denied" in stream



def test_a_final_list_of_leftovers_becomes_nodes_through_the_real_hooks(lab):
    """The worker ends its turn listing what is not done; the Stop hook (real Claude Code) reads it through the
    follow-up model, the item becomes a node under the open problem, and the session is kept going."""
    L = lab["L"]
    L.set_config("hard_forced", 2)
    L.set_config("k_stall", 99)
    L.set_config("k_idle", 99)
    setup_project(lab["proj"], sys.executable, sys.executable)
    lab["api"].steps = [{"text": "Main part done. Still missing: not tested on the Windows VM yet."},
                        {"text": "Working on it."}]
    lab["api"].followup_reply = {"items": [{
        "title": "Run the e2e check on the Windows VM", "detail": "not tested there yet", "kind": "work",
        "about": "P1", "existing": "", "question": ""}]}
    out = watch(lab["run"], runner(lab), max_sessions=1, log=lambda *_: None)
    if out["why"] == "hooks_disabled":
        pytest.skip("Claude Code ran with hooks off here")
    decisions = [json.loads(x) for x in open(os.path.join(lab["run"], "decisions.jsonl"), encoding="utf-8")]
    assert decisions[0]["action"] == "block" and "listed work that is not done" in decisions[0]["reason"]
    assert decisions[0]["classified"]["followups"]["work"] == ["P1.S1"]
    n = L.node("P1.S1")
    assert n["source"] == "followup" and n["title"] == "Run the e2e check on the Windows VM"
    assert any("pull out the work it says is NOT done" in json.dumps(b.get("system", ""))
               for _, _, b in lab["api"].requests)
    assert "listed work that is not done" in lab["api"].all_main_text()   # the nudge reached the worker
