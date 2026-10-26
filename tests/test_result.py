# -*- coding: utf-8 -*-
"""VMResult contract and the set_matcher extension point."""
import re

import pytest

import dualvm
from dualvm import _result
from dualvm._result import VMResult, _find_match


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
    assert _find_match("anything@flare-on.com") is None


def test_set_matcher_is_exported_and_used():
    dualvm.set_matcher(lambda t: (re.search(r"\w+@flare-on\.com", t) or [None])[0])
    assert _find_match("out: abc@flare-on.com\n") == "abc@flare-on.com"
    assert _find_match("nothing here") is None


def test_matcher_receives_empty_string_for_none():
    seen = []
    dualvm.set_matcher(lambda t: seen.append(t))
    _find_match(None)
    assert seen == [""]
