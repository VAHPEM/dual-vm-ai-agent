# -*- coding: utf-8 -*-
"""Test setup: the unit tests never touch a real VM.

`labhand.remote` reads KALI_HOST/KALI_USER at import time and loads the repo's
`.env` by default. Point LABHAND_ENV / LABHAND_SSH_KEY (and the pre-rename DUALVM_* names) at paths that do not exist and
give the host vars placeholder values, so importing the package works on any
machine (CI, a laptop without the lab) and nothing can reach the real VMs.
"""
import os
import pathlib
import sys

_NOWHERE = "/nonexistent/labhand-tests"
for _p in ("LABHAND", "DUALVM"):
    os.environ[_p + "_ENV"] = _NOWHERE + "/.env"
    os.environ[_p + "_SSH_KEY"] = _NOWHERE + "/key"
for _k, _v in {"KALI_HOST": "kali.invalid", "KALI_USER": "test",
               "WIN_HOST": "windows.invalid", "WIN_USER": "test"}.items():
    os.environ[_k] = _v

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
