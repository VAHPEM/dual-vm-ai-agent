import json
import os
import sqlite3

import pytest

from beacon.cli import parse_checklist
from beacon.ledger import Ledger, LedgerError


def test_create_and_integrity(make_run):
    L = make_run()
    assert [p["id"] for p in L.problems()] == ["P1", "P2"]
    assert L.verify_integrity()[0]
    with pytest.raises(LedgerError):
        Ledger.create(L.dir, "again", [{"title": "x" * 5}])


def test_add_node_shape_rules(make_run):
    L = make_run()
    s = L.add_node("P1", "key is derived from the timestamp")
    assert s == "P1.S1" and L.node(s)["kind"] == "solution"
    a = L.add_node(s, "emulate the decrypt routine")
    assert a == "P1.S1.A1" and L.node(a)["kind"] == "approach"
    assert L.add_node("P1", "strings survey", kind="approach") == "P1.A1"
    with pytest.raises(LedgerError):
        L.add_node(a, "approach under an approach")
    with pytest.raises(LedgerError):
        L.add_node(s, "solution under a solution", kind="solution")
    with pytest.raises(LedgerError):
        L.add_node("P1", "ab")  # too short


def test_start_opens_ancestors(make_run):
    L = make_run()
    a = L.add_node(L.add_node("P1", "hypothesis one"), "approach one")
    L.start(a)
    assert L.node(a)["status"] == "in_progress"
    assert L.node("P1.S1")["status"] == "in_progress"
    assert L.node("P1")["status"] == "in_progress"
    with pytest.raises(LedgerError):
        L.start(a)  # not todo any more


def test_dead_end_needs_reason_and_assumption(make_run):
    L = make_run()
    a = L.add_node("P1", "angr on the check function", kind="approach")
    with pytest.raises(LedgerError):
        L.set_status(a, "dead_end", reason="path explosion")
    with pytest.raises(LedgerError):
        L.set_status(a, "dead_end", reason="no", assumption="the check is in main")
    L.set_status(a, "dead_end", reason="path explosion after 10 min",
                 assumption="the check loop depends on all 32 input bytes")
    n = L.node(a)
    assert n["status"] == "dead_end" and "32 input bytes" in n["assumption"]
    with pytest.raises(LedgerError):
        L.set_status(a, "todo")  # worker cannot reopen
    with pytest.raises(LedgerError):
        L.add_node(a, "child of a dead end")


def test_done_needs_own_evidence(make_run):
    L = make_run()
    a1 = L.add_node("P1", "first approach here", kind="approach")
    a2 = L.add_node("P1", "second approach here", kind="approach")
    with pytest.raises(LedgerError):
        L.set_status(a1, "done")
    e1, d1 = L.attach_evidence(a1, "output of run", content="RAW OUTPUT 123")
    assert d1 is None
    e2, d2 = L.attach_evidence(a2, "same output again", content="RAW OUTPUT 123")
    assert d2 == e1
    with pytest.raises(LedgerError):  # a copy of evidence recorded elsewhere does not count
        L.set_status(a2, "done")
    L.set_status(a1, "done")
    assert L.node(a1)["status"] == "done"
    assert L.counter("unique_evidence") == 1


def test_problem_done_only_through_verification(make_run, tmp_path):
    L = make_run([{"title": "Solve the crackme", "verify_cmd": 'test "$BEACON_CANDIDATE" = "flag{ok}"'}])
    a = L.add_node("P1", "invert the transform", kind="approach")
    e, _ = L.attach_evidence(a, "inverted output", content="flag{ok} from inversion")
    with pytest.raises(LedgerError):
        L.set_status("P1", "done")
    r = L.propose_done("P1", "flag{wrong}", [e])
    assert r["verified"] is False and L.node("P1")["status"] == "in_progress"
    r = L.propose_done("P1", "flag{ok}", [e])
    assert r["verified"] is True and L.node("P1")["status"] == "done"
    # nothing under a solved problem stays open
    assert L.node(a)["status"] == "superseded"
    assert L.verify_integrity()[0]


def test_candidate_never_reaches_the_shell(make_run, tmp_path):
    marker = tmp_path / "pwned"
    L = make_run([{"title": "Solve the crackme", "verify_cmd": 'test "$BEACON_CANDIDATE" = "x"'}])
    a = L.add_node("P1", "some approach", kind="approach")
    e, _ = L.attach_evidence(a, "an output", content="zzz")
    L.propose_done("P1", f"$(touch {marker}); `touch {marker}`", [e])
    assert not marker.exists()


def test_no_verify_cmd_waits_for_human(make_run):
    L = make_run()
    a = L.add_node("P1", "some approach", kind="approach")
    e, _ = L.attach_evidence(a, "an output", content="yyy")
    r = L.propose_done("P1", "flag{maybe}", [e])
    assert r["status"] == "pending_verification"
    with pytest.raises(LedgerError):
        L.verify("P1", True, actor="worker")
    L.verify("P1", False, "decoy flag", actor="human")
    assert L.node("P1")["status"] == "in_progress"


def test_ask_approach_choice_and_questions(make_run):
    L = make_run()
    s = L.add_node("P1", "hypothesis about the check")
    r = L.ask("approach_choice", "angr or static read first?", node_id=s,
              options=["static read in Ghidra", "symbolic execution with angr"])
    assert len(r["created"]) == 2
    assert L.node(r["created"][0])["priority"] > L.node(r["created"][1])["priority"]
    assert not L.questions("open")
    a = r["created"][1]
    q = L.ask("need_permission", "May I execute the sample on the Windows VM?", node_id=a)
    assert L.node(a)["status"] == "blocked"
    with pytest.raises(LedgerError):
        L.answer(q["qid"], "yes", actor="worker")
    res = L.answer(q["qid"], "No: static and emulation only.")
    assert res["reopened"] == [a] and L.node(a)["status"] == "todo"


def test_near_done_claim(make_run):
    L = make_run()
    a = L.add_node("P1", "decrypt the blob", kind="approach")
    L.attach_evidence(a, "partial plaintext", content="abc")
    L.claim_near_done(a, ["recompute key with offset 0x40", "compare with flag format"])
    n = L.node(a)
    assert n["near_done_claims"] == 1 and n["near_done_evidence"] == 1
    assert json.loads(n["remaining"])[0].startswith("recompute")
    with pytest.raises(LedgerError):
        L.claim_near_done(a, [])


def test_tamper_detection(make_run):
    L = make_run()
    L.add_node("P1", "some hypothesis")
    db = sqlite3.connect(L.path)
    db.execute("UPDATE nodes SET status='done' WHERE id='P1'")
    db.commit()
    ok, why = L.verify_integrity()
    assert not ok and "digest" in why
    db.execute("UPDATE nodes SET status='todo' WHERE id='P1'")
    db.commit()
    assert L.verify_integrity()[0]
    db.execute("UPDATE events SET payload='{}' WHERE seq=2")
    db.commit()
    ok, why = L.verify_integrity()
    assert not ok and "altered" in why
    db.close()


def test_two_writers_keep_one_chain(make_run):
    L = make_run()
    other = Ledger.open(L.dir)
    for i in range(10):
        (L if i % 2 else other).add_node("P2", f"approach number {i}", kind="approach")
    other.close()
    ok, why = L.verify_integrity()
    assert ok, why
    assert len(L.children("P2")) == 10


def test_workable_and_next(make_run):
    L = make_run()
    assert [n["id"] for n in L.workable()] == ["P1", "P2"]   # problems with no approach yet
    a = L.add_node("P1", "first approach here", kind="approach")
    b = L.add_node("P1", "second approach here", kind="approach")
    L.start(b)
    assert L.next_node()["id"] == b
    ids = [n["id"] for n in L.workable()]
    assert a in ids and b in ids and "P1" not in ids and "P2" in ids


def test_parse_checklist():
    title, probs = parse_checklist("""# Flare-On 13 run
- [ ] Challenge 5: recover the flag
  verify: python3 check5.py
  note: the binary is a Rust PE
- [x] Challenge 1
- Challenge 6 triage
  extra context line
""")
    assert title == "Flare-On 13 run"
    assert [p["status"] for p in probs] == ["todo", "done", "todo"]
    assert probs[0]["verify_cmd"] == "python3 check5.py"
    assert "Rust PE" in probs[0]["rationale"]
    assert "extra context" in probs[2]["rationale"]


def test_run_dir_permissions(make_run):
    L = make_run()
    if os.name == "posix":
        assert oct(os.stat(L.dir).st_mode & 0o777) == "0o700"


def test_create_validates_problems(tmp_path):
    with pytest.raises(LedgerError):
        Ledger.create(str(tmp_path / "r"), "t", [{"rationale": "no title"}])
    with pytest.raises(LedgerError):
        Ledger.create(str(tmp_path / "r2"), "t", [])


def test_next_node_prefers_near_done_then_approaches(make_run):
    L = make_run()
    a = L.add_node("P1", "trace the flag buffer writes", kind="approach")
    L.start(a)
    L.attach_evidence(a, "two of three fragments", "frag1 frag2")
    L.claim_near_done(a, ["capture the third fragment"])
    s = L.add_node("P2", "the length field is attacker controlled")
    L.start(s)                                  # touched last, but a solution and not near-done
    assert L.next_node()["id"] == a
    b = L.add_node(s, "reproduce under the sanitizer", kind="approach")
    L.start(b)
    L.set_status(a, "dead_end", reason="the third fragment never appears", assumption="the timer check passes")
    assert L.next_node()["id"] == b             # an approach before its in-progress solution


def test_problem_waiting_on_the_human_is_not_workable(make_run):
    L = make_run()
    a = L.add_node("P1", "decrypt with the passphrase", kind="approach")
    L.start(a)
    assert any(n["id"] == a for n in L.workable())
    qid = L.ask("need_info", "What is the passphrase for data.bin?", node_id=a)["qid"]
    assert not [n for n in L.workable() if n["id"].startswith("P1")]     # waits on the human
    b = L.add_node("P2", "a blocked approach with no question", kind="approach")
    L.set_status(b, "blocked", reason="the VM is down right now")
    assert any(n["id"] == "P2" for n in L.workable())                    # still needs approaches
    L.answer(qid, "kiwi", actor="human")
    assert any(n["id"] == a for n in L.workable())
