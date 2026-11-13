# -*- coding: utf-8 -*-
"""beacon.ledger — the single source of truth for a run.

A run is one checklist. Its ledger is a SQLite file holding a three-level graph

    problem  (from the checklist)        P1
      solution / hypothesis              P1.S1
        approach (a way to test it)      P1.S1.A1   (or directly P1.A1)

plus evidence attached to nodes, questions for the human, and an append-only
event log. Every mutation goes through `_mutate`, which (inside one
BEGIN IMMEDIATE transaction) applies the change, recomputes a digest of the
whole state and appends a hash-chained event. `verify_integrity()` recomputes
the chain and compares the last digest with the live state, so an edit made
behind the ledger's back (sqlite3 on the file, a script) is detected.

Tamper-*evident*, not tamper-proof: the worker runs as the same OS user. The
PreToolUse guard (beacon.hooks) blocks the obvious routes; the digest catches
the rest after the fact.

Rules enforced here (not in the prompt, so the worker cannot talk its way past
them):
  * a node is never deleted; terminal states are terminal for the worker
    (only the human reopens);
  * `done` on a solution/approach needs at least one non-duplicate evidence
    item in its subtree; a problem is only `done` through verification
    (a verify_cmd from the checklist, or the human);
  * `dead_end` needs a reason AND the assumption it rests on;
  * evidence is de-duplicated by content hash: re-running a command and
    attaching the same output does not count as new evidence.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time

SCHEMA_VERSION = 1
GENESIS = "0" * 64

KINDS = ("problem", "solution", "approach")
PROBLEM_STATUSES = ("todo", "in_progress", "pending_verification", "done",
                    "blocked", "abandoned", "proposed")
# "proposed": a problem the worker found and added (propose_problem). It is not open -- the gate
# does not push on it -- until the human accepts it (the checklist is the human's scope).
PROBLEM_APPROVAL = ("human", "auto")
PERMISSION_POLICIES = ("approve", "ask")
NODE_STATUSES = ("todo", "in_progress", "done", "dead_end", "blocked",
                 "superseded", "abandoned")
OPEN = ("todo", "in_progress")
# A node in one of these takes no new children and the worker cannot leave it.
CLOSED = ("done", "dead_end", "superseded", "abandoned")
QUESTION_KINDS = ("approach_choice", "need_info", "need_permission")
# How the worker picks between approaches without asking anyone (the human's standing rule:
# effectiveness first, then cost). Used by the ask tool, the Stop gate's nudge and WORKER.md.
CHOICE_RULE = ("effectiveness first, then cost: take the option most likely to reach the answer; if a quick "
               "check would tell which option works, do that check first; between options with similar "
               "chances, take the cheaper one")
HUMAN_PICK = 100      # priority of a node the human picked (`beacon: pick`): it goes before everything
DECIDER_TOP = 60      # the decider's first choice; its ranking counts down from here (below a human pick)
ACTORS = ("worker", "lighthouse", "human", "gate", "verifier", "checklist", "decider")

MIN_REASON = 8          # characters; a one-word reason is not a reason
MAX_TEXT = 4000         # per free-text field
EXCERPT_HEAD, EXCERPT_TAIL = 1500, 500

DEFAULT_CONFIG = {
    "soft_forced": 30,           # blocks before the soft budget is reached
    "hard_forced": 40,           # never exceeded, never auto-extended
    "k_stall": 4,                # stops without a conclusion -> lighthouse
    "k_idle": 2,                 # stops without any new evidence -> lighthouse
    "max_lighthouse_calls": 20,
    "lighthouse_dry_max": 2,     # consecutive lighthouse rounds that add nothing
    "near_done_extension": 0.2,  # one-time soft-budget extension
    "near_done_claims_max": 3,   # claims without new evidence -> audit
    "near_done_grace": 6,        # stops that near-done evidence may stand in for a conclusion
    "lighthouse_model": "claude-opus-5-5",
    # Opus 5.5 always thinks and its thinking counts toward max_tokens: the limits are generous
    # (you pay for what is used, not the limit) and effort sets how hard it thinks.
    "lighthouse_max_tokens": 16000,
    "lighthouse_effort": "high",
    "lighthouse_timeout": 180,
    "lighthouse_transcript": "observations",   # observations | full | off: how much of the session it reads
    "lighthouse_transcript_chars": 60000,       # so it can review the steps the worker took, recorded or not
    "no_web": False,             # PreToolUse denies WebSearch/WebFetch
    "verify_timeout": 100,       # runs inside the propose_done tool call: stay under Claude Code's 2-minute mark
    "max_wait_minutes": 120,
    "notify_cmd": "",            # run (shell) when the gate lets the run stop
    "rotate_every": 15,          # under the watchdog: forced continuations per session, then a fresh one
    "block_internal_tasks": True,  # deny TodoWrite/TaskCreate/TaskUpdate: the ledger is the task list
    "classifier_model": "claude-opus-5-5",   # reads a question left as prose at a stop; decides choices
    "classifier_max_tokens": 8000,
    "classifier_effort": "medium",
    "max_classifier_calls": 100,
    "classifier_min_confidence": 0.6,
    "classifier_timeout": 90,
    # A need_permission request that runs a file it does not vouch for is checked for a real malware sample
    # (ask_before_running_real_samples) by the classifier model at low effort.
    "sample_check_effort": "low",
    "sample_check_max_tokens": 4000,
    "sample_check_timeout": 45,
    "permission_mode": "",       # how `beacon claude` / `beacon watch` start the worker ("" = Claude Code's default)
    "decider_enabled": True,     # a branch (ask approach_choice) is decided by the decider model
    "decider_model": "claude-opus-5-5",
    "decider_max_tokens": 16000,
    "decider_effort": "high",
    "decider_timeout": 100,      # under Claude Code's 2-minute mark, after which a tool call moves to the background
    "max_decider_calls": 60,
    "decider_transcript": "observations",   # observations | full | off: how much of the session it reads
    "decider_transcript_chars": 80000,
    "permission_policy": "approve",  # approve: the worker's requests to run things are granted (standing rule); ask
    # The human's decision (2026-11-13): running a binary, a real sample included, is approved like any other
    # request. True sends a request to execute a real malware sample to the human instead.
    "ask_before_running_real_samples": False,
    "problem_approval": "auto",   # auto: a problem the worker adds is open at once; human: it waits for you
    # Work the worker lists as not done when it ends a turn becomes nodes it must do (beacon.followups).
    "followup_policy": "work",   # work: do it now; propose: the human accepts it first; off
    "followup_model": "claude-opus-5-5",
    "followup_max_tokens": 12000,
    "followup_effort": "medium",
    "followup_timeout": 90,
    "max_followup_calls": 100,    # at most one per stop, each distinct message once (12 ran out in a 25-stop run)
    "followup_max_items": 8,     # per stop
    "followup_max_total": 40,    # per run: a worker that finds new gaps forever is stopped here
    "followup_autoverify": True,  # a follow-up problem closes once every item is closed with evidence
    "worker_capabilities": "",   # what the worker can reach ("" = the default lab description)
    "stop_budget_seconds": 270,  # every model call of one stop together (Claude Code kills the Stop hook at 300 s)
}

FOLLOWUP_POLICIES = ("work", "propose", "off")

PERMISSION_MODES = ("", "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")

DEFAULT_GATE = {
    "stops": 0, "forced": 0, "stall": 0, "idle": 0,
    "last_progress": 0, "last_evidence": 0, "last_evidence_seq": 0,
    "lighthouse_calls": 0, "lighthouse_dry": 0,
    "extension_used": False, "soft_cap": None, "near_grace": 0,
    "session_forced": {}, "activity_offset": 0, "activity_seen": [],
    "sessions": [], "last_decision": "",
    "followup_seen": [], "followup_calls": 0, "leftover_nudges": 0, "leftovers_pending": False,
}


def _closed_problem_msg(pid, status, actor):
    if actor == "human":
        return f"problem {pid} is {status}: reopen it first (`beacon: reopen {pid} [note]`), or add the idea under an open problem"
    return (f"problem {pid} is {status}: new work goes under an open problem, or propose_problem for work that is "
            "its own")


class LedgerError(Exception):
    """A rule was violated. The message is meant for the worker to read."""


def _now():
    return time.time()


def _clip(text, limit=MAX_TEXT):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + " …[clipped]"


def _excerpt(text):
    text = "" if text is None else str(text)
    if len(text) <= EXCERPT_HEAD + EXCERPT_TAIL:
        return text
    return text[:EXCERPT_HEAD] + "\n…[snip]…\n" + text[-EXCERPT_TAIL:]


def _norm_title(title):
    return re.sub(r"[^0-9a-z]+", "", (title or "").lower())


def _origin_tag(rationale):
    m = re.match(r"origin: (P\d+|-)(?=\s|$)", rationale or "")
    return m.group(1) if m else None


def db_path(run):
    """A run is a directory; the ledger lives in <run>/ledger.db."""
    run = os.path.abspath(os.path.expanduser(run))
    return run if run.endswith(".db") else os.path.join(run, "ledger.db")


def run_dir(run):
    return os.path.dirname(db_path(run))


_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE nodes(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent TEXT,
    title TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    assumption TEXT NOT NULL DEFAULT '', superseded_by TEXT,
    verify_cmd TEXT, candidate TEXT,
    remaining TEXT NOT NULL DEFAULT '[]',
    near_done_claims INTEGER NOT NULL DEFAULT 0,
    near_done_evidence INTEGER NOT NULL DEFAULT 0,
    blocked_by_question INTEGER,
    ordinal INTEGER NOT NULL,
    created_seq INTEGER NOT NULL, updated_seq INTEGER NOT NULL);
CREATE TABLE evidence(
    id INTEGER PRIMARY KEY, node_id TEXT NOT NULL, kind TEXT NOT NULL,
    summary TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
    duplicate_of INTEGER, seq INTEGER NOT NULL);
CREATE TABLE questions(
    id INTEGER PRIMARY KEY, node_id TEXT, kind TEXT NOT NULL,
    question TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL);
CREATE TABLE events(
    seq INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL,
    type TEXT NOT NULL, payload TEXT NOT NULL, state_digest TEXT NOT NULL,
    prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""

_DIGEST_TABLES = (("meta", "key"), ("nodes", "id"), ("evidence", "id"),
                  ("questions", "id"))


def _event_hash(prev, seq, ts, actor, etype, payload, digest):
    blob = "|".join([prev, str(seq), repr(ts), actor, etype, payload, digest])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_verify(cmd, candidate, cwd, timeout=120):
    """Run a checklist verify command. The candidate reaches it ONLY through the
    BEACON_CANDIDATE environment variable, never the command line. -> (ok, detail)"""
    from .shellrun import run
    env = dict(os.environ)
    env["BEACON_CANDIDATE"] = candidate
    try:
        r = run(cmd, cwd=cwd, env=env, timeout=timeout)       # sh, or Git Bash on Windows (as Claude Code)
    except subprocess.TimeoutExpired:
        return False, "verify_cmd timed out"
    except OSError as e:
        return False, f"verify_cmd could not run: {e}"
    tail = ((r.stdout or "") + (r.stderr or ""))[-1500:]
    return r.returncode == 0, f"exit {r.returncode}: {tail.strip()}"


class Ledger:
    """Open with Ledger.open(run) or Ledger.create(...). Cheap to construct;
    every hook invocation opens its own instance."""

    def __init__(self, path):
        self.path = db_path(path)
        self.dir = os.path.dirname(self.path)
        if not os.path.exists(self.path):
            raise LedgerError(f"no ledger at {self.path}")
        self.db = self._connect(self.path)
        ver = self._meta_get("schema_version")
        if ver != SCHEMA_VERSION:
            raise LedgerError(f"ledger schema {ver!r} != supported {SCHEMA_VERSION}")

    # ------------------------------------------------------------ plumbing
    @staticmethod
    def _connect(path):
        db = sqlite3.connect(path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=OFF")
        return db

    @classmethod
    def open(cls, run):
        return cls(run)

    @classmethod
    def create(cls, run, title, problems, config=None, workdir=None):
        """problems: list of {"title", "rationale"?, "verify_cmd"?, "status"?}."""
        path = db_path(run)
        if os.path.exists(path):
            raise LedgerError(f"ledger already exists at {path}")
        if not problems:
            raise LedgerError("a run needs at least one problem")
        for i, p in enumerate(problems, 1):
            if not isinstance(p, dict) or len(str(p.get("title") or "").strip()) < 3:
                raise LedgerError(f"problem #{i} needs a title")
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(config or {})
        if cfg.get("permission_mode") not in PERMISSION_MODES:
            raise LedgerError(f"permission_mode must be one of {PERMISSION_MODES[1:]} (or empty)")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        db = cls._connect(path)
        db.executescript(_SCHEMA)
        db.execute("INSERT INTO meta VALUES('schema_version', ?)", (json.dumps(SCHEMA_VERSION),))
        db.close()
        led = cls(path)

        def apply(cur, seq):
            for key, val in (("title", title), ("config", cfg), ("gate", dict(DEFAULT_GATE)),
                             ("workdir", os.path.abspath(workdir or os.getcwd())),
                             ("progress", 0), ("unique_evidence", 0), ("waiting", None),
                             ("flags", [])):
                cur.execute("INSERT INTO meta VALUES(?, ?)", (key, json.dumps(val)))
            for i, p in enumerate(problems, 1):
                status = p.get("status", "todo")
                if status not in ("todo", "done"):
                    raise LedgerError(f"checklist status must be todo or done, got {status!r}")
                cur.execute(
                    "INSERT INTO nodes(id, kind, parent, title, rationale, status, priority,"
                    " source, reason, verify_cmd, ordinal, created_seq, updated_seq)"
                    " VALUES(?, 'problem', NULL, ?, ?, ?, 0, 'checklist', ?, ?, ?, ?, ?)",
                    (f"P{i}", _clip(p["title"], 500), _clip(p.get("rationale", "")), status,
                     "checked in the input checklist" if status == "done" else "",
                     p.get("verify_cmd") or None, i, seq, seq))
            return {"title": title, "problems": len(problems)}

        led._mutate("checklist", "create", apply)
        return led

    def close(self):
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass

    def _meta_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    @staticmethod
    def _meta_set(cur, key, value):
        cur.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)))

    @staticmethod
    def _meta_read(cur, key, default=None):
        row = cur.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def _bump(self, cur, key, by=1):
        self._meta_set(cur, key, int(self._meta_read(cur, key, 0)) + by)

    @staticmethod
    def _digest(cur):
        h = hashlib.sha256()
        for table, key in _DIGEST_TABLES:
            for row in cur.execute(f"SELECT * FROM {table} ORDER BY {key}"):
                h.update(json.dumps(list(row), separators=(",", ":"),
                                    ensure_ascii=False, default=str).encode("utf-8"))
                h.update(b"\n")
            h.update(table.encode() + b"\x00")
        return h.hexdigest()

    def _mutate(self, actor, etype, fn):
        """Run fn(cur, seq) inside one write transaction, then append the
        hash-chained event. fn returns the event payload (a dict)."""
        if actor not in ACTORS:
            raise LedgerError(f"unknown actor {actor!r}")
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            last = cur.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last[0] + 1) if last else 1
            prev = last[1] if last else GENESIS
            payload = fn(cur, seq) or {}
            payload_s = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
            digest = self._digest(cur)
            ts = _now()
            cur.execute("INSERT INTO events VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (seq, ts, actor, etype, payload_s, digest, prev,
                         _event_hash(prev, seq, ts, actor, etype, payload_s, digest)))
            cur.execute("COMMIT")
            return payload
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ integrity
    def verify_integrity(self):
        """(ok, message). Recomputes the hash chain and checks that the last
        event's digest matches the current state."""
        prev, last_digest, n = GENESIS, None, 0
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            n += 1
            if row["seq"] != n:
                return False, f"event sequence gap at {row['seq']} (expected {n})"
            if row["prev_hash"] != prev:
                return False, f"broken chain at event {row['seq']}"
            want = _event_hash(prev, row["seq"], row["ts"], row["actor"], row["type"],
                               row["payload"], row["state_digest"])
            if row["hash"] != want:
                return False, f"event {row['seq']} was altered"
            prev, last_digest = row["hash"], row["state_digest"]
        if n == 0:
            return False, "no events"
        live = self._digest(self.db.cursor())
        if live != last_digest:
            return False, "state changed outside the ledger (digest mismatch)"
        return True, f"ok ({n} events)"

    # ------------------------------------------------------------ reading
    def config(self):
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(self._meta_get("config", {}) or {})
        return cfg

    def title(self):
        return self._meta_get("title", "")

    def workdir(self):
        return self._meta_get("workdir", self.dir)

    def counter(self, key):
        return int(self._meta_get(key, 0) or 0)

    def gate_state(self):
        g = dict(DEFAULT_GATE)
        g.update(self._meta_get("gate", {}) or {})
        return g

    def flags(self):
        return list(self._meta_get("flags", []) or [])

    def waiting(self, now=None):
        """The active job wait, or None once it has expired."""
        w = self._meta_get("waiting")
        if not w:
            return None
        if (now or _now()) > w.get("until", 0):
            return None
        return w

    def node(self, node_id):
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no node {node_id!r}")
        return dict(row)

    def nodes(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM nodes ORDER BY created_seq, ordinal")]

    def children(self, node_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM nodes WHERE parent=? ORDER BY ordinal", (node_id,))]

    def subtree_ids(self, node_id):
        out, todo = [], [node_id]
        while todo:
            nid = todo.pop()
            out.append(nid)
            todo.extend(r[0] for r in self.db.execute("SELECT id FROM nodes WHERE parent=?", (nid,)))
        return out

    def problem_of(self, node_id):
        nid = node_id
        while True:
            n = self.node(nid)
            if n["kind"] == "problem":
                return n
            nid = n["parent"]

    def ancestors(self, node_id):
        out, nid = [], self.node(node_id)["parent"]
        while nid:
            n = self.node(nid)
            out.append(n)
            nid = n["parent"]
        return out

    def evidence(self, node_id=None, since_seq=0, unique_only=False):
        q = "SELECT * FROM evidence WHERE seq > ?"
        args = [since_seq]
        if node_id is not None:
            ids = self.subtree_ids(node_id)
            q += " AND node_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        if unique_only:
            q += " AND duplicate_of IS NULL"
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def questions(self, status=None):
        if status:
            rows = self.db.execute("SELECT * FROM questions WHERE status=? ORDER BY id", (status,))
        else:
            rows = self.db.execute("SELECT * FROM questions ORDER BY id")
        return [dict(r) for r in rows]

    def last_seq(self):
        row = self.db.execute("SELECT MAX(seq) FROM events").fetchone()
        return row[0] or 0

    def events(self, since_seq=0, etype=None):
        q, args = "SELECT * FROM events WHERE seq > ?", [since_seq]
        if etype:
            q += " AND type=?"
            args.append(etype)
        return [dict(r) for r in self.db.execute(q + " ORDER BY seq", args)]

    # --- derived views used by the gate
    def problems(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM nodes WHERE kind='problem' ORDER BY ordinal")]

    def open_problems(self):
        return [p for p in self.problems() if p["status"] in OPEN]

    def awaiting_human(self):
        """Problems / nodes / questions only the human can move."""
        return {
            "pending_verification": [p for p in self.problems() if p["status"] == "pending_verification"],
            "blocked": [n for n in self.nodes() if n["status"] == "blocked"],
            "questions": [q for q in self.questions("open")],
            "proposed": [p for p in self.problems() if p["status"] == "proposed"],
        }

    def workable(self):
        """Nodes the worker can act on now: open solutions/approaches under an
        open problem (no closed/blocked ancestor), plus open problems that have
        no open descendant (they need approaches) -- unless a node of theirs waits
        on an open question to the human: the worker said what it needs, so the
        gate does not push it to invent other ways while it waits."""
        nodes = {n["id"]: n for n in self.nodes()}     # one query; the walk is in memory
        asked = {q["id"] for q in self.questions("open")}
        waiting = {n["id"].split(".", 1)[0] for n in nodes.values()
                   if n["status"] == "blocked" and n["blocked_by_question"] in asked}

        def chain_open(n):
            pid = n["parent"]
            while pid:
                a = nodes[pid]
                if a["status"] not in OPEN:
                    return False
                pid = a["parent"]
            return True

        by_problem = {}
        for n in nodes.values():
            if n["kind"] != "problem" and n["status"] in OPEN and chain_open(n):
                by_problem.setdefault(n["id"].split(".", 1)[0], []).append(n)
        out = []
        for p in sorted((n for n in nodes.values() if n["kind"] == "problem" and n["status"] in OPEN),
                        key=lambda n: n["ordinal"]):
            desc = sorted(by_problem.get(p["id"], []), key=lambda n: (n["created_seq"], n["ordinal"]))
            out.extend(desc if desc else ([] if p["id"] in waiting else [p]))
        return out

    def next_node(self):
        """In-progress first -- a claimed near-done node, then approaches (the concrete work)
        before solutions, then the most recently touched -- then highest-priority todo, in
        checklist order."""
        work = self.workable()
        if not work:
            return None
        picked = [n for n in work if n["status"] == "todo" and n["priority"] >= HUMAN_PICK]
        if picked:            # the human's pick goes first, even before unfinished work
            return max(picked, key=lambda n: (n["priority"], -n["created_seq"]))
        inprog = [n for n in work if n["status"] == "in_progress" and n["kind"] != "problem"]
        if inprog:
            return max(inprog, key=lambda n: (n["near_done_claims"] > 0, n["kind"] == "approach", n["updated_seq"]))
        return sorted(work, key=lambda n: (-n["priority"], n["created_seq"], n["ordinal"]))[0]

    def near_done_nodes(self):
        return [n for n in self.nodes() if n["near_done_claims"] > 0 and n["status"] in OPEN]

    # ------------------------------------------------------------ writing
    def _check_actor_text(self, actor):
        if actor not in ACTORS:
            raise LedgerError(f"unknown actor {actor!r}")

    def add_node(self, parent, title, rationale="", kind=None, source=None,
                 priority=0, actor="worker"):
        title = (title or "").strip()
        if len(title) < 4:
            raise LedgerError("title is too short; say what this node is about")
        par = self.node(parent)
        if kind is None:
            kind = "solution" if par["kind"] == "problem" else "approach"
        if kind not in ("solution", "approach"):
            raise LedgerError("only solutions and approaches can be added (problems come from the checklist)")
        if kind == "solution" and par["kind"] != "problem":
            raise LedgerError("a solution's parent must be a problem")
        if kind == "approach" and par["kind"] == "approach":
            raise LedgerError("an approach's parent must be a problem or a solution, not another approach")
        if par["status"] in CLOSED:
            if par["kind"] == "problem":    # a problem has no parent to fall back on
                raise LedgerError(_closed_problem_msg(parent, par["status"], actor))
            raise LedgerError(f"{parent} is {par['status']}; add the node under its parent instead")
        problem = self.problem_of(parent)
        if problem["status"] in ("done", "abandoned"):
            raise LedgerError(_closed_problem_msg(problem["id"], problem["status"], actor))
        source = source or actor
        letter = "S" if kind == "solution" else "A"

        def apply(cur, seq):
            n = cur.execute("SELECT COUNT(*) FROM nodes WHERE parent=? AND kind=?",
                            (parent, kind)).fetchone()[0] + 1
            nid = f"{parent}.{letter}{n}"
            while cur.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone():
                n += 1
                nid = f"{parent}.{letter}{n}"
            ordinal = cur.execute("SELECT COUNT(*) FROM nodes WHERE parent=?", (parent,)).fetchone()[0] + 1
            cur.execute(
                "INSERT INTO nodes(id, kind, parent, title, rationale, status, priority, source,"
                " ordinal, created_seq, updated_seq) VALUES(?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)",
                (nid, kind, parent, _clip(title, 500), _clip(rationale), int(priority), source,
                 ordinal, seq, seq))
            return {"id": nid, "parent": parent, "kind": kind, "title": title, "source": source}

        return self._mutate(actor, "add_node", apply)["id"]

    def start(self, node_id, actor="worker"):
        n = self.node(node_id)
        if n["kind"] == "problem":
            raise LedgerError("start a solution or approach, not the problem itself")
        if n["status"] != "todo":
            raise LedgerError(f"{node_id} is {n['status']}, not todo")
        closed = [a for a in self.ancestors(node_id) if a["status"] in CLOSED + ("blocked",)]
        if closed:
            raise LedgerError(f"ancestor {closed[0]['id']} is {closed[0]['status']}")

        def apply(cur, seq):
            cur.execute("UPDATE nodes SET status='in_progress', updated_seq=? WHERE id=?", (seq, node_id))
            # Open the ancestors too (todo -> in_progress), problem included.
            for a in self.ancestors(node_id):
                if a["status"] == "todo":
                    cur.execute("UPDATE nodes SET status='in_progress', updated_seq=? WHERE id=?",
                                (seq, a["id"]))
            return {"id": node_id}

        return self._mutate(actor, "start", apply)

    def attach_evidence(self, node_id, summary, ref="", content=None, kind="output", actor="worker"):
        """Returns (evidence_id, duplicate_of or None). The content hash is taken
        over the raw content when given (preferred: paste the actual output or
        the VM spill file's sha256), else over summary+ref."""
        n = self.node(node_id)
        summary = (summary or "").strip()
        if len(summary) < 4:
            raise LedgerError("evidence needs a summary of what it shows")
        if kind not in ("output", "file", "flag_candidate", "note"):
            raise LedgerError("kind must be output, file, flag_candidate or note")
        basis = content if content not in (None, "") else (summary + "\x00" + (ref or ""))
        chash = hashlib.sha256(str(basis).encode("utf-8", "replace")).hexdigest()
        if n["status"] in ("done", "abandoned") and n["kind"] == "problem":
            raise LedgerError(f"problem {node_id} is {n['status']}: attach the output to a node of an open problem, "
                              "or propose_problem for work that is its own")

        def apply(cur, seq):
            dup = cur.execute("SELECT id FROM evidence WHERE content_hash=? ORDER BY id LIMIT 1",
                              (chash,)).fetchone()
            dup_id = dup[0] if dup else None
            cur.execute("INSERT INTO evidence(node_id, kind, summary, ref, excerpt, content_hash,"
                        " duplicate_of, seq) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (node_id, kind, _clip(summary, 1000), _clip(ref or "", 1000),
                         _excerpt(content) if content else "", chash, dup_id, seq))
            eid = cur.lastrowid
            if dup_id is None:
                self._bump(cur, "unique_evidence")
            return {"id": eid, "node": node_id, "duplicate_of": dup_id, "hash": chash[:16]}

        p = self._mutate(actor, "evidence", apply)
        return p["id"], p["duplicate_of"]

    def _require_evidence(self, node_id, evidence_ids=()):
        """At least one evidence item in the subtree that is not a copy of
        evidence recorded elsewhere. A duplicate of an item that is itself in
        this subtree is fine (the original counts)."""
        sub_rows = {r["id"]: r for r in self.evidence(node_id)}
        if evidence_ids:
            try:
                ids = [int(e) for e in evidence_ids]
            except (TypeError, ValueError):
                raise LedgerError("evidence ids must be integers") from None
            missing = [e for e in ids if e not in sub_rows]
            if missing:
                raise LedgerError(f"evidence {missing} not found under {node_id}: evidence counts only under the "
                                  f"node it proves, and a copy of evidence from elsewhere does not count. Attach "
                                  f"{node_id}'s own output (the file or result this work produced) and cite that")
            chosen = [sub_rows[e] for e in ids]
        else:
            chosen = list(sub_rows.values())
        ok = [r for r in chosen if r["duplicate_of"] is None or r["duplicate_of"] in sub_rows]
        if not ok:
            raise LedgerError(f"{node_id} has no evidence of its own (only copies of evidence recorded "
                              "elsewhere); attach_evidence with the new output first")
        return ok

    def apply_decision(self, order, question="", why="", first_step="", switch_if="", model="", confidence=None,
                       usage=None, actor="decider"):
        """The decider's ranking of a branch's options: priorities count down from DECIDER_TOP
        (a human pick still goes first) and each option's rationale records its rank."""
        if actor != "decider":
            raise LedgerError("only the decider ranks a branch")
        nodes = [self.node(nid) for nid in order]
        if not nodes:
            raise LedgerError("nothing to rank")

        def apply(cur, seq):
            for i, n in enumerate(nodes):
                if n["status"] not in OPEN:
                    continue
                note = (f"[decider: first of {len(nodes)}] {why}" + (f" | first step: {first_step}" if first_step else "")
                        + (f" | switch if: {switch_if}" if switch_if else "")) if i == 0 else \
                    f"[decider: {i + 1} of {len(nodes)}]"
                rationale = (n["rationale"] + "\n" + note).strip()
                # a node the human picked keeps its place; the rest follow the decider's order
                prio = n["priority"] if n["priority"] >= HUMAN_PICK else max(1, DECIDER_TOP - i)
                cur.execute("UPDATE nodes SET priority=?, rationale=?, updated_seq=? WHERE id=?",
                            (prio, _clip(rationale, 4000), seq, n["id"]))
            return {"question": _clip(question, 500), "order": [n["id"] for n in nodes], "why": why,
                    "first_step": first_step, "switch_if": switch_if, "model": model, "confidence": confidence,
                    "usage": usage}

        return self._mutate(actor, "decision", apply)

    def pick(self, node_id, actor="human"):
        """The human chooses which open node is taken next (`beacon: pick`)."""
        if actor != "human":
            raise LedgerError("only the human picks")
        n = self.node(node_id)
        if n["kind"] == "problem":
            raise LedgerError("pick a solution or an approach (P1.S1, P1.A2, ...), not a problem")
        if n["status"] not in OPEN:
            raise LedgerError(f"{node_id} is {n['status']}; reopen it first")
        top = self.db.execute("SELECT MAX(priority) FROM nodes").fetchone()[0] or 0
        prio = max(HUMAN_PICK, top + 1)

        def apply(cur, seq):
            cur.execute("UPDATE nodes SET priority=?, updated_seq=? WHERE id=?", (prio, seq, node_id))
            return {"id": node_id, "priority": prio}

        return self._mutate(actor, "pick", apply)

    def set_status(self, node_id, status, reason="", assumption="", superseded_by=None,
                   evidence_ids=(), actor="worker"):
        n = self.node(node_id)
        reason, assumption = (reason or "").strip(), (assumption or "").strip()
        valid = PROBLEM_STATUSES if n["kind"] == "problem" else NODE_STATUSES
        if status not in valid:
            raise LedgerError(f"invalid status {status!r} for a {n['kind']}")
        # Measured 2026-11-12: the human rejected P2's candidate, P2.S2 redid the work, and the worker could not
        # mark the old P2.S1 as replaced ("only the human can reopen it"), so the ledger kept showing it as the
        # answer. done -> superseded, naming the node that replaced it, is a correction, not a reopening.
        relabel = (n["status"] == "done" and status == "superseded" and n["kind"] != "problem" and bool(superseded_by))
        if actor in ("worker", "lighthouse") and not relabel:
            if n["status"] in CLOSED or n["status"] == "pending_verification":
                raise LedgerError(f"{node_id} is {n['status']}; only the human can reopen it"
                                  + (" (a done node whose result was replaced can be set superseded with "
                                     "superseded_by=<the node that replaced it>)" if n["status"] == "done" else ""))
            if n["kind"] == "problem" and status not in ("blocked",):
                raise LedgerError("a problem becomes done only through propose_done + verification; "
                                  "the worker may only mark it blocked")
            if status in ("abandoned", "todo", "pending_verification"):
                raise LedgerError(f"the worker cannot set {status!r}")
            if status == "in_progress":
                return self.start(node_id, actor)
        if status in ("dead_end", "blocked", "superseded", "abandoned") and len(reason) < MIN_REASON:
            raise LedgerError(f"{status} needs a reason (at least {MIN_REASON} characters)")
        if status == "dead_end" and len(assumption) < MIN_REASON:
            raise LedgerError("dead_end needs the assumption it rests on "
                              "(what would have to be false for this approach to work after all?)")
        if superseded_by:
            self.node(superseded_by)
            if relabel and (superseded_by == node_id
                            or self.problem_of(superseded_by)["id"] != self.problem_of(node_id)["id"]):
                raise LedgerError("superseded_by must be another node of the same problem (the one that replaced it)")
        if status == "done" and n["kind"] != "problem":
            self._require_evidence(node_id, evidence_ids)
        counts_as_progress = status in ("done", "dead_end", "superseded") and not relabel

        def apply(cur, seq):
            if status in CLOSED:
                self._drop_questions(cur, node_id, f"{node_id} is {status}")
            cur.execute("UPDATE nodes SET status=?, reason=?, assumption=?, superseded_by=?,"
                        " updated_seq=? WHERE id=?",
                        (status, _clip(reason), _clip(assumption), superseded_by, seq, node_id))
            if counts_as_progress and actor in ("worker", "verifier", "human"):
                self._bump(cur, "progress")
            return {"id": node_id, "from": n["status"], "to": status, "reason": reason,
                    "assumption": assumption, "evidence": list(evidence_ids)}

        return self._mutate(actor, "status", apply)

    def propose_done(self, problem_id, candidate, evidence_ids, summary="", actor="worker"):
        """The only way a problem gets done. With a verify_cmd (from the
        checklist) it is run immediately; the candidate reaches it ONLY through
        the BEACON_CANDIDATE environment variable, never the command line."""
        p = self.node(problem_id)
        if p["kind"] != "problem":
            raise LedgerError("propose_done takes a problem id (P<n>)")
        if p["status"] in ("done", "abandoned"):
            raise LedgerError(f"{problem_id} is already {p['status']}")
        if p["status"] == "proposed":
            raise LedgerError(f"{problem_id} is a proposed problem waiting for the human to accept it")
        candidate = (candidate or "").strip()
        if not candidate:
            raise LedgerError("propose_done needs the candidate answer (the flag / result)")
        if not evidence_ids:
            raise LedgerError("propose_done needs evidence ids showing how the candidate was obtained")
        self._require_evidence(problem_id, evidence_ids)
        auto = (not p["verify_cmd"] and p["source"] == "followup" and self.config().get("followup_autoverify", True))
        if auto:
            left = [n["id"] for n in (self.node(i) for i in self.subtree_ids(problem_id)[1:])
                    if n["status"] in OPEN or n["status"] == "blocked"]
            if left:
                raise LedgerError(f"close every item of follow-up {problem_id} first (done with evidence, or a dead "
                                  f"end with its assumption); still open: {', '.join(left)}")

        def mark_pending(cur, seq):
            cur.execute("UPDATE nodes SET status='pending_verification', candidate=?, reason=?,"
                        " updated_seq=? WHERE id=?",
                        (_clip(candidate, 4096), _clip(summary), seq, problem_id))
            return {"id": problem_id, "candidate": candidate, "evidence": list(evidence_ids)}

        self._mutate(actor, "propose_done", mark_pending)
        if auto:
            note = "follow-up: every item is closed and the work has evidence of its own (followup_autoverify)"
            self.verify(problem_id, True, note, actor="verifier")
            return {"status": "done", "verified": True, "detail": note}
        if not p["verify_cmd"]:
            return {"status": "pending_verification", "verified": None,
                    "detail": f"no verify_cmd for this problem; the human verifies it (`beacon: verify {problem_id}` in the Claude Code chat, or `beacon verify {problem_id}` in a terminal). Tell them the candidate."}
        ok, detail = self._run_verify(p["verify_cmd"], candidate)
        self.verify(problem_id, ok, detail, actor="verifier")
        return {"status": "done" if ok else "in_progress", "verified": ok, "detail": detail}

    def _run_verify(self, cmd, candidate):
        return run_verify(cmd, candidate, self.workdir(), int(self.config()["verify_timeout"]))

    def verify(self, problem_id, accept, note="", actor="human"):
        if actor not in ("human", "verifier"):
            raise LedgerError("only the human or the verifier can verify a problem")
        p = self.node(problem_id)
        if p["status"] != "pending_verification":
            raise LedgerError(f"{problem_id} is {p['status']}, not pending_verification")

        def apply(cur, seq):
            new = "done" if accept else "in_progress"
            cur.execute("UPDATE nodes SET status=?, reason=?, updated_seq=? WHERE id=?",
                        (new, _clip(note or ("verified" if accept else "verification failed")),
                         seq, problem_id))
            closed, dropped = [], []
            if accept:  # nothing under a solved problem is still open work, or still waiting on the human
                for nid in self.subtree_ids(problem_id)[1:]:
                    row = cur.execute("SELECT status FROM nodes WHERE id=?", (nid,)).fetchone()
                    if row[0] in OPEN or row[0] == "blocked":
                        cur.execute("UPDATE nodes SET status='superseded', reason=?, blocked_by_question=NULL,"
                                    " updated_seq=? WHERE id=?", (f"{problem_id} verified done", seq, nid))
                        closed.append(nid)
                dropped = self._drop_questions(cur, problem_id, f"{problem_id} was solved")
            # A refuted candidate is information too: it rules something out.
            self._bump(cur, "progress")
            return {"id": problem_id, "accepted": bool(accept), "note": note, "closed": closed, "dropped": dropped}

        return self._mutate(actor, "verify", apply)

    def _drop_questions(self, cur, root_id, why):
        """Open questions about nodes under root_id no longer need the human (inside a transaction)."""
        ids = self.subtree_ids(root_id)
        rows = [r[0] for r in cur.execute(
            "SELECT id FROM questions WHERE status='open' AND node_id IN (%s)" % ",".join("?" * len(ids)), ids)]
        for qid in rows:
            cur.execute("UPDATE questions SET status='obsolete', answer=?, delivered=1 WHERE id=?",
                        (_clip("no longer needed: " + why), qid))
        return rows

    def claim_near_done(self, node_id, remaining, actor="worker"):
        n = self.node(node_id)
        steps = [str(s).strip() for s in (remaining or []) if str(s).strip()]
        if not steps:
            raise LedgerError("claim_near_done needs the concrete remaining steps")
        if n["status"] not in OPEN:
            raise LedgerError(f"{node_id} is {n['status']}")
        unique_now = len(self.evidence(node_id, unique_only=True))

        def apply(cur, seq):
            first = n["near_done_claims"] == 0
            cur.execute("UPDATE nodes SET remaining=?, near_done_claims=near_done_claims+1,"
                        " near_done_evidence=?, updated_seq=? WHERE id=?",
                        (json.dumps(steps[:20], ensure_ascii=False),
                         unique_now if first else n["near_done_evidence"], seq, node_id))
            return {"id": node_id, "remaining": steps[:20], "claim": n["near_done_claims"] + 1}

        return self._mutate(actor, "near_done", apply)

    def ask(self, kind, question, node_id=None, options=(), actor="worker"):
        """approach_choice is never sent to the human: each option becomes a
        sibling approach. need_info / need_permission open a question and block
        the node (if given) until the human answers."""
        if kind not in QUESTION_KINDS:
            raise LedgerError(f"kind must be one of {QUESTION_KINDS}")
        question = (question or "").strip()
        if len(question) < 8:
            raise LedgerError("say what you need to know")
        node = self.node(node_id) if node_id else None
        if kind == "approach_choice":
            opts = [str(o).strip() for o in (options or []) if str(o).strip()]
            if len(opts) < 2:
                raise LedgerError("approach_choice needs at least two options")
            if node is None or node["kind"] == "approach":
                raise LedgerError("approach_choice needs the problem or solution the options belong to")
            ids = [self.add_node(node_id, o, rationale=f"option from: {question}",
                                 kind="approach", priority=len(opts) - i, actor=actor)
                   for i, o in enumerate(opts)]

            def rec(cur, seq):
                cur.execute("INSERT INTO questions(node_id, kind, question, options, status, answer,"
                            " delivered, seq) VALUES(?, ?, ?, ?, 'auto_resolved', ?, 1, ?)",
                            (node_id, kind, _clip(question), json.dumps(opts, ensure_ascii=False),
                             "converted to approaches " + ", ".join(ids), seq))
                return {"qid": cur.lastrowid, "created": ids}

            return self._mutate(actor, "ask", rec)

        def apply(cur, seq):
            cur.execute("INSERT INTO questions(node_id, kind, question, options, status, seq)"
                        " VALUES(?, ?, ?, ?, 'open', ?)",
                        (node_id, kind, _clip(question),
                         json.dumps([str(o) for o in (options or [])], ensure_ascii=False), seq))
            qid = cur.lastrowid
            if node is not None and node["status"] in OPEN:
                cur.execute("UPDATE nodes SET status='blocked', reason=?, blocked_by_question=?,"
                            " updated_seq=? WHERE id=?",
                            (f"awaiting the human on question #{qid}", qid, seq, node_id))
            return {"qid": qid, "node": node_id, "kind": kind}

        return self._mutate(actor, "ask", apply)

    def approve_request(self, question, node_id=None, basis="", actor="gate"):
        """A worker's request granted by the human's standing rule (permission_policy=approve):
        recorded like a question that was answered at once, so the audit trail shows it."""
        if actor not in ("gate", "worker"):
            raise LedgerError("standing approvals are recorded by the gate or the ask tool")
        if self.config().get("permission_policy", "approve") != "approve":
            raise LedgerError("permission_policy is not approve")
        if node_id:
            self.node(node_id)
        answer = "approved by the human's standing rule (permission_policy=approve)" + (f"; {basis}" if basis else "")

        def apply(cur, seq):
            cur.execute("INSERT INTO questions(node_id, kind, question, options, status, answer, delivered, seq)"
                        " VALUES(?, 'need_permission', ?, '[]', 'auto_approved', ?, 1, ?)",
                        (node_id, _clip(question), answer, seq))
            return {"qid": cur.lastrowid, "node": node_id, "question": _clip(question, 500), "answer": answer}

        return self._mutate(actor, "approved", apply)

    def answer(self, qid, text, actor="human"):
        if actor != "human":
            raise LedgerError("only the human answers questions")
        row = self.db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if row is None:
            raise LedgerError(f"no question #{qid}")
        if row["status"] != "open":
            raise LedgerError(f"question #{qid} is {row['status']}")

        def apply(cur, seq):
            cur.execute("UPDATE questions SET status='answered', answer=?, delivered=0 WHERE id=?",
                        (_clip(text), qid))
            reopened = [r[0] for r in cur.execute(
                "SELECT id FROM nodes WHERE blocked_by_question=? AND status='blocked'", (qid,))]
            for nid in reopened:
                cur.execute("UPDATE nodes SET status='todo', reason='', blocked_by_question=NULL,"
                            " updated_seq=? WHERE id=?", (seq, nid))
            self._bump(cur, "progress")
            return {"qid": qid, "answer": text, "reopened": reopened}

        return self._mutate(actor, "answer", apply)

    def mark_delivered(self, qids, actor="gate"):
        qids = [int(q) for q in qids]
        if not qids:
            return None

        def apply(cur, seq):
            cur.executemany("UPDATE questions SET delivered=1 WHERE id=?", [(q,) for q in qids])
            return {"delivered": qids}

        return self._mutate(actor, "delivered", apply)

    def propose_problem(self, title, rationale="", evidence_ids=(), actor="worker"):
        """A new problem found during the work (a second crash, a second stage, ...). With
        problem_approval=auto (the default) it is todo at once; with human it is "proposed" until the human
        accepts it."""
        title, rationale = (title or "").strip(), (rationale or "").strip()
        if len(title) < 8:
            raise LedgerError("say what the problem is (a title of at least 8 characters)")
        if len(rationale) < MIN_REASON:
            raise LedgerError("say why it is a problem of its own and how you found it (the rationale)")
        ev = [int(e) for e in (evidence_ids or [])]
        for eid in ev:
            if not self.db.execute("SELECT 1 FROM evidence WHERE id=?", (eid,)).fetchone():
                raise LedgerError(f"no evidence #{eid}")
        for p in self.problems():
            if _norm_title(p["title"]) == _norm_title(title):
                raise LedgerError(f"{p['id']} is already that problem ({p['status']})")
        status = "todo" if self.config().get("problem_approval") == "auto" else "proposed"
        note = rationale + (f" (evidence {', '.join('#' + str(e) for e in ev)})" if ev else "")

        def apply(cur, seq):
            n = cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='problem'").fetchone()[0] + 1
            pid = f"P{n}"
            while cur.execute("SELECT 1 FROM nodes WHERE id=?", (pid,)).fetchone():
                n += 1
                pid = f"P{n}"
            cur.execute(
                "INSERT INTO nodes(id, kind, parent, title, rationale, status, priority, source, ordinal,"
                " created_seq, updated_seq) VALUES(?, 'problem', NULL, ?, ?, ?, 0, ?, ?, ?, ?)",
                (pid, _clip(title, 500), _clip(note), status, actor, n, seq, seq))
            return {"id": pid, "title": title, "status": status, "evidence": ev}

        return self._mutate(actor, "propose_problem", apply)

    def accept_problem(self, problem_id, accept=True, note="", actor="human"):
        if actor != "human":
            raise LedgerError("only the human accepts or rejects a proposed problem")
        p = self.node(problem_id)
        if p["kind"] != "problem" or p["status"] != "proposed":
            raise LedgerError(f"{problem_id} is not a proposed problem ({p['kind']}, {p['status']})")
        if not accept and len((note or "").strip()) < MIN_REASON:
            raise LedgerError("say why it is rejected")
        new = "todo" if accept else "abandoned"

        def apply(cur, seq):
            cur.execute("UPDATE nodes SET status=?, reason=?, updated_seq=? WHERE id=?",
                        (new, _clip(note or ("accepted by the human" if accept else "")), seq, problem_id))
            if not accept:
                self._drop_questions(cur, problem_id, f"{problem_id} was rejected")
                # Its items go with it (measured 2026-11-13: P7 rejected, P7.S1 still listed as todo under it).
                for nid in self.subtree_ids(problem_id):
                    if nid != problem_id:
                        cur.execute("UPDATE nodes SET status='abandoned', reason=?, updated_seq=? WHERE id=? AND "
                                    "status IN ('todo', 'in_progress', 'blocked', 'proposed')",
                                    (_clip(f"{problem_id} was rejected"), seq, nid))
            if accept:
                self._bump(cur, "progress")
            return {"id": problem_id, "accepted": accept, "note": note}

        return self._mutate(actor, "accept_problem" if accept else "reject_problem", apply)

    def followup_problem(self, origin, optional=False, wait=False, actor="gate"):
        """The problem that holds the unfinished work listed after `origin` (a problem id, or None) was
        closed: an open one of that origin is reused, else a new one is made -- todo (the worker does it); for
        optional ideas "proposed" only under problem_approval=human; with wait=True always "proposed" (the
        human said so: followup_policy=propose, or the origin is a problem they abandoned). -> its id."""
        if actor not in ("gate", "lighthouse"):
            raise LedgerError("follow-up problems are made by the gate")
        o = self.node(origin) if origin else None
        if o is not None and o["kind"] != "problem":
            raise LedgerError(f"{origin} is not a problem")
        status = "proposed" if wait or (optional and self.config().get("problem_approval") != "auto") else "todo"
        want = ("proposed",) if status == "proposed" else OPEN
        tag = origin or "-"
        mine = [p for p in self.problems() if p["source"] == "followup" and _origin_tag(p["rationale"]) == tag]
        for p in mine:
            if p["status"] in want:
                return p["id"]
        label = "Optional follow-ups" if status == "proposed" else "Follow-ups"
        base = f"{label} of {origin}: {o['title'][:80]}" if o is not None else f"{label} of this run"
        titles = {_norm_title(p["title"]) for p in self.problems()}
        title, k = base, 2
        while _norm_title(title) in titles:
            title, k = f"{base} ({k})", k + 1
        what = "Checks the lighthouse reviewer suggested" if actor == "lighthouse" else "Work the worker listed as not done"
        rationale = (f"origin: {tag}\n{what}"
                     + ((" after " + origin + (" was handed to the human to verify" if o["status"] == "pending_verification"
                                               else " was closed")) if o is not None else "")
                     + ("; ideas beyond the run, waiting for the human to accept them" if status == "proposed" else "")
                     + ".")

        def apply(cur, seq):
            n = cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='problem'").fetchone()[0] + 1
            pid = f"P{n}"
            while cur.execute("SELECT 1 FROM nodes WHERE id=?", (pid,)).fetchone():
                n += 1
                pid = f"P{n}"
            cur.execute(
                "INSERT INTO nodes(id, kind, parent, title, rationale, status, priority, source, ordinal,"
                " created_seq, updated_seq) VALUES(?, 'problem', NULL, ?, ?, ?, 0, 'followup', ?, ?, ?)",
                (pid, _clip(title, 500), _clip(rationale), status, n, seq, seq))
            return {"id": pid, "title": title, "status": status, "origin": origin}

        return self._mutate(actor, "followup_problem", apply)["id"]

    def reopen(self, node_id, note="", actor="human"):
        if actor != "human":
            raise LedgerError("only the human reopens a node")
        n = self.node(node_id)
        new = "in_progress" if n["kind"] == "problem" else "todo"

        def apply(cur, seq):
            cur.execute("UPDATE nodes SET status=?, reason=?, blocked_by_question=NULL,"
                        " updated_seq=? WHERE id=?", (new, _clip(note), seq, node_id))
            return {"id": node_id, "from": n["status"], "to": new, "note": note}

        return self._mutate(actor, "reopen", apply)

    def mark_waiting(self, node_id, job, host="kali", max_minutes=30, actor="worker"):
        self.node(node_id)
        cap = int(self.config()["max_wait_minutes"])
        minutes = max(1, min(int(max_minutes), cap))
        until = _now() + 60 * minutes

        def apply(cur, seq):
            w = {"node": node_id, "job": str(job), "host": str(host), "until": until,
                 "minutes": minutes}
            self._meta_set(cur, "waiting", w)
            return w

        return self._mutate(actor, "waiting", apply)

    def vm_baseline(self, host):
        return (self._meta_get("vm_baseline", {}) or {}).get(host)

    def set_vm_baseline(self, host, fingerprint, actor="worker"):
        def apply(cur, seq):
            b = self._meta_read(cur, "vm_baseline", {}) or {}
            b[host] = fingerprint
            self._meta_set(cur, "vm_baseline", b)
            return {"host": host, "processes": len(fingerprint.get("processes", [])),
                    "ports": len(fingerprint.get("ports", [])), "files": fingerprint.get("files")}

        return self._mutate(actor, "vm_baseline", apply)

    def add_flag(self, kind, node_id, why, actor="lighthouse"):
        def apply(cur, seq):
            flags = self._meta_read(cur, "flags", []) or []
            entry = {"kind": kind, "node": node_id, "why": _clip(why, 600), "seq": seq}
            flags.append(entry)
            self._meta_set(cur, "flags", flags[-100:])
            return entry

        return self._mutate(actor, "flag", apply)

    def save_gate(self, gate, decision, actor="gate"):
        def apply(cur, seq):
            self._meta_set(cur, "gate", gate)
            if decision.get("clear_waiting"):
                self._meta_set(cur, "waiting", None)
            return decision

        return self._mutate(actor, "gate", apply)

    # Requests the human makes from the chat (pause, a lighthouse review). Kept apart from the
    # gate state: the Stop hook rewrites that whole object, and a request made while it runs
    # would be lost. Each change is one atomic event.
    HUMAN_FLAGS = ("paused", "lighthouse_request")

    def human_flags(self):
        return dict(self._meta_get("human", {}) or {})

    def set_human_flag(self, key, value, actor="human"):
        if actor != "human":
            raise LedgerError("only the human sets this")
        if key not in self.HUMAN_FLAGS:
            raise LedgerError(f"unknown flag {key!r}")

        def apply(cur, seq):
            flags = self._meta_read(cur, "human", {}) or {}
            if value is None:
                flags.pop(key, None)
            else:
                flags[key] = value
            self._meta_set(cur, "human", flags)
            return {"flag": key, "value": value}

        return self._mutate(actor, "human_flag", apply)

    def take_human_flag(self, key, actor="gate"):
        """Read and clear a one-shot request atomically. -> its value, or None."""
        if key not in self.HUMAN_FLAGS:
            raise LedgerError(f"unknown flag {key!r}")
        if key not in self.human_flags():
            return None

        def apply(cur, seq):
            flags = self._meta_read(cur, "human", {}) or {}
            val = flags.pop(key, None)
            self._meta_set(cur, "human", flags)
            return {"taken": key, "value": val}

        return self._mutate(actor, "human_flag_taken", apply)["value"]

    def set_config(self, key, value, actor="human"):
        if key not in DEFAULT_CONFIG:
            raise LedgerError(f"unknown config key {key!r}")
        if key == "permission_mode" and value not in PERMISSION_MODES:
            raise LedgerError(f"permission_mode must be one of {PERMISSION_MODES[1:]} (or empty)")
        if key == "problem_approval" and value not in PROBLEM_APPROVAL:
            raise LedgerError(f"problem_approval must be one of {PROBLEM_APPROVAL}")
        if key == "permission_policy" and value not in PERMISSION_POLICIES:
            raise LedgerError(f"permission_policy must be one of {PERMISSION_POLICIES}")
        if key == "followup_policy" and value not in FOLLOWUP_POLICIES:
            raise LedgerError(f"followup_policy must be one of {FOLLOWUP_POLICIES}")

        def apply(cur, seq):
            cfg = self._meta_read(cur, "config", {}) or {}
            cfg[key] = value
            self._meta_set(cur, "config", cfg)
            return {"key": key, "value": value}

        return self._mutate(actor, "config", apply)

    def record(self, etype, payload, actor="gate"):
        """An audit-only event (lighthouse call, session start, ...)."""
        return self._mutate(actor, etype, lambda cur, seq: dict(payload))

    # ------------------------------------------------------------ rendering
    def render_tree(self, root=None, evidence=True, max_evidence=3):
        lines = []

        def walk(nid, depth):
            n = self.node(nid)
            tag = n["status"]
            if n["near_done_claims"]:
                tag += f", near-done x{n['near_done_claims']}"
            line = f"{'  ' * depth}{n['id']} [{tag}] {n['title']}"
            if n["source"] not in ("worker", "checklist"):
                line += f"  (from {n['source']})"
            lines.append(line)
            pad = "  " * (depth + 1)
            if n["kind"] == "problem":
                # The checklist's note is written for the worker (e.g. the exact form of the answer): show it.
                # The verify command itself is not shown: it may hold the expected answer.
                if n["rationale"]:
                    lines.append(f"{pad}note: " + n["rationale"].replace("\n", " | ")[:600])
                if n["status"] in OPEN:
                    if n["verify_cmd"]:
                        how = ("a verify command, run on propose_done; your candidate reaches it as $BEACON_CANDIDATE, "
                               "so give exactly the form the note asks for")
                    elif n["source"] == "followup" and self.config().get("followup_autoverify", True):
                        how = ("beacon itself: propose_done passes once every item under it is closed and the work "
                               "has evidence (no human check)")
                    else:
                        how = "the human"
                    lines.append(f"{pad}checked by: {how}")
            if n["reason"] and n["status"] not in OPEN:
                lines.append(f"{pad}reason: {n['reason']}")
            if n["assumption"]:
                lines.append(f"{pad}assumes: {n['assumption']}")
            if n["status"] in OPEN and n["near_done_claims"]:
                steps = json.loads(n["remaining"] or "[]")
                if steps:
                    lines.append(f"{pad}remaining: " + "; ".join(steps))
            if n["candidate"]:
                lines.append(f"{pad}candidate: {n['candidate']}")
            if evidence:
                ev = [dict(r) for r in self.db.execute(
                    "SELECT * FROM evidence WHERE node_id=? ORDER BY id DESC LIMIT ?",
                    (nid, max_evidence))]
                for e in reversed(ev):
                    dup = f" (duplicate of #{e['duplicate_of']})" if e["duplicate_of"] else ""
                    ref = f" @ {e['ref']}" if e["ref"] else ""
                    lines.append(f"{pad}evidence #{e['id']}: {e['summary']}{ref}{dup}")
            for c in self.children(nid):
                walk(c["id"], depth + 1)

        roots = [root] if root else [p["id"] for p in self.problems()]
        for r in roots:
            walk(r, 0)
        return "\n".join(lines)
