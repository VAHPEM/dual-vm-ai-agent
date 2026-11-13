# -*- coding: utf-8 -*-
"""Work the worker lists as not done when it ends a turn becomes nodes it must do (beacon.followups),
plus the per-stop deadline and the Stop lock."""
import json
import os
import time

import pytest

from beacon import followups, lighthouse
from beacon.gate import on_stop, stop_lock
from beacon.ledger import LedgerError

LEFTOVER_MSG = ("Đã sửa xong phần chính.\n\n3. Những gì còn chưa xử lý\n"
                "* Chưa chạy test trên Windows VM.\n* Chưa có lời gọi Opus thật nào (cần API key).\n"
                "* Có thể thêm giao diện web sau này.")


def solve(L, pid):
    """Close a checklist problem the way the worker and the human would."""
    a = L.add_node(pid, f"approach for {pid}", kind="approach")
    L.start(a)
    eid, _ = L.attach_evidence(a, "output that answers it", content=f"answer {pid} {time.time()}")
    L.set_status(a, "done", evidence_ids=[eid])
    L.propose_done(pid, "the answer", [eid])
    L.verify(pid, True, "checked")


def items(*specs):
    out = []
    for kind, title, extra in specs:
        it = {"title": title, "detail": title, "kind": kind, "about": "", "existing": "", "question": ""}
        it.update(extra)
        out.append(it)
    return out


def fake_extractor(result_items, calls):
    def ex(text, cfg, context=None, calls_used=0):
        calls.append(text)
        return {"items": result_items, "source": "model", "error": "", "attempted": True}
    return ex


def stop(L, text, **kw):
    return on_stop(L, {"session_id": "s1", "last_assistant_message": text}, **kw)


# ------------------------------------------------------------------ spotting a list of leftovers
@pytest.mark.parametrize("text", [
    LEFTOVER_MSG, "Remaining work: run it on PE32.", "Known limitations: no MSVC support yet.",
    "Not tested on macOS.", "If you want, I can also add a GUI.", "TODO: handle the 32-bit case",
    "Bước tiếp: đo lại trên Kali.", "Còn thiếu test cho trường hợp rỗng.",
    "Còn chưa làm:\n- chưa chạy `./scripts/check.sh changed`", "Follow-ups:\n- rerun on PE32",
    "Còn chờ anh/chị #8.\n\nCòn chưa làm:\n- chưa thử trên Windows",
    "Hai test này fail vì HEAD chưa có `_selftest_elf`; hành vi cũ tôi đã đo tay (#16, #17).",   # evidence ids
])
def test_lists_leftovers_spots_unfinished_work(text):
    assert followups.lists_leftovers(text)


@pytest.mark.parametrize("text", [
    "Done. All 227 tests pass and the flag was verified.", "I need to do the decryption next, starting now.",
    "Đã xong, flag đã được xác minh.",
    # measured 2026-11-13: these set off follow-up calls that found nothing
    "Còn chờ anh/chị:\n- `beacon: accept P5` nếu muốn tôi xử lý P5.",
    "Phần còn lại của P6 là P6.S2, và nó cần anh/chị trả lời câu hỏi #5.",
    "P6 đã đóng: beacon trả về `P6 VERIFIED and done. follow-up: every item is closed`.",
    "Từ câu cuối lượt trước, beacon tự tạo follow-up P8 rồi chuyển nó thành câu hỏi #6.",
])
def test_lists_leftovers_ignores_plain_reports(text):
    assert not followups.lists_leftovers(text)


# ------------------------------------------------------------------ the model call
def test_extract_uses_schema_effort_and_filters_items():
    seen = {}

    def transport(body, key, timeout):
        seen["body"] = body
        reply = {"items": [
            {"title": "Run the test suite on the Windows VM", "detail": "x64 Python", "kind": "work",
             "about": "P1", "existing": "", "question": ""},
            {"title": "short", "detail": "", "kind": "work", "about": "", "existing": "", "question": ""},
            {"title": "A proper item with a bad kind", "detail": "", "kind": "maybe", "about": "",
             "existing": "", "question": ""}]}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(reply)}]}

    r = followups.extract(LEFTOVER_MSG, {"followup_effort": "medium"}, context="<ledger>P1</ledger>",
                          transport=transport, key="k")
    assert r["source"] == "model" and r["attempted"]
    assert [i["title"] for i in r["items"]] == ["Run the test suite on the Windows VM"]
    oc = seen["body"]["output_config"]
    assert oc["effort"] == "medium" and oc["format"]["type"] == "json_schema"
    assert "<capabilities>" in seen["body"]["messages"][0]["content"]


def test_extract_without_key_or_budget_makes_no_call():
    assert followups.extract(LEFTOVER_MSG, {}) == {"items": [], "source": "unavailable", "error": "no API key",
                                                   "attempted": False}
    called = []
    r = followups.extract(LEFTOVER_MSG, {"max_followup_calls": 2}, transport=lambda *a: called.append(1), key="k",
                          calls_used=2)
    assert r["source"] == "unavailable" and not r["attempted"] and not called


def test_extract_survives_an_api_error():
    def transport(body, key, timeout):
        raise RuntimeError("HTTP 400: bad request")
    r = followups.extract(LEFTOVER_MSG, {}, transport=transport, key="k")
    assert r["source"] == "unavailable" and r["attempted"] and "HTTP 400" in r["error"]


# ------------------------------------------------------------------ into the ledger
def test_gaps_of_a_closed_problem_get_a_followup_problem(make_run):
    L = make_run()
    solve(L, "P1")
    out = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1"}),
                                   ("work", "Measure the real Opus call cost", {"about": "P1"})), L.config())
    assert len(out["work"]) == 2 and len(out["problems"]) == 1
    fp = L.node(out["problems"][0])
    assert fp["source"] == "followup" and fp["status"] == "todo" and fp["title"].startswith("Follow-ups of P1:")
    assert all(L.node(n)["parent"] == fp["id"] for n in out["work"])
    # the same list again: nothing new; another item: same follow-up problem
    again = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1"}),
                                     ("work", "Check the macOS path too", {"about": "P1"})), L.config())
    assert again["skipped"][0][1].startswith("same as") and again["problems"] == [fp["id"]]


def test_items_of_a_problem_still_open_go_under_it(make_run):
    L = make_run()
    out = followups.apply(L, items(("work", "Try the emulation approach next", {"about": "P2"})), L.config())
    assert L.node(out["work"][0])["parent"] == "P2" and not any(
        p["source"] == "followup" for p in L.problems())


def test_needs_human_blocks_on_a_question_and_optional_waits_for_acceptance(make_run):
    L = make_run(problem_approval="human")
    solve(L, "P1")
    out = followups.apply(L, items(
        ("needs_human", "Get an API key for the real Opus test", {"about": "P1", "question": "Bạn đặt BEACON_API_KEY giúp mình?"}),
        ("optional", "Add a web dashboard for the ledger", {"about": "P1"})), L.config())
    (nid, qid), = out["human"]
    assert L.node(nid)["status"] == "blocked" and L.questions("open")[0]["id"] == qid
    opt = L.node(L.node(out["optional"][0])["parent"])
    assert opt["status"] == "proposed" and opt["title"].startswith("Optional follow-ups of P1")
    assert opt["id"] not in [n["id"] for n in L.workable()]


def test_existing_node_and_caps_are_respected(make_run):
    L = make_run(followup_max_total=1)
    a = L.add_node("P1", "decrypt the config blob", kind="approach")
    out = followups.apply(L, items(("work", "Decrypt the blob with the found key", {"existing": a}),
                                   ("work", "Run it on the Windows VM", {}),
                                   ("work", "Run it on the Kali VM", {})), L.config())
    assert out["skipped"][0] == ("Decrypt the blob with the found key", f"covered by {a}")
    assert len(out["work"]) == 1 and "followup_max_total=1" in out["skipped"][1][1]


def test_policy_propose_makes_everything_wait_for_the_human(make_run):
    L = make_run(followup_policy="propose")
    solve(L, "P1")
    out = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1"})), L.config())
    assert out["optional"] and L.node(out["problems"][0])["status"] == "proposed"


def test_followups_never_nest(make_run):
    L = make_run()
    solve(L, "P1")
    first = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1"})), L.config())
    fp = first["problems"][0]
    second = followups.apply(L, items(("work", "Also test the PE32 path", {"about": fp})), L.config())
    assert second["problems"] == [fp]           # an open follow-up of P1 takes it, no "follow-ups of P3"


def test_a_followup_problem_closes_itself_once_every_item_is_closed(make_run):
    L = make_run()
    solve(L, "P1")
    out = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1"}),
                                   ("work", "Run the test suite on the Kali VM", {"about": "P1"})), L.config())
    fp, (a, b) = out["problems"][0], out["work"]
    L.start(a)
    eid, _ = L.attach_evidence(a, "227 passed on Windows", content="227 passed win")
    L.set_status(a, "done", evidence_ids=[eid])
    with pytest.raises(LedgerError, match="still open"):
        L.propose_done(fp, "tests run on both VMs", [eid])
    L.set_status(b, "dead_end", reason="the Kali VM was unreachable all day", assumption="the VM is up when tested")
    r = L.propose_done(fp, "tests run on Windows; Kali unreachable", [eid])
    assert r["verified"] and L.node(fp)["status"] == "done"
    # a second round of gaps after that gets a fresh follow-up problem with its own title
    again = followups.apply(L, items(("work", "Retry the Kali VM run", {"about": "P1"})), L.config())
    assert again["problems"][0] != fp and L.node(again["problems"][0])["title"].endswith("(2)")


# ------------------------------------------------------------------ the gate
def test_gate_turns_a_final_list_into_work_and_keeps_going(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}])
    solve(L, "P1")
    calls = []
    ex = fake_extractor(items(("work", "Run the test suite on the Windows VM", {"about": "P1"}),
                              ("needs_human", "Get an API key for the real Opus test",
                               {"about": "P1", "question": "Please set BEACON_API_KEY"})), calls)
    d = stop(L, LEFTOVER_MSG, followup=ex)
    assert (d.action, d.why) == ("block", "open_work")
    assert "listed work that is not done" in d.reason and "need the human were asked" in d.reason
    assert d.classified["followups"]["work"]
    d2 = stop(L, LEFTOVER_MSG, followup=ex)      # the same message is read once
    assert len(calls) == 1 and d2.action == "block"


def test_gate_with_only_human_items_lets_the_session_stop(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}])
    solve(L, "P1")
    ex = fake_extractor(items(("needs_human", "Get an API key for the real Opus test",
                               {"about": "P1", "question": "Please set BEACON_API_KEY"})), [])
    d = stop(L, LEFTOVER_MSG, followup=ex)
    assert (d.action, d.why) == ("allow", "awaiting_human") and L.questions("open")


def test_gate_without_a_model_tells_the_worker_to_record_its_list(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}])
    solve(L, "P1")
    ex = lambda text, cfg: {"items": [], "source": "unavailable", "error": "no API key", "attempted": False}  # noqa: E731
    whys = [stop(L, LEFTOVER_MSG + f"\n(round {i})", followup=ex).why for i in range(5)]
    assert whys[:3] == ["leftovers_unrecorded"] * 3 and whys[3:] == ["no_open_problems"] * 2
    assert "A list of what is left is not a way to end a turn" in stop(
        make_run(problems=[{"title": "x problem"}]), LEFTOVER_MSG, followup=ex).reason


def test_gate_without_extractor_behaves_as_before(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}])
    solve(L, "P1")
    assert stop(L, LEFTOVER_MSG).why == "no_open_problems"


def test_followup_policy_off(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}], followup_policy="off")
    solve(L, "P1")
    calls = []
    assert stop(L, LEFTOVER_MSG, followup=fake_extractor([], calls)).why == "no_open_problems" and not calls


def test_followup_and_classifier_do_not_ask_the_human_twice(make_run):
    L = make_run()
    ex = fake_extractor(items(("needs_human", "Get the sample's password", {"about": "P1", "question": "What is the zip password?"})), [])

    def classifier(text, cfg, context=None, calls_used=0):
        return {"kind": "need_info", "question": "What is the zip password?", "source": "model", "note": "",
                "choice": "", "why": "", "real_sample": False}

    stop(L, "Còn thiếu mật khẩu file zip. What is the zip password?", followup=ex, classifier=classifier)
    assert len(L.questions("open")) == 1


def test_followup_nudge_names_propose_done_for_a_closed_followup(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}])
    solve(L, "P1")
    out = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1"})), L.config())
    a = out["work"][0]
    L.start(a)
    eid, _ = L.attach_evidence(a, "227 passed", content="227 passed w")
    L.set_status(a, "done", evidence_ids=[eid])
    d = stop(L, "Ran it.")
    assert d.action == "block" and f"propose_done({out['problems'][0]}" in d.reason


# ------------------------------------------------------------------ one clock per stop
def test_call_json_respects_the_deadline():
    called = []
    with pytest.raises(lighthouse.ModelError, match="no time left"):
        lighthouse.call_json("m", "s", "u", transport=lambda *a: called.append(1), key="k",
                             deadline=time.monotonic() + 5)
    assert not called


def test_call_json_caps_the_timeout_and_skips_a_retry_it_has_no_time_for():
    timeouts = []

    def transport(body, key, timeout):
        timeouts.append(timeout)
        raise RuntimeError("HTTP 529: overloaded")
    with pytest.raises(lighthouse.ModelError):
        lighthouse.call_json("m", "s", "u", timeout=120, transport=transport, key="k", deadline=time.monotonic() + 25)
    assert len(timeouts) == 1 and timeouts[0] <= 25


def test_lighthouse_is_postponed_not_burned_when_the_stop_has_no_time(make_run):
    L = make_run(k_stall=1, k_idle=1, stop_budget_seconds=30)
    L.start(L.add_node("P1", "fuzz the parser", kind="approach"))
    calls = []
    lh = lambda *a, **k: calls.append(1) or {"status": "empty", "added": []}  # noqa: E731
    d = stop(L, "still working", lighthouse=lh)
    d = stop(L, "still working", lighthouse=lh)
    assert d.action == "block" and not calls and L.gate_state()["lighthouse_calls"] == 0
    assert d.lighthouse.get("status") == "postponed"


# ------------------------------------------------------------------ one Stop decision at a time
def test_stop_lock_serialises(tmp_path):
    with stop_lock(str(tmp_path)) as first:
        assert first
        with stop_lock(str(tmp_path), timeout=0.3) as second:
            assert not second
    with stop_lock(str(tmp_path), timeout=0.3) as third:
        assert third
    assert os.path.exists(tmp_path / "gate.lock")


def test_questions_the_gate_asked_do_not_hide_the_next_question(make_run):
    L = make_run()
    ex = fake_extractor(items(("needs_human", "Get the sample's password", {"about": "P2", "question": "What is the zip password?"})), [])
    stop(L, "Còn thiếu mật khẩu file zip.", followup=ex)            # the gate asks the human
    seen = []

    def classifier(text, cfg, context=None, calls_used=0):
        seen.append(text)
        return {"kind": "approach_choice", "question": "A or B?", "source": "model", "note": "", "choice": "A",
                "why": "faster", "real_sample": False}

    stop(L, "Should I try static A or dynamic B?", classifier=classifier)
    assert seen                       # the worker's next question is still read


def test_a_crashing_reader_does_not_break_the_stop(make_run):
    L = make_run(problems=[{"title": "Fix the review findings"}])
    solve(L, "P1")

    def boom(text, cfg):
        raise ValueError("bad")
    d = stop(L, LEFTOVER_MSG, followup=boom)
    assert d.why == "leftovers_unrecorded"


def test_a_problem_is_never_the_node_that_covers_an_item(make_run):
    L = make_run()
    out = followups.apply(L, items(("work", "Run the test suite on the Windows VM", {"about": "P1", "existing": "P1"})),
                          L.config())
    assert out["work"] and not out["skipped"]


def test_leftovers_of_unaccepted_or_abandoned_problems_wait_for_the_human(make_run):
    L = make_run(problem_approval="human")
    prop = L.propose_problem("Second payload stage found", "seen in the unpacked dump")["id"]
    a = followups.apply(L, items(("work", "Decode the second stage config", {"about": prop})), L.config())
    assert L.node(a["optional"][0])["parent"] == prop and not a["work"]
    L.set_status("P2", "abandoned", reason="the human dropped the crash triage", actor="human")
    b = followups.apply(L, items(("work", "Minimise the crashing input", {"about": "P2"})), L.config())
    fp = L.node(L.node(b["optional"][0])["parent"])
    assert fp["status"] == "proposed" and fp["source"] == "followup" and not b["work"]


def test_the_lock_wait_counts_against_the_stop_budget(make_run):
    L = make_run(k_stall=1, k_idle=1)
    L.start(L.add_node("P1", "fuzz the parser", kind="approach"))
    calls = []
    lh = lambda *a, **k: calls.append(1) or {"status": "empty", "added": []}  # noqa: E731
    stop(L, "still working", lighthouse=lh)                # a review runs (plenty of time)
    before = len(calls)
    d = on_stop(L, {"session_id": "s1", "last_assistant_message": "still working"}, lighthouse=lh,
                started=time.monotonic() - 200)           # 200 s spent waiting for the lock
    assert d.lighthouse.get("status") == "postponed" and len(calls) == before


def test_session_start_names_every_beacon_tool(make_run):
    """The context SessionStart gives the worker lists the tools the MCP server really publishes."""
    from beacon.hooks import session_start
    from beacon.tools import TOOLS
    out = session_start(make_run().dir, {"session_id": "s", "source": "startup"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert all(t.__name__ in ctx for t in TOOLS), [t.__name__ for t in TOOLS if t.__name__ not in ctx]


def test_the_step_back_survives_a_long_nudge(make_run):
    """When the worker is stuck the step-back is the most important part of the nudge, so it comes first:
    the 3500-character cap must cut the end, never it."""
    review = "re-read chal1/README.txt; you ran strings three times and never opened the second section"
    long_desc = ["P1.A%d \"idea\" " % i + "z" * 900 for i in range(2, 7)]
    # (a) the lighthouse added ideas: its review leads (the counters restart, so no generic reminder)
    L = make_run(k_stall=4, k_idle=99)
    L.start(L.add_node("P1", "fuzz the parser", kind="approach"))
    lh = lambda *a, **k: {"status": "ok", "added": ["P1.A2"], "added_desc": long_desc, "review": review}  # noqa: E731
    last = [stop(L, "still working", lighthouse=lh).reason for _ in range(4)][-1]
    assert len(last) == 3500 and last.splitlines()[1].startswith("The lighthouse reviewer (Opus)") and review in last
    # (b) the lighthouse found nothing new: its review, then the generic step-back, both before the rest
    L = make_run(k_stall=4, k_idle=99, lighthouse_dry_max=9)
    L.start(L.add_node("P1", "fuzz the parser", kind="approach"))
    for i in range(12):
        L.add_node("P1", f"approach number {i} " + "x" * 300, kind="approach")
    lh = lambda *a, **k: {"status": "empty", "added": [], "review": review}  # noqa: E731
    last = [stop(L, "still working", lighthouse=lh).reason for _ in range(4)][-1]
    lines = last.splitlines()
    assert lines[1].startswith("The lighthouse reviewer (Opus)") and lines[2].startswith("You seem stuck")


def test_the_lighthouse_reads_the_workers_steps(make_run, tmp_path):
    """The lighthouse prompt carries the session's tool calls and outputs, without the worker's prose."""
    from beacon.lighthouse import build_prompt
    L = make_run()
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I am fairly sure it is XOR"},
                                                      {"type": "tool_use", "name": "Bash", "id": "t1",
                                                       "input": {"command": "strings chal1.bin | head"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                  "content": "SECRET_SECTION_TWO"}]}}]) + "\n", encoding="utf-8")
    with open(os.path.join(L.dir, "sessions.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1, "session": "s1", "transcript_path": str(tr)}) + "\n")
    p = build_prompt(L, "stall")
    assert "<session>" in p and "strings chal1.bin" in p and "SECRET_SECTION_TWO" in p
    assert "fairly sure it is XOR" not in p                      # observations only: no reasoning
    L.set_config("lighthouse_transcript", "off", actor="human")
    assert "<session>" not in build_prompt(L, "stall")


def test_a_list_left_while_waiting_on_the_human_is_still_raised_later(make_run):
    """Measured on the first live run (no key): the worker ended a turn with "Còn chưa làm:" while waiting on two
    questions. The stop is let through (awaiting_human); the reminder to record the list must come with the next
    nudge, after the human answers, not be lost."""
    L = make_run(problems=[{"title": "Fix the review findings"}], k_stall=99, k_idle=99)
    a = L.add_node("P1", "ask the human first", kind="approach")
    L.start(a)
    qid = L.ask("need_info", "Which recoverer should the notes describe?", node_id=a)["qid"]
    ex = lambda text, cfg: {"items": [], "source": "unavailable", "error": "no API key", "attempted": False}  # noqa: E731
    d = stop(L, "Waiting for you.\n\nCòn chưa làm:\n- chưa chạy ./scripts/check.sh changed", followup=ex)
    assert d.why == "awaiting_human" and L.gate_state()["leftovers_pending"]
    L.answer(qid, "recover_stackstrings")
    d = stop(L, "Working on it.", followup=ex)
    assert d.action == "block" and "listed work that is not done" in d.reason
    assert not L.gate_state()["leftovers_pending"]
    assert "listed work that is not done" not in stop(L, "Still working.", followup=ex).reason   # said once


def test_a_failed_read_of_the_list_is_visible_in_the_log(make_run):
    """Measured 2026-11-12: the follow-up call failed on TLS and the stop record said nothing (classified: null)."""
    from beacon.logview import render_stop
    L = make_run(problems=[{"title": "Fix the review findings"}], k_stall=99, k_idle=99)
    ex = lambda text, cfg: {"items": [], "source": "unavailable", "attempted": True,  # noqa: E731
                            "error": "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed>"}
    d = stop(L, "Done for now.\n\nCòn chưa làm:\n- chưa chạy ./scripts/check.sh changed", followup=ex)
    assert "CERTIFICATE_VERIFY_FAILED" in d.classified["followups"]["error"]
    shown = render_stop({"ts": 0, "session": "s", "text": "x"},
                        {"action": d.action, "why": d.why, "reason": d.reason, "classified": d.classified})
    assert "unfinished-work list: not read by Opus" in shown and "CERTIFICATE_VERIFY_FAILED" in shown
    assert "question read by" not in shown
    assert L.gate_state()["followup_calls"] == 1


def test_system_changes_and_the_supervisor_itself_are_the_humans_call():
    """Measured 2026-11-12: the worker's report said the decider's TLS error "can be fixed with Python's Install
    Certificates.command"; it became a work node and the worker tried to change the system Python (root-owned,
    it failed). A system-wide change is a question for the human; a beacon problem is a note for the human."""
    assert "needs root or sudo" in followups.SYSTEM and "supervisor: a problem the worker reports" in followups.SYSTEM
    assert "the worker reports a supervisor problem, it does not fix it" in followups.SYSTEM.lower()
    assert "never a node or a question" in followups.SYSTEM and "supervisor" in followups.ITEM_KINDS
    assert "is the human's call" in followups.capabilities({})
    assert followups.capabilities({"worker_capabilities": "only the repo"}) == "only the repo"


def test_a_problem_waiting_for_the_humans_check_gets_no_second_question(make_run):
    """Measured 2026-11-12: the worker's report said "P3 is waiting for your review"; it became P5 "Follow-ups of
    P3" with question #9 "have you reviewed P3's file?", next to P3's own pending verification."""
    L = make_run(problems=[{"title": "Write the note"}, {"title": "Count the scripts"}], k_stall=99, k_idle=99)
    a = L.add_node("P1", "write it", kind="approach")
    L.start(a)
    eid, _ = L.attach_evidence(a, "wrote notes/x.md", content="five sentences")
    L.set_status(a, "done", evidence_ids=[eid])
    L.propose_done("P1", "notes/x.md", [eid])
    assert L.node("P1")["status"] == "pending_verification"
    qid = L.ask("need_info", "Which recoverer should the note describe?", node_id="P2")["qid"]
    before = len(L.problems())
    out = followups.apply(L, items(
        ("needs_human", "Duyệt file notes/x.md của P1", {"about": "P1", "question": "Bạn đã duyệt notes/x.md chưa?"}),
        ("needs_human", "Ask again", {"about": "P2", "question": "Which recoverer should the note describe?"}),
        ("work", "Run the gate once more", {"about": "P1"})), L.config())
    reasons = dict(out["skipped"])
    assert reasons["Duyệt file notes/x.md của P1"] == "P1 already waits for the human's verification"
    assert reasons["Ask again"] == f"already asked as #{qid}"
    assert out["human"] == [] and len(out["work"]) == 1
    new = L.problems()[before:]
    assert len(new) == 1 and "was handed to the human to verify" in new[0]["rationale"]
    assert "pending_verification is already the human's to check" in followups.SYSTEM


def test_evidence_for_a_closed_problem_is_refused_with_where_it_goes(make_run):
    """Measured 2026-11-12: the worker ran the three leftover checks after P1 was done and got only
    "REFUSED: problem P1 is done"."""
    L = make_run(problems=[{"title": "Run the gate"}], k_stall=99, k_idle=99)
    solve(L, "P1")
    for call in (lambda: L.attach_evidence("P1", "check.sh changed: rc=0", content="rc=0"),
                 lambda: L.add_node("P1", "run the selftest", kind="approach")):
        with pytest.raises(LedgerError, match="propose_problem"):
            call()


def test_the_tree_says_a_follow_up_problem_closes_without_the_human(make_run):
    """Measured 2026-11-12: P4/P5 ("Follow-ups of ...") showed "checked by: the human", but propose_done closes them."""
    L = make_run(problems=[{"title": "Run the gate"}], k_stall=99, k_idle=99)
    solve(L, "P1")
    out = followups.apply(L, items(("work", "Run the selftest of one module", {"about": "P1"})), L.config())
    pid = out["problems"][0]
    tree = L.render_tree()
    block = tree[tree.index(pid):]
    assert "checked by: beacon itself" in block.splitlines()[2]
    L.set_config("followup_autoverify", False)
    tree = L.render_tree()
    assert "checked by: the human" in tree[tree.index(pid):].splitlines()[2]



def test_remarks_about_beacon_go_to_the_report_not_to_questions(make_run):
    """Measured 2026-11-12: the closing report listed five beacon bugs; as needs_human items they became six
    questions (#10-#15) that kept a finished run waiting. Now: one report line each, once, no node, no question,
    and the stop is not held for them."""
    from beacon.logview import render_stop
    from beacon.report import render_report
    L = make_run(problems=[{"title": "Run the gate"}], k_stall=99, k_idle=99)
    solve(L, "P1")
    fb = items(("supervisor", "ledger_view hid the problem's note", {"detail": "proposed P1 wrong three times"}),
               ("supervisor", "Decider failed on TLS certificates", {}))
    calls = []
    d = stop(L, "Xong.\n\nCòn chưa làm:\n- beacon nên hiện note", followup=fake_extractor(fb, calls))
    assert d.action == "allow" and d.why == "no_open_problems"
    assert L.questions("open") == [] and len(L.problems()) == 1
    assert d.classified["followups"]["supervisor"] == ["ledger_view hid the problem's note",
                                                      "Decider failed on TLS certificates"]
    shown = render_stop({"ts": 0, "session": "s", "text": "x"}, {"action": d.action, "why": d.why,
                                                                "classified": d.classified})
    assert "2 about beacon (to the report)" in shown
    again = followups.apply(L, fb, L.config())
    assert again["supervisor"] == [] and dict(again["skipped"])["Decider failed on TLS certificates"] == "already reported"
    rep = render_report(L)
    assert "What Claude reported about beacon itself" in rep and "proposed P1 wrong three times" in rep
    assert rep.count("Decider failed on TLS certificates") == 1


def test_evidence_from_another_problem_is_refused_with_what_to_attach(make_run):
    """Measured 2026-11-12: propose_done(P2, ..., [1]) cited P1's measurement and got only "evidence [1] not found
    under P2"; re-attaching the same output under P2 would be a copy that does not count either."""
    L = make_run(problems=[{"title": "Measure"}, {"title": "Write it up"}], k_stall=99, k_idle=99)
    a = L.add_node("P1", "time the selftests", kind="approach")
    L.start(a)
    eid, _ = L.attach_evidence(a, "timings", content="decompiler_bridge 0.61 s")
    with pytest.raises(LedgerError, match="Attach P2's own output"):
        L.propose_done("P2", "notes/x.md", [eid])


def test_after_a_rejection_the_worker_can_mark_the_old_result_replaced(make_run):
    """Measured 2026-11-12: P2's candidate was rejected, P2.S2 redid the work, and the worker could not mark P2.S1
    superseded ("only the human can reopen it"). done -> superseded naming the replacement is allowed, once, without
    counting as progress; reopening and every other change of a closed node stay the human's."""
    L = make_run(problems=[{"title": "Write the table"}], k_stall=99, k_idle=99)
    old = L.add_node("P1", "time each selftest once", kind="approach")
    L.start(old)
    e1, _ = L.attach_evidence(old, "one run each", content="t1")
    L.set_status(old, "done", evidence_ids=[e1])
    L.propose_done("P1", "notes/x.md", [e1])
    L.verify("P1", False, "need 12 runs and medians", actor="human")
    new = L.add_node("P1", "time the top 4 twelve times", kind="approach")
    L.start(new)
    e2, _ = L.attach_evidence(new, "12 runs each", content="t2")
    L.set_status(new, "done", evidence_ids=[e2])
    with pytest.raises(LedgerError, match="superseded_by=<the node that replaced it>"):
        L.set_status(old, "superseded", reason="replaced after the rejection")
    with pytest.raises(LedgerError, match="another node of the same problem"):
        L.set_status(old, "superseded", reason="replaced after the rejection", superseded_by=old)
    before = L.db.execute("SELECT value FROM meta WHERE key='counters'").fetchone()
    L.set_status(old, "superseded", reason="replaced after the rejection", superseded_by=new)
    assert L.node(old)["status"] == "superseded" and L.node(old)["superseded_by"] == new
    assert L.db.execute("SELECT value FROM meta WHERE key='counters'").fetchone() == before
    for status in ("done", "todo", "dead_end"):
        with pytest.raises(LedgerError):
            L.set_status(old, status, reason="x" * 20, assumption="y" * 20)


def test_the_human_adding_to_a_closed_problem_is_told_to_reopen_it(make_run):
    L = make_run(problems=[{"title": "Run the gate"}], k_stall=99, k_idle=99)
    solve(L, "P1")
    with pytest.raises(LedgerError, match=r"reopen it first \(`beacon: reopen P1"):
        L.add_node("P1", "write a test for the missing file case", actor="human")
    with pytest.raises(LedgerError, match="propose_problem"):
        L.add_node("P1", "write a test for the missing file case", kind="approach")


def test_no_push_to_decide_what_the_follow_up_reader_left_to_the_human(make_run):
    """Measured 2026-11-12: "nếu muốn mở lại P3 thì là quyết định của anh/chị" became a proposal waiting for the human
    AND the heuristic push "Your last message left a choice to the human ... call ask(approach_choice)"."""
    L = make_run(problems=[{"title": "Fix the loader"}, {"title": "Old goal"}], k_stall=99, k_idle=99)
    L.set_status("P2", "abandoned", reason="not reachable within the limit", actor="human")
    L.start(L.add_node("P1", "patch it", kind="approach"))
    text = "Đã sửa P1.\n\nCòn chưa làm:\n- nếu muốn, mở lại P2 vì mục tiêu của nó giờ đã đạt"
    fu = items(("optional", "Mở lại P2: mục tiêu đã đạt nhờ P1", {"about": "P2"}))
    d = stop(L, text, followup=fake_extractor(fu, []))
    assert d.action == "block" and "left a choice to the human" not in d.reason
    assert "Items recorded for the human to accept first" in d.reason
    L2 = make_run(problems=[{"title": "Fix the loader"}], k_stall=99, k_idle=99)    # no reader: the push stays
    L2.start(L2.add_node("P1", "patch it", kind="approach"))
    assert "left a choice to the human" in stop(L2, "Patched. Nếu muốn, tôi có thể thêm test.").reason


def test_rejecting_a_proposed_problem_closes_its_items(make_run):
    """Measured 2026-11-13: `beacon: reject P7` left P7.S1 listed as todo under the abandoned P7."""
    L = make_run(problems=[{"title": "Run the gate"}], k_stall=99, k_idle=99, problem_approval="human")
    solve(L, "P1")
    out = followups.apply(L, items(("optional", "Add a web view for the report", {"about": "P1"})), L.config())
    pid, nid = out["problems"][0], out["optional"][0]
    assert L.node(pid)["status"] == "proposed" and L.node(nid)["status"] == "todo"
    L.accept_problem(pid, False, note="not needed for this run", actor="human")
    assert L.node(pid)["status"] == "abandoned" and L.node(nid)["status"] == "abandoned"
    assert L.verify_integrity()[0]


def test_a_closed_run_with_something_for_the_human_is_awaiting_human(make_run):
    """Measured 2026-11-13: P10 waited for `beacon: accept`, yet the stop was logged as no_open_problems."""
    L = make_run(problems=[{"title": "Run the gate"}], k_stall=99, k_idle=99, problem_approval="human")
    solve(L, "P1")
    assert stop(L, "All done.").why == "no_open_problems"
    L.propose_problem("Add a regression test for the error path", "a missing input must give rc=1, seen in P1")
    d = stop(L, "All done; P2 waits for you.")
    assert d.action == "allow" and d.why == "awaiting_human"
