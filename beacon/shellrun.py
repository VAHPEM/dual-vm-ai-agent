# -*- coding: utf-8 -*-
"""beacon.shellrun — run a shell command the way Claude Code runs a hook command.

POSIX: /bin/sh -c. Windows: Claude Code runs command hooks through Git Bash, so a command a human
writes for beacon (a checklist's verify_cmd, notify_cmd) and the hook commands beacon installs are
POSIX shell there too. Git Bash is found the way Claude Code finds it: CLAUDE_CODE_GIT_BASH_PATH, the
two Program Files locations, then <git.exe>\\..\\..\\bin\\bash.exe -- never a bare "bash", which
CreateProcess looks up in System32 first and so finds WSL's bash.exe stub. Without Git Bash, cmd.exe.
Output is decoded as UTF-8 (errors replaced), never with the Windows ANSI code page.
"""
import os
import shutil
import subprocess


def git_bash():
    """Git Bash's bash.exe on Windows, else None (and always None elsewhere)."""
    if os.name != "nt":
        return None
    cands = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "").strip(),
             r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    git = shutil.which("git")
    if git:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def run(cmd, cwd=None, env=None, timeout=None, input=None):
    """-> subprocess.CompletedProcess with str stdout/stderr. Raises what subprocess.run raises."""
    kw = dict(cwd=cwd, env=env, timeout=timeout, input=input, capture_output=True, text=True,
              encoding="utf-8", errors="replace")
    bash = git_bash()
    if bash:
        return subprocess.run([bash, "-c", cmd], **kw)
    return subprocess.run(cmd, shell=True, **kw)


def posix_path(path):
    """A path spelled for a command that Git Bash will parse: on Windows, C:\\x\\y becomes C:/x/y (bash takes
    backslashes as escapes, so a hook command with them silently never runs). Unchanged elsewhere."""
    return str(path).replace("\\", "/") if os.name == "nt" else str(path)


__all__ = ["git_bash", "run", "posix_path"]
