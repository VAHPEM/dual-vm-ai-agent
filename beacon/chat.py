# -*- coding: utf-8 -*-
"""beacon.chat — talk to beacon from the Claude Code chat itself.

The UserPromptSubmit hook sees every prompt the human submits in a supervised session,
before Claude does. Two things are handled there:

  `beacon: <command>`   a command for beacon, typed in the chat box (see HELP).
      Views and pause are answered by beacon alone: the prompt stops before it reaches
      Claude and the answer is shown to you — no model turn, no tokens. Actions are
      recorded as the human's and the prompt then goes on to Claude with a note of what
      changed, so it resumes at once.

  a reply to a question  `#3 use the emulator` answers question #3. When Claude stopped
      because it was waiting for you and exactly one question is open, a plain message is
      taken as the answer to it (beacon says so in the chat).

Only prompts the human submits reach this hook. Prompts that a supervised session could
submit by itself — a scheduled wakeup, a nested `claude` started from Bash — are refused by
the PreToolUse guard, and `beacon watch` (whose prompts are the watchdog's) skips this hook.
"""
import json
import re
import sqlite3

from .ledger import LedgerError

PREFIX = re.compile(r"^\s*beacon\s*:\s*", re.I)
REPLY = re.compile(r"^\s*#(\d+)\b[\s:,-]*(.*)\Z", re.S)
HELP = """beacon — commands you can type in this chat

Look (answered by beacon here; nothing is sent to Claude, no tokens):
  beacon: status              the whole tree with statuses, and the gate's counters
  beacon: show P1.A2          one node: why it exists, its result, evidence, children
  beacon: graph               open the visual graph in your browser (it keeps itself up to date)
  beacon: log [N]             the last N stops: what Claude said, what beacon replied (default 3)
  beacon: questions           questions waiting for you
  beacon: report              the report (near-done first, then what needs you)
  beacon: decisions           the latest decisions (Opus at branches) and the requests approved by standing rule
  beacon: help                this list

Decide (recorded as yours, then passed on to Claude so it carries on):
  beacon: answer N <text>     answer question N            (short form:  #N <text>)
  beacon: verify P1           accept P1's candidate        beacon: verify P1 reject <why>   reject it
  beacon: add P1 <idea>       add your own hypothesis/approach under a node
  beacon: reopen P1.A2 [note] reopen a closed node
  beacon: abandon P2 <why>    drop a problem or a node (you decide it is not worth it)
  beacon: pick P1.A3          choose which approach goes next (overrides Claude's order)
  beacon: accept P4           accept a problem Claude proposed (it becomes part of the run)
  beacon: reject P4 <why>     turn a proposed problem down
  beacon: lighthouse [note]   ask the lighthouse (Claude API) to review the ledger at the next stop

Steer:
  beacon: pause               stop pushing: Claude may stop at its next stop (nothing sent to Claude)
  beacon: resume              push again; Claude continues with the ledger's next node

Terminal equivalents (views and the run's config): python3 -m beacon <command> --run RUN."""
VIEW_MAX = 3800


def _clip(text, n=VIEW_MAX):
    return text if len(text) <= n else text[:n] + "\n… (truncated; `python3 -m beacon status --run RUN` shows all)"


def _questions(L):
    qs = L.questions("open")
    if not qs:
        return "No open questions."
    out = []
    for q in qs:
        out.append(f"#{q['id']} ({q['kind']}" + (f", {q['node_id']}" if q.get("node_id") else "")
                   + f"): {q['question']}")
    return "\n".join(out)


def _show(L, nid):
    n = L.node(nid)
    lines = [f"{n['id']}  [{n['status']}]  {n['kind']}, added by {n['source']}", n["title"]]
    for key, label in (("rationale", "why"), ("reason", "result"), ("assumption", "assumes"),
                       ("candidate", "candidate")):
        if n.get(key):
            lines.append(f"{label}: {n[key]}")
    steps = json.loads(n.get("remaining") or "[]")
    if steps and n["near_done_claims"]:
        lines.append(f"near-done claimed {n['near_done_claims']}x; remaining: " + "; ".join(steps))
    if n.get("blocked_by_question"):
        lines.append(f"blocked by question #{n['blocked_by_question']}")
    kids = L.children(nid)
    if kids:
        lines.append("children: " + ", ".join(f"{c['id']} [{c['status']}]" for c in kids))
    ev = L.evidence(nid)
    if ev:
        lines.append(f"evidence ({len(ev)} in this subtree, newest last):")
        for e in ev[-8:]:
            lines.append(f"  #{e['id']} on {e['node_id']}: {e['summary']}"
                         + (f" (duplicate of #{e['duplicate_of']})" if e["duplicate_of"] else ""))
    return "\n".join(lines)


def view(L, words, opener=None):
    """-> text for a view command, or None if `words` is not one."""
    cmd = words[0].lower() if words else "help"
    if cmd in ("help", "?", "commands"):
        return HELP
    if cmd in ("status", "tree"):
        g, h = L.gate_state(), L.human_flags()
        return _clip(f"Run: {L.title()}" + ("   (PAUSED)" if h.get("paused") else "") + "\n"
                     + L.render_tree(max_evidence=1)
                     + f"\n\ngate: {g['stops']} stops, {g['forced']} forced, stall {g['stall']}, idle {g['idle']}, "
                       f"lighthouse {g['lighthouse_calls']} calls; last decision {g['last_decision'] or '-'}")
    if cmd == "show":
        if len(words) < 2:
            raise LedgerError("usage: beacon: show NODE   (e.g. beacon: show P1.A2)")
        return _clip(_show(L, words[1]))
    if cmd == "log":
        from .logview import stop_log
        n = int(words[1]) if len(words) > 1 and words[1].isdigit() else 3
        return _clip(stop_log(L.dir, last=max(1, min(n, 20)), width=400))
    if cmd in ("questions", "q"):
        return _questions(L)
    if cmd in ("decisions", "decision"):
        from .decider import recent
        rows = recent(L, 5)
        approved = [q for q in L.questions() if q["status"] == "auto_approved"][-8:]
        if not rows and not approved:
            return "No branch decisions or approved requests yet."
        out = []
        if approved:
            out.append("Approved by your standing rule (newest last):\n" + "\n".join(
                f"  #{q['id']} {q['question'][:200]}" for q in approved))
        for d in rows:
            out.append(f"{' > '.join(d.get('order') or [])}   ({d.get('model')}, confidence {d.get('confidence')})\n"
                       f"  question: {d.get('question')}\n  why: {d.get('why')}\n  first step: {d.get('first_step')}"
                       f"\n  switch if: {d.get('switch_if')}")
        return _clip("\n\n".join(out))
    if cmd == "report":
        from .report import render_report
        return _clip(render_report(L))
    if cmd in ("graph", "board"):
        from .board import write_board
        path = write_board(L)
        opened = False
        if opener is not None and not (len(words) > 1 and words[1].lower() in ("nopen", "--no-open", "path")):
            try:
                opened = bool(opener("file://" + path))
            except Exception:  # noqa: BLE001  -- no browser here (e.g. Claude Code over SSH): show the path
                opened = False
        return (f"Graph: file://{path}\n" + ("Opened in your browser. " if opened else "Open it in a browser. ")
                + "It reloads itself every few seconds while the run goes on (untick \"live\" to stop that).")
    return None


def act(L, words, rest):
    """Run an action as the human -> (text for the human, note for Claude, block?)."""
    cmd = words[0].lower()
    if cmd == "answer":
        if len(words) < 3 or not words[1].lstrip("#").isdigit():
            raise LedgerError("usage: beacon: answer N <text>")
        show, note = _answer(L, int(words[1].lstrip("#")), rest.split(None, 2)[2])
        return show, note, False
    if cmd == "verify":
        if len(words) < 2:
            raise LedgerError("usage: beacon: verify P1   |   beacon: verify P1 reject <why>")
        pid = words[1]
        verdict = words[2].lower() if len(words) > 2 else "accept"
        if verdict in ("reject", "no", "wrong"):
            why = rest.split(None, 3)[3] if len(words) > 3 else "rejected by the human"
            L.verify(pid, False, why, actor="human")
            return (f"{pid}: candidate rejected ({why}).",
                    f"The human rejected the candidate for {pid}: {why}. It is open again.", False)
        if verdict not in ("accept", "ok", "yes", "correct"):   # never read an unclear word as "accept"
            raise LedgerError("usage: beacon: verify P1   |   beacon: verify P1 reject <why>")
        r = L.verify(pid, True, "verified by the human in chat", actor="human") or {}
        return (f"{pid}: candidate accepted, problem done." + dropped_note(L, r.get("dropped")),
                f"The human verified {pid}: it is done.", False)
    if cmd == "reopen":
        if len(words) < 2:
            raise LedgerError("usage: beacon: reopen NODE [note]")
        note = rest.split(None, 2)[2] if len(words) > 2 else ""
        L.reopen(words[1], note, actor="human")
        return (f"{words[1]} reopened.", f"The human reopened {words[1]}" + (f": {note}" if note else "."), False)
    if cmd == "add":
        if len(words) < 3:
            raise LedgerError("usage: beacon: add PARENT <idea>   (optionally: beacon: add P1 approach <idea>)")
        parent, kind, title = words[1], None, rest.split(None, 2)[2]
        if words[2].lower() in ("solution", "approach", "hypothesis") and len(words) == 3:
            raise LedgerError(f"usage: beacon: add {parent} {words[2].lower()} <idea>")
        if words[2].lower() in ("solution", "approach", "hypothesis") and len(words) > 3:
            kind = "solution" if words[2].lower() in ("solution", "hypothesis") else "approach"
            title = rest.split(None, 3)[3]
        nid = L.add_node(parent, title, rationale="suggested by the human", kind=kind, priority=5,
                         actor="human")
        return (f"added {nid}: {title}",
                f"The human added {nid} under {parent}: \"{title}\". Treat it as a serious lead: start_node it "
                "when you reach it (it has a high priority).", False)
    if cmd in ("accept", "reject"):
        if len(words) < 2 or (cmd == "reject" and len(words) < 3):
            raise LedgerError(f"usage: beacon: {cmd} P4" + (" <why>" if cmd == "reject" else ""))
        why = rest.split(None, 2)[2] if len(words) > 2 else ""
        L.accept_problem(words[1], accept=cmd == "accept", note=why, actor="human")
        if cmd == "accept":
            return (f"{words[1]} accepted: it is part of the run now.",
                    f"The human accepted {words[1]} \"{L.node(words[1])['title']}\": it is an open problem now.", False)
        return (f"{words[1]} rejected.", f"The human rejected the proposed problem {words[1]}: {why}. Do not work on it.",
                False)
    if cmd == "pick":
        if len(words) < 2:
            raise LedgerError("usage: beacon: pick NODE   (e.g. beacon: pick P1.A3)")
        L.pick(words[1], actor="human")
        n = L.node(words[1])
        return (f"{words[1]} goes next.",
                f"The human picked {words[1]} \"{n['title']}\" to go next. Record what you have on the current "
                "step (attach_evidence), then start_node it.", False)
    if cmd == "abandon":
        if len(words) < 3:
            raise LedgerError("usage: beacon: abandon NODE <why>")
        why = rest.split(None, 2)[2]
        L.set_status(words[1], "abandoned", reason=why, actor="human")
        return (f"{words[1]} abandoned.", f"The human abandoned {words[1]}: {why}. Do not work on it.", False)
    if cmd == "lighthouse":
        note = rest.split(None, 1)[1] if len(words) > 1 else ""
        L.set_human_flag("lighthouse_request", {"note": note[:1000]})
        return ("lighthouse review queued: it runs at Claude's next stop and its ideas join the ledger.",
                "The human asked the lighthouse to review the ledger; it runs when you next stop. Carry on with "
                "your current node meanwhile.", False)
    if cmd == "pause":
        L.set_human_flag("paused", True)
        return ("paused: beacon lets Claude stop at its next stop and pushes nothing. `beacon: resume` ends it.",
                "", True)
    if cmd == "resume":
        L.set_human_flag("paused", None)
        nxt = L.next_node()
        return ("resumed: beacon keeps Claude going again.",
                "Supervision is back on." + (f" Continue with {nxt['id']} \"{nxt['title']}\"." if nxt else ""),
                False)
    return None


def _answer(L, qid, text):
    r = L.answer(qid, text, actor="human")
    L.mark_delivered([qid])          # Claude reads the note below in this very turn
    extra = f"; {', '.join(r['reopened'])} unblocked" if r["reopened"] else ""
    return (f"Recorded as the answer to question #{qid}{extra}.",
            f"The human answered question #{qid}: {text}" + (f" ({', '.join(r['reopened'])} is unblocked)"
                                                              if r["reopened"] else ""))


# Claude Code submits some turns itself: a background task that finishes arrives as
# "<task-notification>...</task-notification>" (measured 2026-11-12 on 2.1.322). Not the human's words: never a
# command, never a reply. With one open question it would otherwise have been recorded as the human's answer.
SYSTEM_TURN = re.compile(r"\s*<([a-z][a-z0-9]*(?:-[a-z0-9]+)+)>.*</\1>", re.S)


def dropped_note(L, qids):
    """What a verification closed that the human may still care about (measured 2026-11-13: verifying P10 made
    question #8 obsolete without a word; the human answered it later and nothing was recorded)."""
    if not qids:
        return ""
    qs = {q["id"]: q for q in L.questions()}
    return (" Closed with it, no longer waiting on you: " + "; ".join(
        f"#{i} {(qs[i]['question'] if i in qs else '')[:90]}" for i in qids)
            + ". If one still matters, reply `#N …` anyway: it goes to Claude as your instruction.")


def handle(L, prompt, waiting_for_human=False, opener=None):
    """-> None (nothing for beacon) or a dict:
         {"block": True,  "show": text}            answered by beacon; the prompt stops here
         {"block": False, "show": text, "note": s}  recorded; the prompt goes on with `note` for Claude
         "changed": True                            the ledger changed (refresh the graph)
    Raises nothing for a bad command: it is shown and the prompt stops (it was meant for beacon)."""
    prompt = prompt or ""
    if SYSTEM_TURN.match(prompt):
        return None
    m = PREFIX.match(prompt)
    if m:
        rest = prompt[m.end():].strip()
        words = rest.split()
        try:
            text = view(L, words, opener=opener)
            if text is not None:
                return {"block": True, "show": text}
            done = act(L, words, rest)
            if done is None:
                return {"block": True, "show": f"unknown beacon command {words[0]!r}.\n\n{HELP}"}
            L.record("chat_command", {"command": rest[:300]}, actor="human")
            show, note, block = done
            out = {"block": block, "show": "beacon ▸ " + show, "changed": True}
            if note:
                out["note"] = note
            return out
        except (LedgerError, ValueError, IndexError, sqlite3.Error) as e:
            return {"block": True, "show": f"beacon: {e}"}

    if prompt.lstrip().startswith(("/", "!")):       # slash commands and shell mode are not replies
        return None
    open_qs = L.questions("open")
    m = REPLY.match(prompt)
    if m and not any(q["id"] == int(m.group(1)) for q in open_qs):
        # A reply to a question that is no longer open: say so, instead of letting it pass as if recorded.
        old = next((q for q in L.questions() if q["id"] == int(m.group(1))), None)
        if old is not None:
            return {"block": False, "show": f"beacon ▸ #{old['id']} is not open any more ({old['status']}: "
                                            f"{(old['answer'] or '')[:160]}); your message goes to Claude as an "
                                            "ordinary message and is not recorded as an answer.",
                    "note": f"The human replied to question #{old['id']}, which was already {old['status']}; treat "
                            "the reply as their instruction."}
    if not open_qs:
        return None
    if m and any(q["id"] == int(m.group(1)) for q in open_qs):
        qid = int(m.group(1))
        if not m.group(2).strip():
            return {"block": True, "show": f"beacon: write the answer after #{qid}, e.g. `#{qid} use the emulator`."}
        show, note = _answer(L, qid, m.group(2).strip())
        L.record("chat_answer", {"qid": qid}, actor="human")
        return {"block": False, "show": "beacon ▸ " + show, "note": note, "changed": True}
    if waiting_for_human and len(open_qs) == 1 and prompt.strip():
        q = open_qs[0]
        show, note = _answer(L, q["id"], prompt.strip())
        L.record("chat_answer", {"qid": q["id"], "implicit": True}, actor="human")
        return {"block": False, "note": note, "changed": True,
                "show": f"beacon ▸ {show} (Claude was waiting on it; write `#N …` to be explicit.)"}
    ids = ", ".join(f"#{q['id']}" for q in open_qs)
    return {"block": False, "show": f"beacon ▸ open question(s) {ids}; to record a reply as the answer, start it "
                                     f"with `#N` (`beacon: questions` lists them).",
            "note": "Open questions to the human (unanswered in the ledger):\n" + _questions(L)}
