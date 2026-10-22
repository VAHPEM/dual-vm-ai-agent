# -*- coding: utf-8 -*-
"""dualvm.output — tach tu ctf-brain tools.py (pre-split), verbatim.
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
from . import remote, config
from ._result import VMResult, SLICE  # noqa


def _clamp_page(length):
    """A paging tool must never hand back more than the Brain will actually be shown,
    or its own "next offset" becomes a lie (measured ch6, 2026-09-23: 12000 requested,
    3500 shown, 8500 silently skipped every page). Returns (length, note)."""
    from . import config
    n = int(length or SLICE)
    if n > config.PAGE_MAX:
        return config.PAGE_MAX, (f"  (asked for {n}, capped to {config.PAGE_MAX} - that "
                                 "is all one turn can show you; continue from the "
                                 "offset below)")
    return n, ""


# how much raw output a single tool call returns inline before it spills to a
# host file that the Brain pages through with read_file
SLICE = 4000


_SPILL_N = [0]


def _spill(host, text, stem="out"):
    """The ONE way a tool hands back output longer than it can show inline.

    Two failures this replaces, both measured on real runs:
      * EVERY large run_cmd/run_script spilled to the SAME `_cmd_out.txt`. The Brain
        noted "full output -> samples/_cmd_out.txt", ran another big command, then
        read that path back and silently got the OTHER command's output. ch6 run 2
        step 29 did exactly this: its own note says it was fetching the sed 869-1140
        output, and it read step 28's instead. No error, no warning, wrong data.
      * win_windows / win_gui_run / pdf_pages just did text[:SLICE] - everything past
        4000 characters was gone with no way to ask for it.

    Returns (inline_text, saved_path_or_None); the name is unique to THIS call.
    """
    # SLICE (4000) is the default PAGE size for peek/read_file; it is NOT the size of
    # what one turn can SHOW. That is config.OBS_CAP_NEWEST (12000). head + marker +
    # tail must stay under it, or render_state trims the result again on its way into
    # the prompt.
    if len(text) <= SLICE:
        return text, None                      # tiny: no host file needed
    from . import config as _cfg
    budget = max(int(getattr(_cfg, "OBS_CAP_NEWEST", 12000)) - 1900, SLICE)
    tail_n = 1200
    _SPILL_N[0] += 1
    name = f"_{stem}_{_SPILL_N[0]:03d}.txt"
    # ALWAYS persist output larger than one PAGE, so it can be paged back on a LATER
    # turn once the observation window has trimmed it. Regression measured 2026-09-23:
    # a version that "saved nothing when it fits the window" showed a 6000-char result
    # in full THIS turn but left no file, so the next turn (window trimmed to 1400) the
    # Brain had to RE-RUN the command to see it again. Callers that keep `saved` put the
    # path in their KNOWN line, so it survives in the ledger.
    saved = _save_to_host(host, name, text)
    if len(text) <= budget + tail_n:
        return text, saved                     # fits one turn: show whole, file kept
    host_arg = "" if host == "kali" else f', host="{host}"'
    if saved:
        where = (f'FULL output is {len(text)} chars; this is the head and tail. '
                 f'Read the rest with read_file(path="{name}"{host_arg})')
    else:
        where = f"{len(text)} chars total; saving the full copy FAILED, so the middle is lost"
    return text[:budget] + f"\n...[{where}]...\n" + text[-tail_n:], saved


def _save_to_host(host, name, data):
    """Persist a full (possibly large) output to samples/<name> on the VM via SFTP
    (write_remote), so the Brain can page it later with read_file. Best-effort.
    NOTE: this used to base64-encode `data` and pass it as a single shell command
    line via ssh_exec() for host="kali" (windows already used write_remote, since
    cmd.exe has no printf/base64). Measured 2026-09-22 (ch5 ntfsm run 7/8): a 403KB
    Ghidra function-list -> 537KB of base64 crammed into one exec_command() call
    overflows the SSH channel itself (paramiko raises EOFError, not a normal shell
    error) - silently swallowed by the except below, so pe_overview reported "full
    list -> inline above" as if nothing were wrong, when the full list was in fact
    NEVER saved anywhere and only the first SLICE chars were ever visible to the
    Brain. write_remote() (SFTP) has no such command-line-size limit and already
    proved reliable for windows - use it unconditionally for both hosts."""
    try:
        remote.write_remote(f"samples/{name}", data, host=host)
        return f"samples/{name}"
    except Exception:  # noqa: BLE001
        return None
