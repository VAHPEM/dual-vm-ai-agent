# -*- coding: utf-8 -*-
"""Regenerate .claude/skills/binary-analysis/SKILL.md from dualvm/playbook.py
(the SINGLE SOURCE). Run after editing playbook.py:

    PYTHONPATH=. python scripts/gen_skill.py
"""
import os
from dualvm.playbook import ANALYSIS_DISCIPLINE, METHOD_PLAYBOOK

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, ".claude", "skills", "binary-analysis", "SKILL.md")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

FRONT = """---
name: binary-analysis
description: >-
  Disciplined methodology for analysing an unknown file, binary or program on the
  dual-VM lab: how to reason (state assumptions, measure before reading, static vs
  dynamic, invert instead of brute-force) and which VM tool to reach for at each
  step. Load it before analysing a target with dualvm. Covers reverse engineering,
  crypto, parameter reasoning, Python bytecode, large traces, and tooling gotchas.
---
"""

INTRO = """
# binary-analysis

Reasoning playbook for analysing an unknown target while driving the two VMs with
`dualvm`. The **discipline** (R1-R10) and the **technique notes** below are the
method; this short intro maps each move to the tool that performs it. Keep raw
measurement (the Muscle, `dualvm`) and conclusions (the caller) separate: the
library only runs things and returns raw output - the discipline here is the
caller's.

## How the method maps to dualvm tools

- **Cheap static survey:** `shell.run_cmd` for `file` / `xxd` / `strings` /
  `objdump` / `readelf` / `rabin2`; page a dumped blob with `shell.read_file`.
- **Structure / decompile:** run Ghidra or radare2 through `shell.run_cmd`; read a
  file a solver wrote on the VM back with `shell.read_file`.
- **Go dynamic** (the moment two static looks stop answering - the two-dead-ends
  rule): Linux ELF -> `linux.linux_gdb` (scripted gdb, ASLR off, fixed PIE base);
  Windows -> `windows.win_frida` (inspect/replace a value, or drive the UI);
  a headless Kali GUI -> `linux.linux_gui_run`.
- **See / read a GUI:** `windows.win_windows` (text, cheap) first, then
  `windows.win_screenshot`; render a PDF/image and look at the returned picture.
- **Write a solver that is efficient, not brute:** `shell.run_script` (print every
  value); a heavy search runs as a background job via `jobs.job_start` /
  `jobs.job_poll`.
- **Confirm a result:** the caller's matcher (`dualvm.set_matcher`) surfaces a
  target string in any raw output.
"""

with open(OUT, "w") as f:
    f.write(FRONT)
    f.write(INTRO.rstrip() + "\n\n")
    f.write(ANALYSIS_DISCIPLINE.strip() + "\n\n")
    f.write(METHOD_PLAYBOOK.strip() + "\n")

print("wrote", OUT, "(", os.path.getsize(OUT), "bytes )")
