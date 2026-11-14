# -*- coding: utf-8 -*-
"""The rename dualvm -> labhand: the old import name, `python -m dualvm.mcp_server`,
the dualvm-mcp script and the DUALVM_* env vars keep working. Each import check runs in
a fresh interpreter so the one-time DeprecationWarning and sys.modules start clean."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _py(code, *args):
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run([sys.executable, *args, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_old_submodules_are_the_labhand_module_objects():
    r = _py("import dualvm.shell, labhand.shell, dualvm.remote, labhand.remote\n"
            "from dualvm import windows, jobs, linux, safety, _result\n"
            "import labhand, dualvm\n"
            "assert dualvm.shell is labhand.shell\n"
            "assert dualvm.remote is labhand.remote\n"
            "assert windows is labhand.windows and jobs is labhand.jobs\n"
            "assert linux is labhand.linux and safety is labhand.safety\n"
            "assert _result is labhand._result\n"
            "assert dualvm.VMResult is labhand.VMResult\n"
            "assert dualvm.set_matcher is labhand.set_matcher\n"
            "import sys; assert sys.modules['dualvm.shell'] is sys.modules['labhand.shell']\n"
            "print('ok')")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_attribute_access_after_bare_import():
    r = _py("import dualvm, labhand.jobs\n"
            "assert dualvm.jobs is labhand.jobs\n"
            "try:\n"
            "    dualvm.no_such_module\n"
            "except AttributeError:\n"
            "    print('ok')\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_missing_submodule_still_raises_module_not_found():
    r = _py("try:\n"
            "    import dualvm.no_such_module\n"
            "except ModuleNotFoundError:\n"
            "    print('ok')\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_exactly_one_deprecation_warning():
    r = _py("import warnings\n"
            "with warnings.catch_warnings(record=True) as w:\n"
            "    warnings.simplefilter('always')\n"
            "    import dualvm, dualvm.shell, dualvm.jobs\n"
            "    from dualvm import windows, linux\n"
            "    import dualvm\n"
            "dep = [x for x in w if issubclass(x.category, DeprecationWarning)\n"
            "       and 'labhand' in str(x.message)]\n"
            "print(len(dep))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1"


def test_labhand_import_does_not_warn():
    r = _py("import labhand, labhand.shell, labhand.jobs", "-W", "error::DeprecationWarning")
    assert r.returncode == 0, r.stderr


def test_env_prefers_labhand_names_and_falls_back_to_dualvm(monkeypatch):
    from labhand import remote
    monkeypatch.delenv("LABHAND_X", raising=False)
    monkeypatch.delenv("DUALVM_X", raising=False)
    assert remote._env_path("LABHAND_X", "DUALVM_X", "d") == "d"
    monkeypatch.setenv("DUALVM_X", "old")
    assert remote._env_path("LABHAND_X", "DUALVM_X", "d") == "old"
    monkeypatch.setenv("LABHAND_X", "new")
    assert remote._env_path("LABHAND_X", "DUALVM_X", "d") == "new"


def test_both_console_scripts_point_at_labhand():
    tomllib = pytest.importorskip("tomllib")
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        proj = tomllib.load(fh)
    assert proj["project"]["name"] == "labhand"
    scripts = proj["project"]["scripts"]
    assert scripts["labhand-mcp"] == scripts["dualvm-mcp"] == "labhand.mcp_server:main"
    assert {"labhand", "dualvm"} <= set(proj["tool"]["setuptools"]["packages"])


def test_python_m_dualvm_mcp_server_still_serves():
    pytest.importorskip("mcp")
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-m", "dualvm.mcp_server"], cwd=ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "labhand-tests", "version": "0"}}}
        out, err = proc.communicate(json.dumps(req) + "\n", timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    first = json.loads(out.splitlines()[0])
    assert first["result"]["serverInfo"]["name"] == "labhand", err


def test_default_key_prefers_labhand_then_the_single_existing_key(tmp_path):
    from labhand import remote
    keys = tmp_path / "keys"
    keys.mkdir()
    assert remote._default_key(str(keys)).endswith("labhand_ed25519")      # nothing there
    (keys / "dualvm_ed25519").write_text("k")
    (keys / "dualvm_ed25519.pub").write_text("p")
    assert remote._default_key(str(keys)).endswith("dualvm_ed25519")        # pre-rename checkout
    (keys / "other_rsa").write_text("k")
    assert remote._default_key(str(keys)).endswith("labhand_ed25519")      # ambiguous: no guess
    (keys / "labhand_ed25519").write_text("k")
    assert remote._default_key(str(keys)).endswith("labhand_ed25519")
