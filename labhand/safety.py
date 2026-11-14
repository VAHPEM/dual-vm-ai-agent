# -*- coding: utf-8 -*-
"""labhand.safety — the last-ditch command filter: refuse only what wrecks a lab disk.

The lab VMs are a free-action malware-analysis lab (decided 2026-11-14): turning off
Windows Defender, adding AV exclusions, changing the firewall, the registry, services,
ACLs or accounts, running `cipher`, or opening a root shell are all legitimate analysis
steps there, so none of them is filtered any more. The safety boundary is the lab's
NETWORK ISOLATION, set by the human in UTM (docs/LAB-ISOLATION.md), not this filter.

What stays refused is exactly three things that destroy a VM's disk and are never
needed for analysis (a revert to a snapshot recovers from them, but costs the run):
  * a recursive delete of the filesystem root (`rm -rf /`, `rm -r -f /*`, ...)
  * formatting / partitioning (`mkfs*`, `diskpart`)
  * `format X:` on Windows
"""
import re

# A word only counts as a COMMAND when it stands in command position: start of a line
# (after optional whitespace), right after a shell separator (; & | and the `(` / `{` /
# backtick of a subshell or group), or right after sudo/doas/xargs. Measured 2026-09-22:
# bare-word patterns blocked ordinary RE work (`grep -r mkfs extracted/`, a solver whose
# comment mentioned a command). `(?!\s*=)` spares a python assignment such as `mkfs = 1`.
_CMDPOS = r"(?:^\s*|[\n;&|({`]\s*|\bsudo\s+(?:-\S+\s+)*|\bdoas\s+|\bxargs\s+)"

# rm with a recursive flag anywhere among its options (-r, -R, -rf, -fr, -Rf, --recursive,
# also split as `-r -f`), optionally --no-preserve-root, aimed at `/` or `/*`.
_RM_ROOT = (_CMDPOS + r"rm\s+(?=(?:-\S+\s+)*?(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\s)"
            r"(?:-\S+\s+)+([\"']?)/\*?\1(?=\s|$|[;&|)])")

_BLOCKED_PATTERNS = [
    (_RM_ROOT, "recursive delete of filesystem root"),
    (_CMDPOS + r"(?:mkfs(?:\.\w+)?|diskpart(?:\.exe)?)(?!\s*=)(?=\s|$|[;&|)])",
     "disk formatting/partitioning"),
    (_CMDPOS + r"format(?:\.com)?\s+[a-zA-Z]:", "Windows disk format"),
]


def _security_block(text):
    """Return a human reason if `text` (a run_cmd command or a run_script/solver body)
    would wreck a lab disk, else None. Checked BEFORE anything reaches a VM."""
    for pat, why in _BLOCKED_PATTERNS:
        if re.search(pat, text or "", re.IGNORECASE | re.MULTILINE):
            return why
    return None
