# -*- coding: utf-8 -*-
"""The demo kit and the live smoke harness (with a scripted worker instead of the model)."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from beacon import demo, hooks, tools
from beacon.cli import parse_checklist
from beacon.ledger import Ledger
from beacon.watchdog import SessionResult


@pytest.fixture
def kit(tmp_path):
    return demo.make_kit(str(tmp_path / "kit"))


def test_kit_files_and_settings(kit):
    proj = kit["project"]
    for rel in ("chal1/hide.py", "chal2/data.bin", "chal2/README.txt", "check.py", ".claude/beacon",
                ".claude/settings.local.json", ".mcp.json", "CLAUDE.md"):
        assert os.path.exists(os.path.join(proj, rel)), rel
    with open(os.path.join(proj, ".claude", "settings.local.json"), encoding="utf-8") as fh:
        s = json.load(fh)
    assert "Bash(python3 *)" in s["permissions"]["allow"] and "mcp__beacon" in s["permissions"]["allow"]
    assert set(demo._installed_hooks(proj)) >= {"Stop", "SessionStart", "PreToolUse", "PostToolUse",
                                                 "StopFailure", "UserPromptSubmit"}
    L = Ledger.open(kit["run"])
    try:
        assert [p["title"][:11] for p in L.problems()] == ["Challenge 1", "Challenge 2", "Write notes"]
        assert L.config()["permission_mode"] == "acceptEdits" and L.workdir() == proj
        assert L.node("P1")["verify_cmd"].endswith("check.py 1") and not L.node("P3")["verify_cmd"]
    finally:
        L.close()
    with open(kit["testing"], encoding="utf-8") as fh:
        text = fh.read()
    assert demo.PASSPHRASE in text and "beacon: verify P3" in text
    # the secret is not in the project Claude works in
    for root, _, files in os.walk(proj):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                assert demo.PASSPHRASE.encode() not in fh.read(), f


def test_puzzles_are_solvable_and_checked(kit):
    proj = kit["project"]
    spec = importlib.util.spec_from_file_location("hide", os.path.join(proj, "chal1", "hide.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert bytes(a ^ b for a, b in zip(m.BLOB, m.keystream(len(m.BLOB)))).decode() == demo.FLAG1
    with open(os.path.join(proj, "chal2", "data.bin"), "rb") as fh:
        data = fh.read()
    assert demo._xor(data, demo.keystream2(demo.PASSPHRASE, len(data))).decode() == demo.FLAG2

    def check(n, cand):
        env = dict(os.environ, BEACON_CANDIDATE=cand)
        return subprocess.run([sys.executable, "check.py", n], cwd=proj, env=env, capture_output=True).returncode

    assert check("1", demo.FLAG1) == 0 and check("2", demo.FLAG2) == 0
    assert check("1", demo.FLAG2) == 1 and check("2", "flag{guess}") == 1 and check("3", demo.FLAG1) == 1


def test_kit_is_not_overwritten(kit):
    with pytest.raises(FileExistsError):
        demo.make_kit(kit["base"])
    L = Ledger.open(kit["run"])
    L.add_node("P1", "something tried before the rebuild")
    L.close()
    os.makedirs(os.path.join(kit["project"], "notes"), exist_ok=True)
    with open(os.path.join(kit["project"], "notes", "chal1.md"), "w", encoding="utf-8") as fh:
        fh.write("old note")
    demo.make_kit(kit["base"], force=True)
    L = Ledger.open(kit["run"])
    try:
        assert len(L.nodes()) == 3 and L.verify_integrity()[0]
    finally:
        L.close()
    assert not os.path.exists(os.path.join(kit["project"], "notes", "chal1.md"))


def test_checklist_template_parses():
    title, problems = parse_checklist(demo.CHECKLIST.format(py="python3"))
    assert title == "beacon demo" and len(problems) == 3 and "need_info" in problems[1]["rationale"]


def test_wiring_runs_the_installed_hook_commands(kit):
    res = demo.Results(out=lambda *_: None)
    demo.check_wiring(kit, res)
    assert not res.failed, res.rows
    assert {r[1] for r in res.rows} >= {"hook PreToolUse runs as installed", "hook UserPromptSubmit runs as installed"}


# ------------------------------------------------------------------ the smoke flow with a scripted worker
def _eid(msg):
    assert msg.startswith("evidence #"), msg
    return int(msg.split("#")[1].split()[0])


class ScriptedWorker:
    """Does what a well-behaved Claude Code would, through the same tools and hooks."""

    def __init__(self, kit, ask=True, push=True):
        self.kit, self.ask, self.push, self.calls = kit, ask, push, 0

    def _stop(self, text):
        return hooks.stop(self.kit["run"], {"session_id": "s1", "last_assistant_message": text,
                                            "transcript_path": os.path.join(self.kit["base"], "t.jsonl")})

    def run(self, prompt, resume=None):
        run = self.kit["run"]
        self.calls += 1
        if self.calls == 1:
            a = tools.add_node("P1", "XOR BLOB with the keystream from hide.py", kind="approach", run=run).split()[1]
            tools.start_node(a, run=run)
            e = _eid(tools.attach_evidence(a, "decoded BLOB with keystream()", content=demo.FLAG1, run=run))
            assert "VERIFIED" in tools.propose_done("P1", demo.FLAG1, [e], run=run)
            if self.push:
                out = self._stop("P1 is done. Stopping as asked.")
                assert out["decision"] == "block"
            b = tools.add_node("P2", "decrypt data.bin with the passphrase", kind="approach", run=run).split()[1]
            tools.start_node(b, run=run)
            if self.ask:
                tools.ask("need_info", "What is the passphrase for chal2/data.bin?", node_id=b, run=run)
            c = tools.add_node("P3", "write notes/chal1.md", kind="approach", run=run).split()[1]
            tools.start_node(c, run=run)
            e = _eid(tools.attach_evidence(c, "wrote the note", content="notes/chal1.md: keystream x*37+11", run=run))
            assert "pending" in tools.propose_done("P3", "notes/chal1.md", [e], run=run)
            self._stop("Waiting for the passphrase.")
            return SessionResult(session_id="s1", cost_usd=0.01)
        hooks.session_start(run, {"session_id": "s1", "source": "resume"})
        if self.ask:
            tools.start_node("P2.A1", run=run)
        e = _eid(tools.attach_evidence("P2.A1", "decrypted with the human's passphrase", content=demo.FLAG2, run=run))
        assert "VERIFIED" in tools.propose_done("P2", demo.FLAG2, [e], run=run)
        self._stop("All done.")
        return SessionResult(session_id="s1", cost_usd=0.02)


def test_smoke_flow_passes_with_a_well_behaved_worker(kit):
    res = demo.Results(out=lambda *_: None)
    cost = demo.smoke(kit, ScriptedWorker(kit), res)
    assert not res.failed, res.rows
    status = {what: st for st, what, _ in res.rows}
    for what in ("the Stop gate decided at each stop", "P1 solved and verified by check.py",
                 "beacon kept Claude going when it stopped with work left", "Claude used the beacon MCP tools",
                 "Claude asked the human for the passphrase", "the session stopped to wait for the human",
                 "P2 solved with the human's answer", "P3 waits for the human's verification",
                 "ledger integrity (hash chain)", "all three problems done"):
        assert status[what] == "PASS", (what, res.rows)
    assert status["the lighthouse and the decider (Claude API)"] == "SKIP"
    assert abs(cost - 0.03) < 1e-9


def test_smoke_flags_a_worker_that_never_asks(kit):
    res = demo.Results(out=lambda *_: None)
    demo.smoke(kit, ScriptedWorker(kit, ask=False), res)
    assert res.failed and ("FAIL", "Claude asked the human for the passphrase") in [r[:2] for r in res.rows]


def test_smoke_reports_hooks_off(kit):
    class HooksOff:
        def run(self, prompt, resume=None):
            return SessionResult(session_id="x", notices=["Hooks not running: could not load organization settings"])

    res = demo.Results(out=lambda *_: None)
    demo.smoke(kit, HooksOff(), res)
    assert res.rows[-1][:2] == ("FAIL", "Claude Code ran beacon's hooks")


def test_lighthouse_check_uses_a_copy(kit):
    good = {"parent": "P2", "title": "Ask for the passphrase format before guessing", "kind": "new_class",
            "targets": "P2", "first_command": "cat chal2/README.txt", "expected_observation": "the cipher description"}

    def transport(body, key, timeout):
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"suggestions": [good]})}]}

    L = Ledger.open(kit["run"])
    try:
        before = len(L.nodes())
        res = demo.Results(out=lambda *_: None)
        demo._check_lighthouse(L, res, transport=transport, key="k")
        assert res.rows[0][0] == "PASS" and "1 idea" in res.rows[0][2]
        assert len(L.nodes()) == before                      # the real run is untouched
        res = demo.Results(out=lambda *_: None)
        demo._check_lighthouse(L, res, transport=lambda *a: 1 / 0, key="k")
        assert res.rows[0][0] == "FAIL"
    finally:
        L.close()


def test_cli_demo_manual(tmp_path, capsys):
    from beacon import cli
    base = str(tmp_path / "k")
    assert cli.main(["demo", base]) == 0
    out = capsys.readouterr().out
    assert "TESTING.md" in out and ".claude/beacon claude --run" in out
    assert cli.main(["demo", base]) == 0                     # an existing kit is reused for the manual test
    assert "using the demo kit already" in capsys.readouterr().out


def test_decider_check_uses_a_copy(kit):
    good = {"ranking": [1, 2], "why": "The README gives the cipher; a decryptor is direct.", "first_step": "cat",
            "switch_if": "the README is wrong", "new_option": None, "confidence": 0.8}

    def transport(body, key, timeout):
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "```json\n" + json.dumps(good) + "\n```"}]}

    L = Ledger.open(kit["run"])
    try:
        before = len(L.nodes())
        res = demo.Results(out=lambda *_: None)
        demo._check_decider(L, res, transport=transport, key="k")
        assert res.rows[0][0] == "PASS" and "chose option 1" in res.rows[0][2]
        assert len(L.nodes()) == before
    finally:
        L.close()
