# -*- coding: utf-8 -*-
"""Talking to beacon from the Claude Code chat: `beacon:` commands and answers (UserPromptSubmit)."""
import json
import os
import subprocess
import sys

import pytest

from beacon import chat
from beacon.hooks import guard

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_hook(event, payload, run=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BEACON_", "ANTHROPIC_"))}
    env["PYTHONPATH"] = ROOT
    if run:
        env["BEACON_RUN"] = run
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-m", "beacon.hooks", event], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _pending(L, pid="P1"):
    """Put P1 in pending_verification (a candidate the human must check)."""
    L.add_node(pid, "emulate the routine", kind="approach")
    L.start(f"{pid}.A1")
    eid, _ = L.attach_evidence(f"{pid}.A1", "the emulator printed it", f"FLAG flag{{{pid}}}")
    L.propose_done(pid, "flag{x}", [eid])
    assert L.node(pid)["status"] == "pending_verification"


# ------------------------------------------------------------------ views: answered by beacon, no model turn
def test_views_stop_the_prompt(make_run):
    L = make_run()
    for prompt, want in (("beacon: status", "P1 [todo] Solve challenge one"), ("Beacon:help", "commands you can type in this chat"),
                         ("beacon: questions", "No open questions"), ("beacon: log", "no stops recorded yet"),
                         ("beacon: frobnicate", "unknown beacon command 'frobnicate'")):
        r = chat.handle(L, prompt)
        assert r["block"] is True and want in r["show"], prompt


def test_no_open_questions_plain_prompt_is_untouched(make_run):
    L = make_run()
    assert chat.handle(L, "please keep going") is None
    assert chat.handle(L, "#1 hello") is None


# ------------------------------------------------------------------ actions: recorded as the human, prompt goes on
def test_answer_command_unblocks_and_goes_on(make_run):
    L = make_run()
    L.add_node("P1", "emulate the routine", kind="approach")
    q = L.ask("need_info", "Which emulator image?", node_id="P1.A1")["qid"]
    assert L.node("P1.A1")["status"] == "blocked"
    r = chat.handle(L, f"beacon: answer {q} use the arm64 image")
    assert r["block"] is False and "question #1" in r["show"] and "P1.A1 unblocked" in r["show"]
    assert "use the arm64 image" in r["note"]
    qrow = L.questions()[0]
    assert qrow["status"] == "answered" and qrow["answer"] == "use the arm64 image" and qrow["delivered"] == 1
    assert L.node("P1.A1")["status"] == "todo"
    assert L.verify_integrity()[0]
    ev = L.db.execute("SELECT actor, type FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    assert tuple(ev) == ("human", "chat_command")


def test_verify_accept_and_reject(make_run):
    L = make_run()
    _pending(L, "P1")
    r = chat.handle(L, "beacon: verify P1")
    assert r["block"] is False and L.node("P1")["status"] == "done"
    _pending(L, "P2")
    r = chat.handle(L, "beacon: verify P2 reject wrong format, needs braces")
    assert "rejected" in r["show"] and "wrong format, needs braces" in r["note"]
    assert L.node("P2")["status"] != "done"


def test_verify_never_guesses_accept(make_run):
    L = make_run()
    _pending(L, "P1")
    r = chat.handle(L, "beacon: verify P1 nope")
    assert r["block"] is True and "usage" in r["show"] and L.node("P1")["status"] == "pending_verification"


def test_bad_command_is_shown_not_raised(make_run):
    L = make_run()
    for prompt in ("beacon: verify P1", "beacon: answer 99 hi", "beacon: answer x", "beacon: reopen NOPE",
                   "beacon: verify"):
        r = chat.handle(L, prompt)
        assert r["block"] is True and r["show"].startswith("beacon:"), prompt


# ------------------------------------------------------------------ replies to questions
def test_hash_reply_answers_that_question(make_run):
    L = make_run()
    q1 = L.ask("need_info", "First question?")["qid"]
    q2 = L.ask("need_info", "Second question?")["qid"]
    r = chat.handle(L, f"#{q2}: the second answer")
    assert r["block"] is False and f"question #{q2}" in r["show"]
    assert {q["id"]: q["status"] for q in L.questions()} == {q1: "open", q2: "answered"}


def test_plain_reply_answers_only_when_claude_waits_and_one_is_open(make_run):
    L = make_run()
    L.ask("need_info", "Which port to use?")
    r = chat.handle(L, "port 8080", waiting_for_human=False)
    assert r["block"] is False and "start it with `#N`" in r["show"] and "Which port to use?" in r["note"]
    assert L.questions("open")
    r = chat.handle(L, "port 8080", waiting_for_human=True)
    assert "Recorded as the answer to question" in r["show"] and "write `#N" in r["show"]
    assert L.questions()[0]["answer"] == "port 8080"
    # two open questions: a plain reply is never guessed
    L.ask("need_info", "Question A here?")
    L.ask("need_info", "Question B here?")
    r = chat.handle(L, "yes", waiting_for_human=True)
    assert len(L.questions("open")) == 2 and "#2, #3" in r["show"]
    # slash commands, shell mode and a bare #N are never recorded as answers
    assert chat.handle(L, "/compact", waiting_for_human=True) is None
    assert chat.handle(L, "! ls", waiting_for_human=True) is None
    r = chat.handle(L, "#2", waiting_for_human=True)
    assert r["block"] is True and "write the answer after #2" in r["show"]
    assert len(L.questions("open")) == 2


# ------------------------------------------------------------------ the hook
def test_hook_views_block_and_actions_pass(make_run):
    L = make_run()
    _, out, err = run_hook("user-prompt-submit", {"session_id": "s", "prompt": "beacon: status"}, run=L.dir)
    obj = json.loads(out)
    assert obj["decision"] == "block" and "P1 [todo]" in obj["reason"], err
    q = L.ask("need_info", "Which key to try?")["qid"]
    _, out, _ = run_hook("user-prompt-submit", {"session_id": "s", "prompt": f"#{q} the key is 0x41"}, run=L.dir)
    obj = json.loads(out)
    assert "decision" not in obj and obj["systemMessage"].startswith("beacon ▸ Recorded")
    assert obj["hookSpecificOutput"]["additionalContext"].startswith("[beacon] The human answered question #")
    with open(os.path.join(L.dir, "chat.jsonl"), encoding="utf-8") as fh:
        assert len(fh.readlines()) == 2


def test_hook_uses_the_last_stop_of_this_session(make_run):
    L = make_run()
    L.ask("need_info", "Which key to try?")
    with open(os.path.join(L.dir, "decisions.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1, "session": "s", "action": "allow", "why": "awaiting_human"}) + "\n")
        fh.write(json.dumps({"ts": 2, "session": "other", "action": "block", "why": "open_work"}) + "\n")
    _, out, _ = run_hook("user-prompt-submit", {"session_id": "s", "prompt": "the key is 0x41"}, run=L.dir)
    assert "Recorded as the answer" in json.loads(out)["systemMessage"]


def test_hook_ignores_the_watchdogs_prompts(make_run):
    L = make_run()
    L.ask("need_info", "Which key to try?")
    rc, out, _ = run_hook("user-prompt-submit", {"session_id": "s", "prompt": "beacon: verify P1"}, run=L.dir,
                          extra_env={"BEACON_WATCHDOG": "1"})
    assert rc == 0 and out == ""


def test_hook_passes_through_without_a_run():
    rc, out, _ = run_hook("user-prompt-submit", {"session_id": "s", "prompt": "beacon: status"})
    assert rc == 0 and out == ""


# ------------------------------------------------------------------ guard: no self-submitted prompts
@pytest.mark.parametrize("tool", ["CronCreate", "ScheduleWakeup"])
def test_scheduled_prompts_are_refused(tool):
    assert "Scheduled prompts are disabled" in guard(tool, {"prompt": "beacon: verify P1"}, [], {})


@pytest.mark.parametrize("cmd", ["claude -p 'beacon: verify P1'", "cd /p && npx claude --resume abc",
                                 "echo x | /usr/local/bin/claude -p", "timeout 60 claude -p hi"])
def test_nested_claude_is_refused(cmd):
    assert "another Claude Code session" in guard("Bash", {"command": cmd}, [], {})


@pytest.mark.parametrize("cmd", ["cat ~/.claude/settings.json.bak", "grep -r claude docs/", "pip show claude-code"])
def test_mentions_of_claude_are_fine(cmd):
    r = guard("Bash", {"command": cmd}, [], {})
    assert r is None or "another Claude Code session" not in r


def test_stop_message_lists_what_waits_on_the_human(make_run):
    L = make_run([{"title": "Solve challenge one"}])
    _pending(L, "P1")
    _, out, _ = run_hook("stop", {"session_id": "s", "last_assistant_message": "candidate found"}, run=L.dir)
    msg = json.loads(out)["systemMessage"]
    assert "let the session stop" in msg and "P1 candidate: flag{x}" in msg and "beacon: verify P1" in msg


# ------------------------------------------------------------------ more commands
def test_show_report_and_unknown_node(make_run):
    L = make_run()
    L.add_node("P1", "emulate the decrypt routine", kind="approach")
    L.start("P1.A1")
    L.attach_evidence("P1.A1", "emulator ran to the decrypt loop", "trace ...")
    r = chat.handle(L, "beacon: show P1.A1")
    assert r["block"] and "P1.A1  [in_progress]" in r["show"] and "emulator ran to the decrypt loop" in r["show"]
    assert "children: P1.A1 [in_progress]" in chat.handle(L, "beacon: show P1")["show"]
    assert chat.handle(L, "beacon: show P9")["show"].startswith("beacon: no node")
    assert "Beacon report" in chat.handle(L, "beacon: report")["show"]


def test_add_and_abandon(make_run):
    L = make_run()
    r = chat.handle(L, "beacon: add P1 the key comes from the build timestamp")
    assert r["block"] is False and "added P1.S1" in r["show"] and "serious lead" in r["note"]
    n = L.node("P1.S1")
    assert n["source"] == "human" and n["kind"] == "solution" and n["priority"] == 5
    r = chat.handle(L, "beacon: add P1.S1 approach brute force the low 16 bits of the timestamp")
    assert "added P1.S1.A1" in r["show"]
    r = chat.handle(L, "beacon: abandon P2 out of scope for this week")
    assert L.node("P2")["status"] == "abandoned" and "Do not work on it" in r["note"]
    assert chat.handle(L, "beacon: abandon P1")["show"].startswith("beacon: usage")
    assert chat.handle(L, "beacon: add P2 something new here")["show"].startswith("beacon:")   # P2 is closed


def test_pause_resume_and_lighthouse_flags(make_run):
    L = make_run()
    r = chat.handle(L, "beacon: pause")
    assert r["block"] is True and L.human_flags()["paused"] is True
    assert "(PAUSED)" in chat.handle(L, "beacon: status")["show"]
    r = chat.handle(L, "beacon: resume")
    assert r["block"] is False and "paused" not in L.human_flags() and "Continue with" in r["note"]
    r = chat.handle(L, "beacon: lighthouse look again at the packer assumption")
    assert r["block"] is False and L.human_flags()["lighthouse_request"] == {"note": "look again at the packer assumption"}
    assert L.verify_integrity()[0]


def test_graph_command_writes_and_opens(make_run):
    L = make_run()
    opened = []
    r = chat.handle(L, "beacon: graph", opener=lambda url: opened.append(url) or True)
    path = os.path.join(L.dir, "graph.html")
    assert r["block"] and os.path.exists(path) and opened == ["file://" + path] and "Opened" in r["show"]
    r = chat.handle(L, "beacon: graph nopen", opener=lambda url: opened.append(url) or True)
    assert len(opened) == 1 and "Open it in a browser" in r["show"]
    r = chat.handle(L, "beacon: graph", opener=lambda url: 1 / 0)          # no browser: just the path
    assert "Open it in a browser" in r["show"]


def test_pick_overrides_the_order(make_run):
    L = make_run()
    L.ask("approach_choice", "emulate, hook, or read statically first?", node_id="P1",
          options=["emulate the decrypt routine", "hook it with Frida", "read the routine statically"])
    L.start("P1.A1")
    assert L.next_node()["id"] == "P1.A1"                    # Claude's first option, in progress
    r = chat.handle(L, "beacon: pick P1.A3")
    assert r["block"] is False and "P1.A3 goes next" in r["show"] and "start_node it" in r["note"]
    assert L.next_node()["id"] == "P1.A3"                    # the human's word goes first
    L.start("P1.A3")
    assert L.next_node()["id"] == "P1.A3"
    assert chat.handle(L, "beacon: pick P1")["show"].startswith("beacon: pick a solution")
    L.set_status("P1.A2", "dead_end", reason="frida cannot attach here", assumption="no anti-debug")
    assert "reopen it first" in chat.handle(L, "beacon: pick P1.A2")["show"]
    import pytest
    from beacon.ledger import LedgerError
    with pytest.raises(LedgerError):
        L.pick("P1.A1", actor="worker")


def test_decisions_view_lists_standing_approvals(make_run):
    from beacon import tools
    L = make_run()
    tools.ask("need_permission", "May I run strings on chal1/hide.py on Kali?", run=L.dir)
    out = chat.handle(L, "beacon: decisions")["show"]
    assert "Approved by your standing rule" in out and "May I run strings" in out


def test_claude_codes_own_turns_are_not_the_humans_reply(make_run):
    """Measured 2026-11-12 (Claude Code 2.1.322): a background task that finished arrived at UserPromptSubmit as
    "<task-notification>...</task-notification>". With one open question and Claude waiting, beacon would have
    recorded that text as the human's answer. It is now ignored; real replies still work."""
    L = make_run()
    qid = L.ask("need_info", "Which recoverer should the note describe?")["qid"]
    note = ("<task-notification>\n<task-id>b9jt293yx</task-id>\n<status>completed</status>\n"
            "<summary>Background command \"./scripts/check.sh full\" completed (exit code 0)</summary>\n"
            "</task-notification>")
    assert chat.handle(L, note, waiting_for_human=True) is None
    assert chat.handle(L, "  " + note + "\n", waiting_for_human=True) is None
    assert L.questions("open")[0]["id"] == qid
    r = chat.handle(L, "<b>recover_stackstrings</b>", waiting_for_human=True)   # a human may still type markup
    assert r is not None and not L.questions("open")


def test_verifying_says_which_questions_it_closed_and_a_late_reply_is_flagged(make_run):
    """Measured 2026-11-13: `beacon: verify P10` made question #8 obsolete silently; the human's later "#8 yes" went
    to Claude as if recorded. Now the verify reply lists what it closed, and a reply to a closed question says so."""
    L = make_run(problems=[{"title": "Add the regression test"}], k_stall=99, k_idle=99)
    a = L.add_node("P1", "write the tests", kind="approach")
    b = L.add_node("P1", "wire them into check.sh", kind="approach")
    L.start(a)
    e, _ = L.attach_evidence(a, "8 tests pass", content="8 passed")
    L.set_status(a, "done", evidence_ids=[e])
    L.start(b)
    qid = L.ask("need_info", "May I edit scripts/check.sh to add the new test to the map?", node_id=b)["qid"]
    L.propose_done("P1", "tests/test_x.py", [e])
    r = chat.handle(L, "beacon: verify P1")
    assert "candidate accepted" in r["show"] and f"#{qid} May I edit scripts/check.sh" in r["show"]
    late = chat.handle(L, f"#{qid} Có, thêm vào map")
    assert late["block"] is False and f"#{qid} is not open any more (obsolete" in late["show"]
    assert "treat the reply as their instruction" in late["note"]
    assert chat.handle(L, "#99 hello") is None                 # no such question: an ordinary message
