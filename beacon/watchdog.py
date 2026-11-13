# -*- coding: utf-8 -*-
"""beacon.watchdog — keep an unattended run alive.

The Stop gate can only act while Claude Code is running. When a turn ends on a
rate limit or an overloaded API, Claude Code fires StopFailure (whose output is
ignored) and the session is over. The watchdog sits outside Claude Code:

    while the ledger has open problems and the budgets allow:
        run the worker headless (claude -p ... --output-format stream-json)
        rate limit / overloaded / server error  -> wait (until the announced reset,
                                                   else exponential backoff), then
                                                   resume the same session
        auth / billing / bad config             -> stop: a human has to fix it
        the gate allowed the stop for a reason   -> stop and report (no open problems,
          only a human can move on (awaiting_human, budgets, lighthouse dry,
          possible refusal, integrity)
        the gate asked to rotate                 -> fresh session from the ledger
                                                   (long contexts degrade)

It uses the Claude Code CLI as a subprocess, the same engine the Agent SDK
drives, so beacon stays dependency-free and the gate, lighthouse and guards run
unchanged as the project's hooks. Never with --bare: bare mode skips the
project's hooks and .mcp.json, i.e. the whole supervisor.

Auth: the CLI uses whatever Claude Code is logged in with; ANTHROPIC_API_KEY in
the environment takes precedence. For long unattended automation an API key is
the clean path (see beacon/README.md).
"""
import datetime
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field

from .ledger import Ledger

FRESH_PROMPT = ("A beacon run supervises this session. Call ledger_view, then work the node the ledger "
                "names next. Record evidence and close nodes through the beacon tools.")
RESUME_PROMPT = ("The previous turn was cut off by an API error. Call ledger_view, then carry on with the "
                 "node you were working on.")

FATAL = {"auth", "billing", "config", "disk"}
RESUMABLE = {"rate_limit", "overloaded", "server_error", "timeout", "terminated"}
TRANSIENT = RESUMABLE | {"max_turns", "crash", "unfinished"}
# Measured on Claude Code 2.1.322: when it cannot read the organization's settings at startup
# it turns the user's hooks off for the session ("Hooks not running: ...") and denies every
# tool call a PreToolUse hook would have checked. The supervisor is then absent: say so.
_HOOKS_OFF = re.compile(r"hooks? (you set up )?aren't running|Hooks not running", re.I)


def hooks_disabled(notices):
    return next((n for n in notices or [] if _HOOKS_OFF.search(n)), None)


GATE_FINAL = {"no_open_problems", "awaiting_human", "hard_budget", "soft_budget", "lighthouse_dry",
              "lighthouse_exhausted", "possible_refusal", "integrity"}

# system/api_retry `error` categories -> our kinds
_API_CATEGORY = {
    "rate_limit": "rate_limit", "overloaded": "overloaded", "server_error": "server_error",
    "unknown": "server_error", "max_output_tokens": "server_error",
    "authentication_failed": "auth", "oauth_org_not_allowed": "auth", "account_on_hold": "auth",
    "cloud_credential_error": "auth", "billing_error": "billing",
    "invalid_request": "config", "model_not_found": "config",
}
_TEXT_KIND = [
    # Claude Code >= 2.1.314/317 prints these and exits 1 (it used to exit 0 silently): retrying cannot help
    (re.compile(r"no space left|disk (is )?full|ENOSPC|EDQUOT|quota exceeded", re.I), "disk"),
    (re.compile(r"unknown option|error: option|invalid (value|choice) for|unrecognized argument|failed to parse "
                r"(settings|config|\.mcp\.json)|could not start|bubblewrap is required", re.I), "config"),
    (re.compile(r"rate.?limit|usage limit|limit reached|\b429\b", re.I), "rate_limit"),
    (re.compile(r"overloaded|\b529\b", re.I), "overloaded"),
    (re.compile(r"credit balance|billing", re.I), "billing"),
    (re.compile(r"invalid api key|authenticat|unauthori[sz]ed|\b401\b|\b403\b|/login|oauth|auth login|"
                r"setup-token|credentials? (have |has )?expired", re.I), "auth"),
    (re.compile(r"\b5\d\d\b|server error|internal error|connection (reset|error|refused)", re.I), "server_error"),
]


@dataclass
class SessionResult:
    session_id: str = ""
    exit_code: int = 0
    is_error: bool = False
    error_kind: str = ""          # "" = clean end
    subtype: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    text: str = ""
    reset_at: float = 0.0         # epoch seconds of an announced rate-limit reset, 0 if none
    api_retries: int = 0
    notices: list = field(default_factory=list)   # system/informational messages from Claude Code
    credential_expires_at: float = 0.0            # epoch seconds, 0 if Claude Code did not say


def parse_reset(text, now=None):
    """Best effort: 'resets 3am', 'reset at 15:30', 'resets in 2h 15m' -> epoch seconds."""
    now = time.time() if now is None else now
    t = text or ""
    m = re.search(r"reset\w*\s+in\s+(?:(\d+)\s*h\w*)?\s*(?:(\d+)\s*m\w*)?", t, re.I)
    if m and (m.group(1) or m.group(2)):
        return now + 3600 * int(m.group(1) or 0) + 60 * int(m.group(2) or 0)
    m = (re.search(r"reset\w*\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t, re.I)
         or re.search(r"reset\w*\s+(?:at\s+)?(\d{1,2}):(\d{2})\b()", t, re.I))
    if not m:
        return 0.0
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return 0.0
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return 0.0
    local = datetime.datetime.fromtimestamp(now)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


def _epoch(value):
    """credential_expires_at as epoch seconds (number, numeric string or ISO-8601), else 0."""
    if value in (None, ""):
        return 0.0
    try:
        v = float(value)
        return v / 1000.0 if v > 1e12 else v       # milliseconds or seconds
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def classify_end(result, retry_errors, exit_code, stderr_tail="", timed_out=False, now=None):
    """Turn what a session left behind into a SessionResult.error_kind."""
    if timed_out:
        return "timeout"
    text = ((result or {}).get("result") or "") + "\n" + (stderr_tail or "")
    if result is not None and not result.get("is_error") and exit_code == 0:
        return ""
    if result is not None and result.get("subtype") == "error_max_turns":
        return "max_turns"
    if result is None and exit_code in (143, -15, 130, -2):
        return "terminated"
    if retry_errors:
        return _API_CATEGORY.get(retry_errors[-1], "server_error")
    for rx, kind in _TEXT_KIND:
        if rx.search(text):
            return kind
    if result is not None and exit_code == 0:
        return "unfinished"       # is_error without a recognisable cause
    return "crash"


def backoff(kind, attempt, reset_at=0.0, now=None):
    now = time.time() if now is None else now
    attempt = max(1, attempt)
    if kind == "rate_limit":
        if reset_at and reset_at > now:
            return reset_at - now + 60          # a minute of slack after the announced reset
        return min(3600.0, 300.0 * 2 ** (attempt - 1))
    if kind in ("overloaded", "server_error"):
        return min(900.0, 30.0 * 2 ** (attempt - 1))
    return min(600.0, 15.0 * 2 ** (attempt - 1))


class CliRunner:
    """One headless Claude Code session per run() call."""

    # After the Stop gate lets a headless session stop, Claude Code should exit at once. A background task keeps
    # it alive and wakes it with each "<task-notification>" (measured 2026-11-13: after soft_budget at 12:29 a
    # `find /` the worker had started kept the session running turns until 12:59). That is not a time limit on
    # the work: the gate already ended it. After this grace the session is stopped.
    END_GRACE_S = 120
    POLL_S = 2.0

    def __init__(self, cwd, run, claude="claude", permission_mode=None, no_prompts=False, model=None,
                 max_budget_usd=None, timeout_s=None, extra_args=(), beacon_env=True, end_grace_s=None):
        self.end_grace_s = self.END_GRACE_S if end_grace_s is None else float(end_grace_s)
        self.cwd, self.run_dir, self.claude, self.beacon_env = cwd, run, claude, beacon_env
        self.permission_mode, self.no_prompts, self.model = permission_mode, no_prompts, model
        self.max_budget_usd, self.timeout_s, self.extra_args = max_budget_usd, timeout_s, list(extra_args)
        self.proc = None
        self.counter = 0

    def command(self, prompt, resume=None):
        cmd = [self.claude, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if resume:
            cmd += ["--resume", resume]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.no_prompts:
            cmd += ["--permission-prompts", "none"]
        if self.model:
            cmd += ["--model", self.model]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return cmd + self.extra_args

    def run(self, prompt, resume=None):
        self.counter += 1
        logdir = os.path.join(self.run_dir, "sessions")
        os.makedirs(logdir, mode=0o700, exist_ok=True)
        stem = os.path.join(logdir, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{self.counter:03d}")
        env = dict(os.environ)
        if self.beacon_env:
            env.update(BEACON_RUN=self.run_dir, BEACON_WATCHDOG="1")
        else:  # an unsupervised (baseline) session must not inherit a run from our environment
            env.pop("BEACON_RUN", None)
            env.pop("BEACON_WATCHDOG", None)
        found = {"session_id": "", "result": None, "retries": [], "notices": [], "expires": None}

        def reader(stream, log):
            for line in stream:
                log.write(line)
                log.flush()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("session_id"):
                    found["session_id"] = ev["session_id"]
                if ev.get("type") == "system" and ev.get("subtype") == "api_retry":
                    found["retries"].append(str(ev.get("error") or "unknown"))
                elif ev.get("type") == "system" and ev.get("subtype") == "informational":
                    found["notices"].append(str(ev.get("content") or "")[:800])
                elif ev.get("type") == "result":
                    found["result"] = ev
                    if ev.get("credential_expires_at"):
                        found["expires"] = ev["credential_expires_at"]

        timed_out = False
        with open(stem + ".jsonl", "w", encoding="utf-8") as log, \
                open(stem + ".stderr", "w", encoding="utf-8") as err:
            # the prompt is not echoed in the stream; keep it so `beacon transcript` shows what was sent
            log.write(json.dumps({"type": "user", "beacon": "prompt", "resume": resume,
                                  "message": {"role": "user", "content": prompt}}, ensure_ascii=False) + "\n")
            log.flush()
            self.proc = subprocess.Popen(self.command(prompt, resume), cwd=self.cwd, env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                         text=True, encoding="utf-8", errors="replace",
                                         start_new_session=True)
            t = threading.Thread(target=reader, args=(self.proc.stdout, log), daemon=True)
            t.start()
            timed_out, gate_ended = self._wait(found)
            t.join(10)
            code = self.proc.returncode
            self.proc = None
        try:
            with open(stem + ".stderr", encoding="utf-8", errors="replace") as fh:
                stderr_tail = fh.read()[-2000:]
        except OSError:
            stderr_tail = ""
        res = found["result"]
        kind = "" if gate_ended else classify_end(res, found["retries"], code, stderr_tail, timed_out)
        if gate_ended:
            found["notices"].append(f"beacon: the Stop gate had let this session stop; it was still running "
                                    f"{self.end_grace_s:.0f} s later (background work), so beacon ended it")
        text = (res or {}).get("result") or stderr_tail
        return SessionResult(
            session_id=found["session_id"] or (res or {}).get("session_id", ""), exit_code=code if code is not None else -1,
            is_error=bool((res or {}).get("is_error")) or kind != "", error_kind=kind,
            subtype=(res or {}).get("subtype", ""), cost_usd=float((res or {}).get("total_cost_usd") or 0.0),
            num_turns=int((res or {}).get("num_turns") or 0), text=str(text)[-2000:],
            reset_at=parse_reset(text) if kind == "rate_limit" else 0.0, api_retries=len(found["retries"]),
            notices=found["notices"][:20], credential_expires_at=_epoch(found["expires"]))

    def _wait(self, found):
        """Wait for the session; -> (timed_out, gate_ended). Stops it on timeout_s (if set), or END_GRACE_S after
        the gate's latest "allow" for this session if it has not exited by then."""
        deadline = None if self.timeout_s is None else time.monotonic() + float(self.timeout_s)
        path = os.path.join(self.run_dir, "decisions.jsonl")
        try:
            offset = os.path.getsize(path)
        except OSError:
            offset = 0
        allowed_at = None
        while True:
            try:
                self.proc.wait(timeout=self.POLL_S)
                return False, False
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                self.stop()
                return True, False
            offset, allowed = _new_allow(path, offset, found.get("session_id") or "")
            if allowed:
                allowed_at = now                    # the latest allow counts: each wake-up gets the full grace
            if allowed_at is not None and now - allowed_at >= self.end_grace_s:
                self.stop()
                return False, True

    def stop(self):
        """SIGINT first (Claude Code ends the turn cleanly), then SIGTERM, then SIGKILL."""
        p = self.proc
        if p is None or p.poll() is not None:
            return
        for sig, wait in ((signal.SIGINT, 30), (signal.SIGTERM, 30)):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    p.send_signal(sig)
                except OSError:
                    return
            try:
                p.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.wait()


def _new_allow(path, offset, session_id):
    """Read decisions.jsonl from `offset`: -> (new offset, did the gate let `session_id` stop?)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return offset, False
    end = data.rfind(b"\n") + 1                 # a half-written last line is read next time
    allowed = False
    for line in data[:end].splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("action") == "allow" and (not session_id or d.get("session") == session_id):
            allowed = True
    return offset + end, allowed


def _last_gate(L, since_seq):
    ev = L.events(since_seq=since_seq, etype="gate")
    return json.loads(ev[-1]["payload"]) if ev else None


def watch(run, runner, *, max_sessions=20, max_hours=None, max_cost_usd=None, max_transient=12,
          max_wait_s=None, clock=time.time, sleep=time.sleep, log=print):
    """Returns {"why", "sessions", "cost_usd", "detail"}. No time or cost limit by default (the human's choice):
    max_hours / max_cost_usd / max_wait_s = None means none; a rate-limit wait lasts until the announced reset."""
    from .gate import Decision
    from .hooks import _notify
    from .report import write_report

    L = Ledger.open(run)
    started, sessions, transient, no_gate = clock(), 0, 0, 0
    warned = {}
    costs, resume = {}, None

    def jlog(obj):
        obj = dict(obj, ts=clock())
        try:
            with open(os.path.join(L.dir, "watch.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def end(why, detail=""):
        integrity = L.verify_integrity()
        path = write_report(L, Decision("allow", f"watchdog:{why}", report=detail), integrity)
        if integrity[0]:
            _notify(L, f"watch:{why}", path)
        out = {"why": why, "sessions": sessions, "cost_usd": round(sum(costs.values()), 4), "detail": detail}
        jlog({"event": "end", **out})
        log(f"[beacon watch] stopped: {why} after {sessions} session(s). Report: {path}")
        L.close()
        return out

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("[beacon watch] note: no ANTHROPIC_API_KEY in the environment; the worker uses the Claude Code "
            "login, i.e. your subscription limits.")
    try:
        while True:
            ok, why = L.verify_integrity()
            if not ok:
                return end("integrity", why)
            if not L.open_problems():
                return end("no_open_problems")
            if sessions >= max_sessions:
                return end("max_sessions")
            if max_hours and clock() - started > max_hours * 3600:
                return end("max_hours")
            if max_cost_usd and sum(costs.values()) >= max_cost_usd:
                return end("max_cost", f"estimated ${sum(costs.values()):.2f}")
            seq0 = L.last_seq()
            sessions += 1
            log(f"[beacon watch] session {sessions}" + (f" (resuming {resume})" if resume else " (fresh)"))
            res = runner.run(RESUME_PROMPT if resume else FRESH_PROMPT, resume=resume)
            if res.session_id:  # a resumed session reports the conversation's running total
                costs[res.session_id] = max(costs.get(res.session_id, 0.0), res.cost_usd)
            gate = _last_gate(L, seq0)
            jlog({"event": "session", "n": sessions, "resume": resume, "result": asdict(res), "gate": gate})
            exp = getattr(res, "credential_expires_at", 0.0) or 0.0
            if exp and exp - clock() < 3 * 86400 and not warned.get("expiry"):
                warned["expiry"] = True
                msg = (f"Claude Code login expires in {max(0.0, (exp - clock()) / 3600):.0f} h: run `claude auth login` "
                       "(or `claude setup-token`) before an unattended run stops on it.")
                log(f"[beacon watch] warning: {msg}")
                jlog({"event": "warning", "kind": "credential_expiry", "detail": msg})
                if L.verify_integrity()[0]:
                    _notify(L, "watch:credential_expiry")
            if res.error_kind in FATAL:
                return end(f"fatal_{res.error_kind}", res.text[-500:])
            off = hooks_disabled(res.notices)
            if off and gate is None:
                return end("hooks_disabled", "Claude Code did not run the hooks in this session, so the beacon "
                                             "supervisor was absent. Its notice: " + off[:500])
            if gate and gate.get("action") == "allow":
                transient, no_gate = 0, 0
                if gate.get("why") == "rotate":
                    resume = None
                    continue
                return end(f"gate_{gate.get('why')}")
            if res.error_kind in TRANSIENT:
                transient += 1
                if transient > max_transient:
                    return end("too_many_failures", f"{transient} consecutive failures, last: {res.error_kind}")
                wait = backoff(res.error_kind, transient, res.reset_at, clock())
                if max_wait_s and wait > max_wait_s:
                    return end("wait_too_long", f"{res.error_kind}: next attempt in {wait / 3600:.1f} h")
                log(f"[beacon watch] {res.error_kind}: waiting {wait:.0f} s")
                jlog({"event": "wait", "kind": res.error_kind, "seconds": wait})
                sleep(wait)
                resume = res.session_id if (res.error_kind in RESUMABLE and res.session_id) else None
                continue
            transient = 0
            # A clean end that the gate never allowed: the Stop hook is not running.
            no_gate = no_gate + 1 if gate is None else 0
            if no_gate >= 2:
                return end("no_gate", "Two sessions ended without the Stop gate deciding. Check the hooks "
                                      "(`beacon hooks-config`) and that BEACON_RUN reaches them.")
            resume = None
    except KeyboardInterrupt:
        stop = getattr(runner, "stop", None)
        if stop:
            stop()
        return end("interrupted")
