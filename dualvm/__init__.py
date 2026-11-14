# -*- coding: utf-8 -*-
"""Deprecated alias of `labhand` (the package was renamed `dualvm` -> `labhand`).

`import dualvm`, `import dualvm.shell`, `from dualvm import windows` and
`python -m dualvm.mcp_server` keep working: every `dualvm.X` is the very same module
object as `labhand.X` (not a copy), so state such as the SSH pool or an injected
matcher is shared between the two names. Importing this package issues one
DeprecationWarning. New code imports `labhand`.
"""
import importlib
import importlib.abc
import importlib.util
import sys
import warnings

import labhand as _labhand
from labhand import *  # noqa: F401,F403
from labhand import VMResult, set_matcher  # noqa: F401

warnings.warn("the 'dualvm' package was renamed 'labhand'; import labhand instead",
              DeprecationWarning, stacklevel=2)

_OLD, _NEW = __name__ + ".", "labhand."


class _AliasLoader(importlib.abc.Loader):
    """Hand back the already-imported labhand.X as dualvm.X."""

    def __init__(self, target):
        self.target = target

    def create_module(self, spec):
        return importlib.import_module(self.target)

    def exec_module(self, module):
        # The labhand module is already executed; importlib only has to register it under
        # the old name. Re-running it would build a second SSH pool / second MCP server.
        pass

    # `python -m dualvm.X`: runpy asks the loader for the code and runs it as __main__.
    def get_code(self, fullname):
        spec = importlib.util.find_spec(self.target)
        return spec.loader.get_code(self.target)

    def is_package(self, fullname):
        spec = importlib.util.find_spec(self.target)
        return spec.submodule_search_locations is not None


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD):
            return None
        new = _NEW + fullname[len(_OLD):]
        if importlib.util.find_spec(new) is None:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(new))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name):
    # `dualvm.shell` after a bare `import dualvm` (labhand/__init__ does not import its
    # submodules, so neither does this): import it on first access, like a package would.
    try:
        return importlib.import_module(_OLD + name)
    except ModuleNotFoundError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
