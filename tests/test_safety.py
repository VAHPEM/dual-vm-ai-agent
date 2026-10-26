# -*- coding: utf-8 -*-
"""dualvm.safety: refuse security-/system-config changes, but never block
ordinary reverse-engineering commands that merely mention those words."""
import pytest

from dualvm.safety import _security_block


@pytest.mark.parametrize("command", [
    "Set-MpPreference -DisableRealtimeMonitoring $true",
    "Add-MpPreference -ExclusionPath C:\\samples",
    "Disable-WindowsDefenderRealtime",
    "netsh advfirewall set allprofiles state off",
    "sc.exe stop WinDefend",
    "dir & sc config svc start= disabled",
    "reg add HKLM\\Software\\X /v a /d 1",
    "icacls C:\\Windows /grant x:F",
    "cipher /w:C",
    "sudo su",
    "sudo -i",
    "ls; iptables -F",
    "sudo ufw disable",
    "systemctl stop apparmor",
    "sudo useradd bob",
    "rm -rf /",
    "mkfs.ext4 /dev/sda1",
    "format C:",
    "echo start\nnft flush ruleset",          # command position on a later line
])
def test_blocks_security_config_changes(command):
    assert _security_block(command) is not None


@pytest.mark.parametrize("command", [
    "cat /etc/passwd",
    "grep -r 'useradd' extracted/",
    "objdump -d a.out | grep passwd",
    "echo 'sc create' >> notes.txt",
    'passwd = "hunter2"',                     # python assignment, not a command
    "iptables_rules = [1, 2]",
    "# this solver does not touch iptables",
    "rm -rf /tmp/work",
    "sudo apt install -y radare2",            # installing tools is allowed
    "python3 solve.py",
    "",
    None,
])
def test_allows_ordinary_analysis(command):
    assert _security_block(command) is None


def test_is_case_insensitive():
    assert _security_block("NETSH ADVFIREWALL show") is not None
