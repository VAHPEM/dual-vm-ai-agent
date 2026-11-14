# -*- coding: utf-8 -*-
"""VMResult contract and the set_matcher extension point."""
import re

import pytest

import labhand
from labhand import _result
from labhand._result import VMResult, _find_match


@pytest.fixture(autouse=True)
def _restore_matcher():
    saved = _result._MATCHER[0]
    yield
    _result._MATCHER[0] = saved


def test_vmresult_defaults():
    r = VMResult(True, "ok")
    assert (r.ok, r.summary, r.detail) == (True, "ok", "")
    assert r.known == [] and r.flag is None and r.info is None
    assert r.image is None and r.waiting is False


def test_vmresult_known_is_not_shared_between_instances():
    a, b = VMResult(True, "a"), VMResult(True, "b")
    a.known.append("x")
    assert b.known == []


def test_no_matcher_means_no_flag():
    assert _find_match("anything WIN{x}") is None


def test_set_matcher_is_exported_and_used():
    labhand.set_matcher(lambda t: (re.search(r"WIN\{[^}]+\}", t) or [None])[0])
    assert _find_match("out: WIN{abc}\n") == "WIN{abc}"
    assert _find_match("nothing here") is None


def test_matcher_receives_empty_string_for_none():
    seen = []
    labhand.set_matcher(lambda t: seen.append(t))
    _find_match(None)
    assert seen == [""]


def test_match_is_the_field_and_flag_its_deprecated_alias():
    r = VMResult(True, "ok", match="m1")
    assert r.match == "m1" and r.flag == "m1"
    old = VMResult(True, "ok", flag="f1")                   # pre-rename keyword still works
    assert old.match == "f1" and old.flag == "f1"
    old.flag = "f2"
    assert old.match == "f2"
    assert VMResult(True, "ok").flag is None
    assert "match='m1'" in repr(r)
    import dataclasses
    assert [f.name for f in dataclasses.fields(VMResult)][4] == "match"   # positional slot kept
    assert VMResult(True, "s", "d", [], "pos").match == "pos"
