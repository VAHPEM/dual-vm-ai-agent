# -*- coding: utf-8 -*-
"""Kieu tra ve thuan du lieu cho dualvm. Khong biet gi ve flag/Workspace."""
import re
from dataclasses import dataclass, field

SLICE = 4000
MATCH_LABEL = "MATCH"

# Mac dinh KHONG tim gi. caller tiem ham tim flag vao bang set_matcher().
_MATCHER = [lambda text: None]


def set_matcher(fn):
    """caller goi cai nay mot lan de dualvm biet cach nhan ra flag.
    Thu vien tu no khong co khai niem flag."""
    _MATCHER[0] = fn


def _find_match(text):
    return _MATCHER[0](text or "")


@dataclass
class VMResult:
    # Giu DUNG ten truong cua ToolResult cu (known/flag) de code chuyen sang
    # KHONG phai sua than ham. caller nhan VMResult va doc .known/.flag/.detail
    # y nhu truoc, roi dong goi thanh ToolResult cua no.
    ok: bool
    summary: str
    detail: str = ""
    known: list = field(default_factory=list)
    flag: str = None
    info: str = None
    image: dict = None
    waiting: bool = False

# _spill / _save_to_host dinh nghia trong output.py (cung nhom).
# Import tre de tranh vong import:
def _spill(*a, **k):
    from .output import _spill as f
    return f(*a, **k)

def _save_to_host(*a, **k):
    from .output import _save_to_host as f
    return f(*a, **k)
