import sqlite3

from beacon.gate import on_stop, possible_refusal


def stop(L, text="Done for now.", lighthouse=None, now=None):
    return on_stop(L, {"session_id": "s1", "last_assistant_message": text}, lighthouse=lighthouse, now=now)


class FakeLighthouse:
    def __init__(self, add=True):
        self.add, self.calls = add, []

    def __call__(self, L, task, focus, cfg):
        self.calls.append((task, list(focus)))
        if not self.add:
            return {"status": "empty", "added": [], "detail": "nothing new"}
        nid = L.add_node("P1", f"lighthouse idea {len(self.calls)} for P1", kind="approach",
                         source="lighthouse", actor="lighthouse")
        return {"status": "ok", "added": [nid], "added_desc": [nid]}


def test_blocks_with_next_node(make_run):
    L = make_run()
    a = L.add_node("P1", "emulate the routine", kind="approach")
    L.start(a)
    d = stop(L)
    assert d.action == "block"
    assert a in d.reason and "still open" in d.reason
    assert L.gate_state()["forced"] == 1
    assert L.verify_integrity()[0]


def test_allows_when_nothing_open(make_run):
    L = make_run([{"title": "Only problem", "status": "done"}])
    assert stop(L).why == "no_open_problems"


def test_stall_calls_lighthouse_then_resets(make_run):
    L = make_run(k_stall=2, k_idle=99)
    a = L.add_node("P1", "a running approach", kind="approach")
    L.start(a)
    lh = FakeLighthouse()
    for i in range(2):  # evidence each round keeps idle at 0, but nothing concludes
        L.attach_evidence(a, f"output {i}", content=f"out {i}")
        d = stop(L, lighthouse=lh)
    assert d.action == "block" and lh.calls and lh.calls[0][0] == "stall"
    assert "lighthouse reviewer queued" in d.reason
    g = L.gate_state()
    assert g["stall"] == 0 and g["lighthouse_calls"] == 1


def test_idle_calls_lighthouse_sooner(make_run):
    L = make_run(k_stall=99, k_idle=2)
    lh = FakeLighthouse()
    stop(L, lighthouse=lh)
    d = stop(L, lighthouse=lh)
    assert lh.calls and lh.calls[0][0] == "idle" and d.action == "block"


def test_progress_resets_stall(make_run):
    L = make_run(k_stall=2, k_idle=99)
    lh = FakeLighthouse()
    a = L.add_node("P1", "first approach here", kind="approach")
    b = L.add_node("P1", "second approach here", kind="approach")
    stop(L, lighthouse=lh)
    L.set_status(a, "dead_end", reason="no xrefs to the string", assumption="the string is used directly")
    stop(L, lighthouse=lh)
    L.set_status(b, "superseded", reason="covered by the other path")
    stop(L, lighthouse=lh)
    assert not lh.calls


def test_dry_lighthouse_ends_the_run(make_run):
    L = make_run(k_stall=1, k_idle=1, lighthouse_dry_max=2)
    lh = FakeLighthouse(add=False)
    d1 = stop(L, lighthouse=lh)
    assert d1.action == "block" and "found nothing new" in d1.reason
    d2 = stop(L, lighthouse=lh)
    assert d2.action == "allow" and d2.why == "lighthouse_dry"


def test_no_lighthouse_means_escalate(make_run):
    L = make_run(k_stall=1, k_idle=1)
    assert stop(L, lighthouse=None).why == "lighthouse_exhausted"


def test_hard_budget(make_run):
    L = make_run(soft_forced=100, hard_forced=3, k_stall=99, k_idle=99)
    whys = [stop(L).why for _ in range(4)]
    assert whys == ["open_work"] * 3 + ["hard_budget"]


def test_soft_budget_extension_only_when_near_done_is_active(make_run):
    L = make_run(soft_forced=2, hard_forced=10, k_stall=99, k_idle=99, near_done_extension=0.5)
    a = L.add_node("P1", "decrypt the blob", kind="approach")
    L.start(a)
    L.attach_evidence(a, "partial plaintext", content="part 0")
    L.claim_near_done(a, ["fix the key offset"])
    assert stop(L).action == "block"
    L.attach_evidence(a, "more plaintext", content="part 1")
    assert stop(L).action == "block"
    L.attach_evidence(a, "even more", content="part 2")
    d = stop(L)                       # forced == soft_cap, near-done active -> extend once
    assert d.action == "block" and L.gate_state()["extension_used"]
    assert L.gate_state()["soft_cap"] == 3
    assert stop(L).why == "soft_budget"   # no new evidence, extension already used


def test_refusal_is_not_pushed(make_run):
    L = make_run()
    d = stop(L, text="I can't help with that request.")
    assert d.action == "allow" and d.why == "possible_refusal"
    long_analysis = ("The tool can't help with ARM64 here, so I switched approach. " * 40)
    assert not possible_refusal(long_analysis)


def test_integrity_failure_writes_nothing(make_run):
    L = make_run()
    db = sqlite3.connect(L.path)
    db.execute("UPDATE nodes SET status='done' WHERE kind='problem'")
    db.commit()
    db.close()
    before = L.last_seq()
    d = stop(L)
    assert d.action == "allow" and d.why == "integrity"
    assert L.last_seq() == before


def test_answers_are_delivered_once(make_run):
    L = make_run(k_stall=99, k_idle=99)
    a = L.add_node("P1", "run the sample dynamically", kind="approach")
    q = L.ask("need_permission", "May I execute the real sample?", node_id=a)
    L.answer(q["qid"], "No. Static and emulation only.")
    d1 = stop(L)
    assert "Static and emulation only" in d1.reason
    d2 = stop(L)
    assert "Static and emulation only" not in d2.reason


def test_waiting_job_is_not_idle(make_run):
    L = make_run(k_stall=99, k_idle=2)
    a = L.add_node("P1", "fuzz the parser", kind="approach")
    L.start(a)
    L.mark_waiting(a, "fuzz1", host="kali", max_minutes=30)
    lh = FakeLighthouse()
    for _ in range(3):
        d = stop(L, lighthouse=lh)
    assert not lh.calls and "fuzz1" in d.reason


def test_offers_and_questions_are_redirected(make_run):
    L = make_run()
    d = stop(L, text="I found the key schedule. If you want, I can also try hooking it with Frida?")
    assert "approach_choice" in d.reason and "effectiveness first, then cost" in d.reason
    assert 'ask(kind="need_info")' in d.reason and "approved by standing rule" in d.reason


def test_a_prose_choice_is_not_left_to_anyone(make_run):
    """"A or B?" at the end of a turn: no one is asked; the nudge says record both, take the best first."""
    L = make_run(k_stall=99, k_idle=99)
    text = "Có hai hướng: giả lập hàm giải mã, hoặc hook bằng Frida trên VM Windows. Mình nên làm hướng nào?"
    d = on_stop(L, {"session_id": "s", "last_assistant_message": text},
                classifier=lambda t, cfg: ("approach_choice", "giả lập hay hook?", "model"))
    assert d.action == "block" and not L.questions()               # not a question for the human
    assert "Do not wait" in d.reason and "decider model ranks them" in d.reason
    assert "effectiveness first, then cost" in d.reason and 'ask(kind="need_info")' not in d.reason
    # a permission question is approved by the standing rule (not sent to the human) ...
    L2 = make_run(k_stall=99, k_idle=99)
    d = on_stop(L2, {"session_id": "s", "last_assistant_message": "Can I run the unpacker on the VM?"},
                classifier=lambda t, cfg: ("need_permission", "run the unpacker on the VM?", "model"))
    assert not L2.questions("open") and "Approved" in d.reason and "Do not wait" not in d.reason
    # ... unless the human set permission_policy=ask
    L3 = make_run(k_stall=99, k_idle=99, permission_policy="ask")
    on_stop(L3, {"session_id": "s", "last_assistant_message": "Can I run the unpacker on the VM?"},
            classifier=lambda t, cfg: ("need_permission", "run the unpacker on the VM?", "model"))
    assert L3.questions("open")[0]["kind"] == "need_permission"


def test_near_done_audit(make_run):
    L = make_run(k_stall=99, k_idle=99, near_done_claims_max=2)
    a = L.add_node("P1", "decrypt the blob", kind="approach")
    L.start(a)
    L.attach_evidence(a, "partial plaintext", content="p0")
    L.claim_near_done(a, ["one more step"])
    stop(L)
    L.claim_near_done(a, ["one more step, really"])
    lh = FakeLighthouse()
    stop(L, lighthouse=lh)
    assert lh.calls and lh.calls[0] == ("audit_near_done", [a])
    stop(L, lighthouse=lh)      # same claim count: not audited again
    assert len(lh.calls) == 1


def test_near_done_grace_is_bounded(make_run):
    L = make_run(k_stall=2, k_idle=99, near_done_grace=2, near_done_claims_max=99)
    a = L.add_node("P1", "decrypt the blob", kind="approach")
    L.start(a)
    L.claim_near_done(a, ["fix the key offset"])
    lh = FakeLighthouse()
    for i in range(3):  # fresh evidence every stop, but nothing ever concludes
        L.attach_evidence(a, f"attempt {i}", content=f"attempt {i}")
        stop(L, lighthouse=lh)
    assert not lh.calls                      # grace covers stops 1-2, stall reaches 1 at stop 3
    L.attach_evidence(a, "attempt 3", content="attempt 3")
    stop(L, lighthouse=lh)
    assert lh.calls and lh.calls[0][0] == "stall"


def test_progress_ends_a_dry_streak(make_run):
    L = make_run(k_stall=1, k_idle=99, lighthouse_dry_max=2)
    lh = FakeLighthouse(add=False)
    a = L.add_node("P1", "first approach here", kind="approach")
    assert stop(L, lighthouse=lh).action == "block"          # dry round 1
    L.set_status(a, "dead_end", reason="nothing at that offset", assumption="the key sits at 0x40")
    assert stop(L, lighthouse=lh).action == "block"          # progress: streak reset, no call
    assert L.gate_state()["lighthouse_dry"] == 0
    assert stop(L, lighthouse=lh).action == "block"          # stuck again: a fresh dry round 1
