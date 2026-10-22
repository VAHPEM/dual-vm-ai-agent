# -*- coding: utf-8 -*-
"""dualvm.safety — split from the original monorepo (verbatim).
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
import re


# Commands that change SECURITY/SYSTEM config, never needed to solve a challenge (the
# official writeups never touch these) but seen once in practice: run 3 (ch4) had the
# Brain disable Windows Defender real-time protection + add a scan exclusion just to
# force a flagged sample to execute, instead of treating the AV block as information.
# Scope is deliberately narrow: EXECUTING the sample itself is still allowed.
# A word only counts as a COMMAND when it stands in command position: start of a
# line, right after a shell separator (; & |), or right after sudo/doas/xargs.
# Measured 2026-09-22: the old bare-word patterns blocked ordinary RE work -
# `cat /etc/passwd`, `grep -r 'useradd' extracted/`, `objdump ... | grep passwd`,
# `echo 'sc create' >> notes.txt`, and any solver whose COMMENT mentioned
# iptables - exactly the same failure mode as the `cipher` bug that killed the
# last 3 steps of the ch2 run. `(?!\s*=)` additionally spares python assignments
# (`iptables_rules = [...]`, `passwd = "..."`) at the start of a line.
_CMDPOS = r"(?:^|[\n;&|]\s*|\bsudo\s+|\bdoas\s+|\bxargs\s+)"


_BLOCKED_PATTERNS = [
    (r"\b-MpPreference\b", "Windows Defender config (Add/Set/Remove-MpPreference)"),
    (r"Disable-\w*(Protection|Defender|Realtime)", "disabling Windows security protection"),
    (r"netsh\s+advfirewall", "Windows firewall config"),
    (_CMDPOS + r"sc(\.exe)?\s+(stop|config|delete|create)\b", "Windows service control"),
    (_CMDPOS + r"reg(\.exe)?\s+(add|delete)\s", "Windows registry write"),
    (_CMDPOS + r"(bcdedit|takeown|icacls)\s", "Windows system/ACL config"),
    (r"\bcipher\.exe\b|\bcipher\s+/[a-zA-Z]", "Windows disk-encryption utility (cipher.exe)"),
    # Installing tools/libraries/interpreters on the analysis VM is ORDINARY RE work
    # and is allowed (granted 2026-09-22). Only an INTERACTIVE root shell stays
    # blocked: whatever gets typed into it never passes through this filter at all.
    (r"\bsudo\s+(su|-i|-s|bash|sh|zsh|dash|fish)\b", "interactive root shell (sudo su / sudo -i)"),
    (_CMDPOS + r"(iptables|ip6tables|nft|ufw)(?!\s*=)\s", "Linux firewall config"),
    (_CMDPOS + r"systemctl\s+(stop|disable|mask)\s+\S*(firewall|ufw|apparmor)",
     "disabling a host security service via systemctl"),
    (_CMDPOS + r"(useradd|userdel|usermod|chpasswd|passwd)(?!\s*=)\s", "Linux account management"),
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$)", "recursive delete of filesystem root"),
    (_CMDPOS + r"(mkfs(\.\w+)?|diskpart)\b", "disk formatting/partitioning"),
    (r"^\s*format\s+[a-zA-Z]:", "Windows disk format"),
]


def _security_block(text):
    """Return a human reason if `text` (a run_cmd command or a run_script/solver body)
    touches security/system config, else None. Checked BEFORE anything reaches a VM."""
    for pat, why in _BLOCKED_PATTERNS:
        if re.search(pat, text or "", re.IGNORECASE | re.MULTILINE):
            return why
    return None
