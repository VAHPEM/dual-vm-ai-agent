# -*- coding: utf-8 -*-
"""VMResult, the plain-data result every labhand tool returns, and set_matcher."""
import re
from dataclasses import dataclass, field

SLICE = 4000
MATCH_LABEL = "MATCH"

# By default nothing matches; a caller injects its own matcher with set_matcher().
_MATCHER = [lambda text: None]


def set_matcher(fn):
    """Teach labhand once what a caller-defined "win string" looks like: `fn(text)` returns
    the matched string or None; tools then put it in VMResult.match. The library itself
    has no notion of one."""
    _MATCHER[0] = fn


def _find_match(text):
    return _MATCHER[0](text or "")


@dataclass
class VMResult:
    """What every labhand tool returns. Field names are a stable contract: callers read them
    directly.

    ok       did the call succeed
    summary  one short line (headline)
    detail   the raw output (large output spills to a file on the host)
    known    measured facts worth keeping
    match    what the matcher injected with set_matcher() found in the raw output, else None
             (the library itself has no notion of what to look for)
    info     the text actually shown
    image    {media_type, data} for picture tools, else None
    waiting  the step is legitimately waiting on a running job

    `flag` is the deprecated, pre-rename name of `match`: VMResult(..., flag=x) and r.flag
    still work (beacon and the research harness read it) and mean exactly `match`.
    """
    ok: bool
    summary: str
    detail: str = ""
    known: list = field(default_factory=list)
    match: str = None
    info: str = None
    image: dict = None
    waiting: bool = False


_dataclass_init = VMResult.__init__


def _init(self, *args, flag=None, **kwargs):
    _dataclass_init(self, *args, **kwargs)
    if flag is not None and self.match is None:
        self.match = flag


_init.__doc__ = _dataclass_init.__doc__
VMResult.__init__ = _init
VMResult.flag = property(lambda self: self.match,
                         lambda self, value: setattr(self, "match", value),
                         doc="Deprecated alias of `match`.")


# _spill / _save_to_host live in output.py; imported lazily to avoid an import cycle.
def _spill(*a, **k):
    from .output import _spill as f
    return f(*a, **k)

def _save_to_host(*a, **k):
    from .output import _save_to_host as f
    return f(*a, **k)
