# -*- coding: utf-8 -*-
"""dualvm.shell — tach tu ctf-brain tools.py (pre-split), verbatim.
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
import os, re, shlex, base64
from . import remote, config
from ._result import VMResult, SLICE, _spill, _save_to_host, _find_match  # noqa
from .output import _clamp_page
from .safety import _security_block


def _read_file_sftp(path, offset, length, host):
    """read_file for hosts whose default shell is not bash (windows/cmd.exe)."""
    length, cap_note = _clamp_page(length)
    # MEASURED ch8 run 5 (s49/s55): win_frida advertised its log at "samples/_frida_log.txt"
    # and the Brain read exactly that path, but this prepended ANOTHER "samples/" ->
    # "samples/samples/_frida_log.txt", which does not exist on Windows (Kali is rescued
    # by the samples/samples->. symlink; Windows has none). So the 39 KB frida log with
    # the per-keystroke accumulator data was unreachable, costing 2 stalls + an escalation.
    # A path already rooted at samples/ (how every tool advertises its files) is used as-is.
    if re.match(r"^([A-Za-z]:|[\\/])", path) or path.startswith(("samples/", "samples\\")):
        p = path
    else:
        p = f"samples/{path}"
    try:
        data = remote.read_bytes(p, host=host)
    except IOError:
        return VMResult(False, f"read_file: {path} does not exist on {host} "
                                 "(nothing was saved there yet).")
    s = data[offset:offset + length]
    body = s.decode("utf-8", "replace")
    end = offset + len(s)
    flag = _find_match(body)
    if not s and offset >= len(data):
        # parity with the kali path: an empty but "successful" page past EOF looked
        # like a stall the Brain had to diagnose for itself.
        return VMResult(False, f"read_file: offset {offset} is at/after the end of "
                                 f"{path} (total {len(data)} bytes) - you already have "
                                 "all of it.")
    return VMResult(True, f"read {path} bytes {offset}..{end} (total {len(data)}){cap_note}",
                      detail=f"[{path} {offset}..{end}/{len(data)}]\n" + body,
                      known=[f"read_file {path}[{offset}:{end}]"], flag=flag)


def _read_file_one(path, offset=0, length=SLICE, host="kali"):
    offset = int(offset or 0)
    length, cap_note = _clamp_page(length)
    if host == "windows":
        return _read_file_sftp(path, offset, length, host)
    # base64, NOT raw bytes: ssh_exec decodes the channel with errors="replace",
    # so every non-ASCII byte used to collapse into one U+FFFD character and
    # `offset + len(body)` counted CHARACTERS while the caller pages by BYTES -
    # each page of a binary blob silently skipped data. base64 is ASCII-safe and
    # lets us recover the exact byte count.
    py = (
        "import sys,base64;"
        f"d=open({path!r},'rb').read();"
        f"s=d[{offset}:{offset+length}];"
        "sys.stdout.write(repr(len(d))+'|'+base64.b64encode(s).decode())"
    )
    out, err = remote.ssh_exec(
        f"cd samples 2>/dev/null; python3 -c {shlex.quote(py)}", host=host, read_timeout=40,
    )
    if err.strip() and not out.strip():
        e = err.strip()
        if "FileNotFoundError" in e or "No such file" in e:
            return VMResult(False, f"read_file: {path} does not exist on {host} "
                                     "(nothing was saved there yet).")
        return VMResult(False, f"read_file failed: {e.splitlines()[-1][:200]}")
    total, _, b64 = out.partition("|")
    try:
        raw = base64.b64decode(b64.strip(), validate=True)
    except Exception:  # noqa: BLE001
        return VMResult(False, "read_file: remote payload was not decodable "
                                 f"(got {out[:120]!r})")
    body = raw.decode("utf-8", "replace")
    flag = _find_match(body)
    end = offset + len(raw)
    try:
        n_total = int(total.strip())
    except (TypeError, ValueError):
        n_total = None
    if not raw and n_total is not None and offset >= n_total:
        # measured ch2 step 12: the Brain paged to offset == total, got an empty but
        # "successful" read, and spent a stall working out why. Say it plainly.
        return VMResult(False, f"read_file: offset {offset} is at/after the end of "
                                 f"{path} (total {n_total} bytes) - there is nothing "
                                 "more to read in this file; you already have all of it.")
    return VMResult(True, f"read {path} bytes {offset}..{end} "
                            f"(total {total.strip()}){cap_note}",
                      detail=f"[{path} {offset}..{end}/{total.strip()}]\n" + body,
                      known=[f"read_file {path}[{offset}:{end}]"], flag=flag)


def read_file(path, offset=0, length=SLICE, host="kali", pinned_docs=()):
    """Try the host asked for; if the file is not there, try the OTHER one before
    failing. MEASURED ch8 run 1: four steps (16, 19, 27, 43) were spent asking for a
    file on `windows` that a kali tool had written - each one a stall, and two of them
    pushed the run to the expensive top tier. Which VM a tool saved its output on is
    OUR bookkeeping, not a fact the Brain should have to remember."""
    # Same pinned-doc guard as peek: the VM copy (samples/_analysis/FINDINGS.md) is the
    # same text the Brain already has whole in context (ch8 run 15 s56/s57 read it so).
    _p = str(path).replace("\\", "/")
    for _nm in pinned_docs:
        if _p == _nm or _p.endswith("/" + _nm):
            return VMResult(True, f"read_file {path}: that is {_nm}, already in your "
                                    "context in full (PRIOR NOTES block) - not re-read",
                              detail=f"[{_nm} is pinned WHOLE in your system context under "
                                     "'PRIOR NOTES'. Read it there; act on it.]",
                              known=[f"{_nm} is pinned in context (no need to read_file)"])
    res = _read_file_one(path, offset=offset, length=length, host=host)
    if res.ok or "does not exist on" not in (res.summary or ""):
        return res
    other = "kali" if host == "windows" else "windows"
    try:
        alt = _read_file_one(path, offset=offset, length=length, host=other)
    except Exception:  # noqa: BLE001 - the fallback must never turn a miss into a crash
        return res
    if alt.ok:
        alt.summary = (f"read_file: '{path}' is NOT on {host} but IS on {other} - "
                       f"served it from {other}. " + alt.summary)
        alt.known = [k + f"  (file lives on {other}, not {host})" for k in alt.known]
        return alt
    return VMResult(False, f"read_file: {path} exists on NEITHER {host} nor {other} "
                             "(both checked just now). Nothing has written it yet - "
                             "re-read the output of the tool that was supposed to "
                             "create it instead of reading the path again.")


def _wrap_cmd(command, host, timeout=20):
    """Working dir + REAL timeout + exit-code marker.

    kali: the command runs inside `timeout -k 5 N bash -c '<command>'`. Two things
    this fixes, both measured 2026-09-22: (a) the `timeout` arg used to be a pure
    fiction - only paramiko's read_timeout existed, so a killed channel left the
    remote command RUNNING to completion (verified: `sleep 12; touch f` still
    created f after the channel died), which could clobber a later step's files and
    pile up heavy processes (Ghidra/angr) on the VM; (b) `bash -c` pins the shell,
    where before the command ran under the login shell (zsh here, despite the old
    comment claiming bash). timeout runs the child in its own process group and
    signals the whole group, so forked children die too. Exit 124 = timed out.
    windows: NOT built here any more - _run_and_wrap sends Windows commands through
    remote.win_run_bounded, which gives them the same real, tree-killing timeout."""
    if host == "windows":
        return ("cd /d samples 2>nul & " + command +
                " & if errorlevel 1 (echo __RC=1) else (echo __RC=0)")
    # Snapshot samples/ before and after, in the SAME shell, and report what changed.
    # Measured ch6 run 2 (2026-09-23): a hand-written run_script wrote 57262 bytes to
    # samples/dump2.txt and printed only "wrote 57262". The Brain had no way to learn
    # the filename, guessed "disasm.txt", read_file said it did not exist - second
    # stall, run dead. The step that did the real work was also scored a stall,
    # because 11 characters of output is below INFO_MIN_CHARS. One shell snapshot
    # fixes both: the Brain learns where its output went, and producing a file now
    # counts as the progress it plainly is.
    snap = "find . -maxdepth 1 -type f -printf '%f\\t%s\\n' 2>/dev/null | sort"
    inner = (
        "cd samples 2>/dev/null\n"
        "set -o pipefail\n"
        f"{snap} > /tmp/_pre.$$ 2>/dev/null\n"
        + command + "\n"
        "__rc=$?\n"
        f"{snap} > /tmp/_post.$$ 2>/dev/null\n"
        "comm -13 /tmp/_pre.$$ /tmp/_post.$$ 2>/dev/null | head -15 | "
        "sed 's/^/__NEWFILE__ /'\n"
        "rm -f /tmp/_pre.$$ /tmp/_post.$$\n"
        "exit $__rc"
    )
    return f"timeout -k 5 {int(timeout)} bash -c {shlex.quote(inner)}; echo __RC=$?"


_NEWFILE_RE = re.compile(r"^__NEWFILE__ (.+?)\t(\d+)$", re.M)


def _peel_newfiles(text):
    """Split the `__NEWFILE__ name\tsize` lines the wrapper appends off `text`.
    Returns (clean_text, [(name, size), ...])."""
    found = [(m.group(1), int(m.group(2))) for m in _NEWFILE_RE.finditer(text or "")]
    return (_NEWFILE_RE.sub("", text or "").rstrip(), found)


def _peel_rc(text):
    """Split the trailing __RC=<n> marker off `text` -> (clean_text, rc or None)."""
    ms = list(re.finditer(r"__RC=(\d+)", text))
    if not ms:
        return text, None
    m = ms[-1]
    return (text[:m.start()] + text[m.end():]).strip(), int(m.group(1))


# grep's exit-status convention: 0 = matched, 1 = searched and found NOTHING, 2 = error.
# MEASURED ch4 run 2 (2026-09-27): every "no match" came back as a FAILED run_cmd
# ("exit 1, 0 chars"), so (a) three honest negative searches spent the tool-error budget
# and escalated the Brain to Opus at step 6, and (b) with the command cut at 55 chars
# in the ledger (`...grep -i '@flare-on.com` -> exit 1) the Brain concluded its quoting
# was broken and burned steps 17-20 re-running a search that had already answered.
_GREP_CMDS = {"grep", "egrep", "fgrep", "zgrep", "rg"}


_GREP_ARG_OPTS = {"-e", "-f", "-m", "-A", "-B", "-C", "--regexp", "--file",
                  "--max-count", "--context", "--after-context", "--before-context"}


_REDIR_OPS = {">", ">>", "<", ">&", "<&", "&>", "&>>", "<<", "<<<", ">|"}


_LABEL_MAX = 110


def _cmd_label(command, limit=_LABEL_MAX):
    """The command as it appears in the ledger. A cut is marked OUTSIDE the backticks,
    so a truncated command can never look like a command with an unclosed quote."""
    c = " ".join(str(command).split())
    if len(c) <= limit:
        return f"`{c}`"
    return f"`{c[:limit]}`...(+{len(c) - limit} chars of the command not shown)"


def _grep_shape(command):
    """How grep's exit 1 should be read for a one-line KALI command:
      'pipeline' - ONE pipeline (no && || ; &) with a grep-family stage, so with
                   `set -o pipefail` an empty exit-1 result means "no match";
      'no_file'  - that grep is the FIRST stage and has no file operand (and no -r),
                   so it searched an EMPTY stdin - a call error, not a negative result;
      None       - anything else (lists, subshells, unparsable): keep exit 1 a failure."""
    if "\n" in command or "$(" in command or "`" in command:
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    if any(t in ("&&", "||", ";", "&", ";;", "(", ")") for t in toks):
        return None
    stages, cur = [], []
    for t in toks:
        if t in ("|", "|&"):
            stages.append(cur)
            cur = []
        else:
            cur.append(t)
    stages.append(cur)
    for i, st in enumerate(stages):
        words, skip = [], False
        for j, w in enumerate(st):              # drop redirections (2>/dev/null ...)
            if skip:
                skip = False
                continue
            if w in _REDIR_OPS:
                skip = True
                continue
            if w.isdigit() and j + 1 < len(st) and st[j + 1] in _REDIR_OPS:
                continue
            words.append(w)
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)                         # VAR=x grep ...
        if not words or os.path.basename(words[0]) not in _GREP_CMDS:
            continue
        cmd, args = os.path.basename(words[0]), words[1:]
        if i > 0 or cmd == "rg":                 # piped input, or rg (searches cwd)
            return "pipeline"
        pat_from_opt, recursive, operands, k, opts_done = False, False, [], 0, False
        while k < len(args):
            a = args[k]
            k += 1
            if opts_done or a == "-" or not a.startswith("-"):
                operands.append(a)
            elif a == "--":
                opts_done = True
            elif a.startswith("--"):             # long option
                name = a.split("=", 1)[0]
                if name in ("--regexp", "--file"):
                    pat_from_opt = True
                if name in ("--recursive", "--dereference-recursive"):
                    recursive = True
                if name in _GREP_ARG_OPTS and "=" not in a:
                    k += 1                       # its value is the next word
            else:                                # short cluster, e.g. -rni or -e PAT
                for ci, ch in enumerate(a[1:]):
                    if ch in "rR":
                        recursive = True
                    if ch in "efmABC":
                        pat_from_opt = pat_from_opt or ch in "ef"
                        if ci == len(a) - 2:     # value not attached -> next word
                            k += 1
                        break
        files = operands if pat_from_opt else operands[1:]
        return "pipeline" if (files or recursive) else "no_file"
    return None


def _run_and_wrap(command, host, timeout, label, grep_shape=None):
    """Shared tail for run_cmd/run_script: exec + spill-to-host + rc/timeout detection +
    flag scan. `label` is what shows up in the ledger (the command text, or a script
    description) so the two tools can phrase their summary differently. `grep_shape`
    (run_cmd only, see _grep_shape) lets an empty exit-1 grep be read as NO MATCH."""
    if host == "windows":
        # real tree-killing timeout + the true exit code (see remote.win_run_bounded)
        out, err = remote.win_run_bounded(command, timeout)
    else:
        out, err = remote.ssh_exec(
            _wrap_cmd(command, host, timeout), host=host, read_timeout=int(timeout) + 25
        )
    combined = (out + ("\n" + err if err.strip() else "")).strip()
    channel_dead = "(TIMEOUT after" in combined
    combined, rc = _peel_rc(combined)
    combined, newfiles = _peel_newfiles(combined)
    # rc 124 = GNU timeout fired on the VM (the real, reliable signal); the
    # "(TIMEOUT after" marker only means paramiko stopped reading the channel.
    timed_out = channel_dead or rc == 124
    # 141 = SIGPIPE: `... | head` closing the pipe early is the NORMAL way to take the
    # first N lines, not a failure. Measured ch6 step 21: a `find | head` surfaced 3666
    # characters of genuinely new information and was still scored a stall because of
    # this exit code, which pushed the run into an escalation it did not need.
    if rc == 141 and combined.strip():
        rc = 0
    no_match = no_file = False
    if not timed_out and rc == 1 and not combined.strip():
        if grep_shape == "pipeline":
            rc, no_match = 0, True           # searched, found nothing: a measurement
        elif grep_shape == "no_file":
            no_file = True
    n = len(combined)
    sl, saved = _spill(host, combined, "cmd_out")
    if newfiles:
        sl += ("\nFILES WRITTEN OR CHANGED in samples/ by this command: "
               + "; ".join(f"{fn} ({fsz} bytes)" for fn, fsz in newfiles[:8])
               + '. Read one with read_file(path="<name>").')
    flag = _find_match(combined)
    failed = timed_out or rc != 0
    why = (f"TIMEOUT (killed after {int(timeout)}s on the VM - pass a larger "
           f"`timeout` if the work is genuinely long, e.g. an install or Ghidra), "
           if timed_out else
           "exit 1 because grep was given NO FILE to search - it read an empty stdin. "
           "Add the file name (cwd is samples/), e.g. `grep -n PATTERN <file>`; "
           if no_file else
           "shell aborted before finishing (syntax error?), " if rc is None else
           f"exit {rc}, " if failed else "")
    known_fact = f"{label} -> {sl[:180]}"
    if no_match:
        # A negative result is a MEASURED fact ("this pattern is not in that data").
        # No `info`: it adds no new text, so repeating searches that come back empty
        # still reaches the stall threshold (design rule: two empty static looks ->
        # change the KIND of approach) instead of the tool-error budget.
        known_fact = (f"{label} -> NO MATCH (grep exit 1 = the search ran and found "
                      "nothing; this is a result, not an error)")
    if newfiles:
        known_fact += " | wrote " + ", ".join(f"samples/{fn} ({fsz}B)"
                                              for fn, fsz in newfiles[:8])
    if saved:
        known_fact += f" (full -> {saved}, page with read_file)"
    if no_match:
        return VMResult(True, f"ran on {host}: {label} -> NO MATCH (grep exit 1: "
                                "searched, found nothing)", detail="", known=[known_fact],
                          info="")
    return VMResult(
        not failed,
        f"ran on {host}: {label} -> {why}{n} chars" + ("; FLAG" if flag else ""),
        detail=sl,
        known=[] if failed else [known_fact],
        flag=flag,
        info=sl,
    )


def run_cmd(command, host="kali", timeout=20):
    blocked = _security_block(command)
    if blocked:
        return VMResult(False, f"blocked: this command touches {blocked}, which is "
                                 "not permitted (see tool description) - it is never "
                                 "needed to solve a challenge; find another way.")
    return _run_and_wrap(command, host, timeout, _cmd_label(command),
                         grep_shape=_grep_shape(command) if host != "windows" else None)


def run_script(script, host="kali", kind=None, timeout=20, python=None):
    blocked = _security_block(script)
    if blocked:
        return VMResult(False, f"blocked: this script touches {blocked}, which is "
                                 "not permitted (see tool description) - it is never "
                                 "needed to solve a challenge; find another way.")
    kind = kind or ("powershell" if host == "windows" else "bash")
    exts = {"bash": "sh", "python3": "py", "powershell": "ps1", "cmd": "bat"}
    if kind not in exts:
        return VMResult(False, f"run_script: unknown kind '{kind}' (use one of {list(exts)})")
    name = f"_script.{exts[kind]}"
    remote.write_remote(f"samples/{name}", script, host=host)
    # The python3 interpreter is PER HOST. Until 2026-09-23 this always used
    # remote.KALI_PYTHON (/home/vahpem/ctf-venv/bin/python3), bash-quoted, so
    # run_script(kind="python3", host="windows") asked cmd.exe to run a Linux path in
    # single quotes - it could never work.
    if host == "windows":
        py_invoke = f'"{python or remote.WIN_PYTHON_EXE}" {name}'
    else:
        py_invoke = f"{shlex.quote(python or remote.KALI_PYTHON)} {name}"
    invoke = {
        "bash": f"bash {name}",
        "python3": py_invoke,
        "powershell": f"powershell -NoProfile -ExecutionPolicy Bypass -File {name}",
        "cmd": name,
    }[kind]
    lines = script.count("\n") + 1
    return _run_and_wrap(invoke, host, timeout, f"script({kind}, {lines} lines)")
