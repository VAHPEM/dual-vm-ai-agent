import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from beacon.ledger import Ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_api(monkeypatch):
    """Unit tests never call the real Claude API, even on a machine with a key in the environment."""
    for var in ("BEACON_API_KEY", "ANTHROPIC_API_KEY", "BEACON_API_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def make_run(tmp_path):
    """make_run(problems=..., **config) -> Ledger in a fresh run directory."""
    made = []

    def _make(problems=None, **config):
        problems = problems or [{"title": "Solve challenge one"}, {"title": "Triage crash two"}]
        L = Ledger.create(str(tmp_path / f"run{len(made)}"), "test run", problems, config=config,
                          workdir=str(tmp_path))
        made.append(L)
        return L

    yield _make
    for L in made:
        L.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The one API retry waits a few seconds in real use; not in the tests."""
    from beacon import lighthouse
    monkeypatch.setattr(lighthouse, "_sleep", lambda s: None)
