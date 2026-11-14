# -*- coding: utf-8 -*-
"""labhand.safety: on the free-action lab only three disk-wrecking commands are refused
(recursive delete of /, mkfs / diskpart, `format X:`). Security/system configuration is
allowed, and words that merely mention a blocked command are never blocked."""
import pytest

from labhand.safety import _BLOCKED_PATTERNS, _security_block


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -fr /",
    "rm -Rf /*",
    "rm -r -f /",
    "rm --recursive --force /",
    "sudo rm -rf --no-preserve-root /",
    "cd /tmp; rm -rf / ",
    "echo x && rm -rf /",
    'rm -rf "/"',
    "rm -rf '/*'",
    "mkfs.ext4 /dev/sda1",
    "sudo mkfs -t ext4 /dev/vdb",
    "diskpart /s wipe.txt",
    "echo start\ndiskpart",                   # command position on a later line
    "format C:",
    "  format D: /q",
    "dir & format C: /fs:ntfs",
    "FORMAT c:",                               # case-insensitive
])
def test_blocks_disk_wrecking_commands(command):
    assert _security_block(command) is not None


@pytest.mark.parametrize("command", [
    # formerly blocked security/system configuration: allowed on the lab now
    "Set-MpPreference -DisableRealtimeMonitoring $true",
    "Add-MpPreference -ExclusionPath C:\\samples",
    "Disable-WindowsDefenderRealtime",
    "netsh advfirewall set allprofiles state off",
    "sc.exe stop WinDefend",
    "dir & sc config svc start= disabled",
    "reg add HKLM\\Software\\X /v a /d 1",
    "reg delete HKCU\\Software\\X /f",
    "icacls C:\\Windows /grant x:F",
    "takeown /f C:\\x",
    "bcdedit /set testsigning on",
    "cipher /w:C",
    "sudo su",
    "sudo -i",
    "sudo bash",
    "ls; iptables -F",
    "sudo ufw disable",
    "systemctl stop apparmor",
    "sudo useradd bob",
    "echo start\nnft flush ruleset",
    # ordinary analysis that merely mentions a blocked word
    "cat /etc/passwd",
    "grep -r 'mkfs' extracted/",
    "strings a.bin | grep diskpart",
    "echo 'format C:' >> notes.txt",
    "mkfs = 1",
    "# this solver never runs rm -rf /",
    "rm -rf /tmp/work",
    "rm -rf /home/kali/samples/out",
    "rm -f /",                                 # not recursive: cannot remove a directory
    "rm -rf ./",
    'rm -rf "/tmp/x"',
    "python3 -c \"print('format C:')\"",
    "sudo apt install -y radare2",
    "python3 solve.py",
    "",
    None,
])
def test_allows_everything_else(command):
    assert _security_block(command) is None


def test_exactly_three_rules():
    assert [why for _, why in _BLOCKED_PATTERNS] == [
        "recursive delete of filesystem root", "disk formatting/partitioning", "Windows disk format"]
