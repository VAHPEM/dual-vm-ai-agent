import datetime
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from beacon.watchdog import (FRESH_PROMPT, RESUME_PROMPT, CliRunner, SessionResult, backoff, classify_end,
                             parse_reset, watch)


# ------------------------------------------------------------------ helpers
def gate_says(L, action, why):
    L.save_gate(L.gate_state(), {"action": action, "why": why})


class FakeRunner:
    def __init__(self, L, steps):
        self.L, self.steps, self.calls, self.stopped = L, list(steps), [], False

    def run(self, prompt, resume=None):
        self.calls.append((prompt, resume))
        return self.steps.pop(0)(self.L)

    def stop(self):
        self.stopped = True


def allow(why, sid="s1", cost=0.0):
    def step(L):
        gate_says(L, "allow", why)
        return SessionResult(session_id=sid, cost_usd=cost)
    return step


def block_then_clean(sid="s1"):
    def step(L):
        gate_says(L, "block", "open_work")
        return SessionResult(session_id=sid)
    return step


def failing(kind, sid="s1", reset_at=0.0, cost=0.0):
    def step(L):
        return SessionResult(session_id=sid, is_error=True, error_kind=kind, reset_at=reset_at, cost_usd=cost)
    return step


def run_watch(L, steps, **kw):
    sleeps = []
    clock = kw.pop("clock", lambda: 1_000_000.0)
    r = FakeRunner(L, steps)
    out = watch(L.dir, r, clock=clock, sleep=sleeps.append, log=lambda *_: None, **kw)
    return out, r, sleeps


# ------------------------------------------------------------------ the loop
def test_rate_limit_waits_for_reset_then_resumes(make_run):
    L = make_run()
    now = 1_000_000.0
    out, r, sleeps = run_watch(L, [failing("rate_limit", reset_at=now + 1800), allow("awaiting_human")],
                               clock=lambda: now)
    assert out["why"] == "gate_awaiting_human"
    assert sleeps == [1860.0]
    assert r.calls[0] == (FRESH_PROMPT, None) and r.calls[1] == (RESUME_PROMPT, "s1")


def test_rotate_starts_fresh(make_run):
    L = make_run()
    out, r, _ = run_watch(L, [allow("rotate", sid="s1"), allow("lighthouse_dry", sid="s2")])
    assert out["why"] == "gate_lighthouse_dry"
    assert [c[1] for c in r.calls] == [None, None]


def test_fatal_errors_stop_immediately(make_run):
    L = make_run()
    out, r, sleeps = run_watch(L, [failing("auth")])
    assert out["why"] == "fatal_auth" and len(r.calls) == 1 and not sleeps
    assert os.path.exists(os.path.join(L.dir, "REPORT.md"))


def test_missing_hooks_are_detected(make_run):
    L = make_run()
    clean = lambda L: SessionResult(session_id="x")  # noqa: E731
    out, r, _ = run_watch(L, [clean, clean, clean])
    assert out["why"] == "no_gate" and len(r.calls) == 2


def test_budgets(make_run):
    L = make_run()
    out, r, _ = run_watch(L, [block_then_clean()] * 5, max_sessions=3)
    assert out["why"] == "max_sessions" and len(r.calls) == 3
    out, r, _ = run_watch(L, [failing("crash")] * 5, max_transient=2)
    assert out["why"] == "too_many_failures" and len(r.calls) == 3


def test_cost_counts_resumed_totals_once(make_run):
    L = make_run()
    steps = [failing("overloaded", sid="s1", cost=2.0), failing("overloaded", sid="s1", cost=3.0),
             failing("overloaded", sid="s2", cost=4.0)]
    out, r, _ = run_watch(L, steps, max_cost_usd=6.5)
    assert out["why"] == "max_cost" and out["cost_usd"] == 7.0


def test_wait_too_long(make_run):
    L = make_run()
    out, _, sleeps = run_watch(L, [failing("rate_limit", reset_at=1_000_000.0 + 10 * 3600)],
                               max_wait_s=6 * 3600)
    assert out["why"] == "wait_too_long" and not sleeps


def test_stops_when_everything_is_done(make_run):
    L = make_run([{"title": "Only problem", "status": "done"}])
    out, r, _ = run_watch(L, [])
    assert out["why"] == "no_open_problems" and not r.calls


def test_interrupt(make_run):
    L = make_run()

    def boom(L):
        raise KeyboardInterrupt

    out, r, _ = run_watch(L, [boom])
    assert out["why"] == "interrupted" and r.stopped


# ------------------------------------------------------------------ pieces
def test_parse_reset():
    base = datetime.datetime(2026, 11, 11, 22, 0).timestamp()
    t = parse_reset("Claude usage limit reached. Your limit will reset at 3am (Asia/Saigon).", base)
    assert datetime.datetime.fromtimestamp(t) == datetime.datetime(2026, 11, 12, 3, 0)
    t = parse_reset("limit reached, resets 11:30 pm", base)
    assert datetime.datetime.fromtimestamp(t) == datetime.datetime(2026, 11, 11, 23, 30)
    assert parse_reset("resets in 2h 15m", base) == base + 2 * 3600 + 15 * 60
    assert parse_reset("resets 12am", base) == datetime.datetime(2026, 11, 12, 0, 0).timestamp()
    assert parse_reset("no reset information here", base) == 0.0
    assert parse_reset("resets 25:00", base) == 0.0


def test_classify_end():
    assert classify_end({"is_error": False}, [], 0) == ""
    assert classify_end({"is_error": True, "subtype": "error_max_turns"}, [], 1) == "max_turns"
    assert classify_end({"is_error": True, "result": "x"}, ["rate_limit"], 1) == "rate_limit"
    assert classify_end({"is_error": True, "result": "Invalid API key · Please run /login"}, [], 1) == "auth"
    assert classify_end({"is_error": True, "result": "Credit balance is too low"}, [], 1) == "billing"
    assert classify_end(None, [], 143) == "terminated"
    assert classify_end(None, ["authentication_failed"], 1) == "auth"
    assert classify_end(None, [], 1, stderr_tail="Segmentation fault") == "crash"
    assert classify_end(None, [], 0, timed_out=True) == "timeout"


def test_backoff_shapes():
    assert backoff("rate_limit", 1) == 300 and backoff("rate_limit", 10) == 3600
    assert backoff("overloaded", 1) == 30 and backoff("overloaded", 10) == 900
    assert backoff("rate_limit", 1, reset_at=100.0, now=50.0) == 110.0


# ------------------------------------------------------------------ the real runner, against a fake CLI
FAKE_CLAUDE = textwrap.dedent('''\
    import json, os, sys, time
    out = os.environ["FAKE_OUT"]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "run": os.environ.get("BEACON_RUN"),
                   "watchdog": os.environ.get("BEACON_WATCHDOG"), "cwd": os.getcwd()}, fh)
    mode = os.environ.get("FAKE_MODE", "ok")
    emit = lambda o: print(json.dumps(o), flush=True)
    emit({"type": "system", "subtype": "init", "session_id": "sess-123"})
    if mode == "ok":
        emit({"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-123",
              "total_cost_usd": 0.12, "num_turns": 3, "result": "done"})
    elif mode == "ratelimit":
        emit({"type": "system", "subtype": "api_retry", "error": "rate_limit", "attempt": 1})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "sess-123",
              "result": "Claude usage limit reached. Your limit will reset at 3am"})
        sys.exit(1)
    elif mode == "hang":
        time.sleep(60)
''')


@pytest.fixture
def fake_claude(tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_cli_runner_ok_and_args(make_run, fake_claude, tmp_path, monkeypatch):
    L = make_run()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "seen.json"))
    monkeypatch.setenv("FAKE_MODE", "ok")
    r = CliRunner(cwd=str(proj), run=L.dir, claude=fake_claude, permission_mode="acceptEdits",
                  no_prompts=True, model="opus", max_budget_usd=5)
    res = r.run(FRESH_PROMPT, resume="old-session")
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert res.error_kind == "" and res.session_id == "sess-123" and res.cost_usd == 0.12
    assert seen["run"] == L.dir and seen["watchdog"] == "1" and os.path.samefile(seen["cwd"], proj)
    argv = seen["argv"]
    assert argv[:2] == ["-p", FRESH_PROMPT] and "--bare" not in argv
    for flag, val in (("--output-format", "stream-json"), ("--resume", "old-session"),
                      ("--permission-mode", "acceptEdits"), ("--permission-prompts", "none"),
                      ("--model", "opus"), ("--max-budget-usd", "5")):
        assert argv[argv.index(flag) + 1] == val
    assert "--verbose" in argv
    logs = os.listdir(os.path.join(L.dir, "sessions"))
    assert any(f.endswith(".jsonl") for f in logs)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_cli_runner_rate_limit(make_run, fake_claude, tmp_path, monkeypatch):
    L = make_run()
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "seen.json"))
    monkeypatch.setenv("FAKE_MODE", "ratelimit")
    res = CliRunner(cwd=str(tmp_path), run=L.dir, claude=fake_claude).run(FRESH_PROMPT)
    assert res.error_kind == "rate_limit" and res.api_retries == 1 and res.reset_at > time.time()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_cli_runner_timeout_kills_the_session(make_run, fake_claude, tmp_path, monkeypatch):
    L = make_run()
    monkeypatch.setenv("FAKE_OUT", str(tmp_path / "seen.json"))
    monkeypatch.setenv("FAKE_MODE", "hang")
    t0 = time.time()
    res = CliRunner(cwd=str(tmp_path), run=L.dir, claude=fake_claude, timeout_s=1).run(FRESH_PROMPT)
    assert res.error_kind == "timeout" and time.time() - t0 < 40


def test_hooks_disabled_notice_stops_with_a_precise_reason(make_run):
    L = make_run()
    notice = ("Mods and hooks you set up aren't running in this session ... Hooks not running: "
              "PostToolUse, PreToolUse, SessionStart, Stop, StopFailure.")
    off = lambda L: SessionResult(session_id="x", notices=[notice])  # noqa: E731
    out, r, _ = run_watch(L, [off, off])
    assert out["why"] == "hooks_disabled" and len(r.calls) == 1 and "Hooks not running" in out["detail"]


def test_no_time_or_cost_limit_by_default(make_run):
    """By default watch has no hour, cost or wait limit: a long run, an expensive one and a long rate-limit wait
    all go on until the gate decides."""
    L = make_run()
    t = [1_000_000.0]

    def clock():
        t[0] += 24 * 3600          # every look at the clock: a day later
        return t[0]
    steps = [failing("overloaded", cost=500.0), failing("rate_limit", reset_at=10 ** 12), allow("no_open_problems")]
    out, r, sleeps = run_watch(L, steps, clock=clock)
    assert out["why"] == "gate_no_open_problems" and len(r.calls) == 3
    assert any(s > 6 * 3600 for s in sleeps)            # waited for the announced reset, however long


def test_cli_watch_defaults_set_no_limit():
    from beacon.cli import build_parser
    a = build_parser().parse_args(["watch", "--cwd", "."])
    assert (a.max_hours, a.max_cost_usd, a.max_wait_hours, a.session_budget_usd, a.session_timeout_min) == \
        (None, None, None, None, None)
