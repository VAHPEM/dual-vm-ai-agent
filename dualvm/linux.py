# -*- coding: utf-8 -*-
"""dualvm.linux — tach tu ctf-brain tools.py (pre-split), verbatim.
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
import re, shlex
from . import remote
from ._result import VMResult, _spill, _find_match  # noqa
from .safety import _security_block
from .images import _prepare_image
from .shell import _run_and_wrap


def linux_gui_run(command, wait=5, screen="1280x800x24", timeout=60):
    blocked = _security_block(command)
    if blocked:
        return VMResult(False, f"blocked: this command touches {blocked}")
    inner = (f"{command} & sleep {int(wait)}; "
             "(xdotool search --onlyvisible --name '.' getwindowname %@ 2>/dev/null "
             "| sed 's/^/XWIN /' | head -40); "
             "import -window root _xshot.png 2>&1 && echo XSHOT_OK")
    cmd = ("xvfb-run -a -s " + shlex.quote("-screen 0 " + str(screen)) + " bash -c "
           + shlex.quote(inner))
    res = _run_and_wrap(cmd, "kali", timeout, f"xvfb({command[:40]})")
    if "XSHOT_OK" not in (res.detail or ""):
        return VMResult(False, "linux_gui_run: no screenshot was produced - "
                                 f"output: {(res.detail or '')[:400]}")
    try:
        raw = remote.read_bytes("samples/_xshot.png", host="kali")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"linux_gui_run: could not read _xshot.png back ({e})")
    media, b64, note = _prepare_image(raw, "png")
    if not media:
        return VMResult(False, f"linux_gui_run: screenshot unusable ({note})")
    return VMResult(True, f"ran {command[:40]} under Xvfb{note}; picture on your next turn",
                      # res.detail already came through _spill in _run_and_wrap:
                      # head + pointer + tail. Truncating it AGAIN here could cut the
                      # pointer off the end and strand the full output.
                      detail=(res.detail or ""),
                      known=[f"linux_gui_run({command[:40]}) -> {(res.detail or '')[:300]}"],
                      image={"media_type": media, "data": b64})


# --- Linux: gdb, the scriptable debugger --------------------------------------------
# The Linux twin of win_frida. FLARE-On ch9 (neon_outrun, 2026-09-27) needed it and had
# no tool: a 20MB Tauri/Rust/V8 desktop app whose real logic (a script decrypted on the
# heap, and three V8 built-ins - sort/Math.min/replaceAll - OVERRIDDEN by obfuscated
# native code) is only reachable at RUNTIME. Static r2/Ghidra reading stalled; the flag
# came from breaking in gdb, dumping the decrypted script, and measuring the native
# functions as black boxes. win_frida is Windows-only, linux_gui_run only screenshots.
# This runs a gdb-python script the Brain writes, on Kali, under Xvfb (so a GUI/webkit
# binary can actually start), with ASLR disabled so a PIE always loads at the SAME base.
# MEASURED in-cloud on the real ch9 binary (2026-09-27): with `set disable-randomization
# on`, the PIE base is a STABLE 0x555555554000 every run - so the Brain can hardcode
# base+RVA without leaking it first. Deterministic, $0, raw output, no interpretation.

# The prelude is prepended to every run: it silences the debugger's own noise, keeps gdb
# on the parent across webkit's fork()s, and hands the Brain the small set of helpers
# that actually solved ch9 (read/write inferior memory, read a register, base+RVA).
_GDB_PRELUDE = r'''# ctf-brain gdb prelude (muscle-side; no interpretation - just plumbing + helpers)
import gdb, struct, sys, re as _re

def _flush(s):
    sys.stdout.write(s); sys.stdout.flush()

def log(*a):
    # one raw line to captured stdout; survives even from inside a Breakpoint.stop()
    _flush(" ".join(str(x) for x in a) + "\n")

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set disable-randomization on")   # PIE base is then a stable 0x555555554000
gdb.execute("set breakpoint pending on")      # allow hooks set before the inferior loads
gdb.execute("set follow-fork-mode parent")    # webkit forks helper procs - stay on main
gdb.execute("set detach-on-fork on")
for _s in ("SIGSEGV", "SIGUSR1", "SIGUSR2", "SIGPIPE", "SIGCHLD"):
    try:
        gdb.execute("handle %s nostop noprint pass" % _s)
    except gdb.error:
        pass

_INF = gdb.selected_inferior
def rd(addr, n):
    return bytes(_INF().read_memory(int(addr), int(n)))
def wr(addr, data):
    _INF().write_memory(int(addr), bytes(data))
def u64(addr): return struct.unpack("<Q", rd(addr, 8))[0]
def u32(addr): return struct.unpack("<I", rd(addr, 4))[0]
def f64(addr): return struct.unpack("<d", rd(addr, 8))[0]
def reg(name): return int(gdb.parse_and_eval("$" + str(name)))
def cstr(addr, maxn=256):
    out = bytearray()
    a = int(addr)
    while len(out) < maxn:
        b = rd(a, 1)
        if b == b"\x00": break
        out += b; a += 1
    return out.decode("utf-8", "replace")

_BASE = [None]
def base():
    # load base of the MAIN executable (== 0x555555554000 with ASLR off, but resolved
    # for real from the memory map so it is correct even if that ever changes).
    if _BASE[0] is None:
        if not gdb.selected_inferior().pid:
            raise gdb.error("base()/at() need a running process - call starti() "
                            "(or run()) BEFORE reading the base or setting an RVA hook.")
        prog = gdb.current_progspace().filename or ""
        tail = prog.split("/")[-1]
        for line in gdb.execute("info proc mappings", to_string=True).splitlines():
            p = line.split()
            if len(p) >= 5 and p[-1].split("/")[-1] == tail:
                _BASE[0] = int(p[0], 16); break
    return _BASE[0]
def at(rva):
    # base + file-offset/RVA. The Brain works in offsets from r2/objdump; this maps one
    # to the live address. (0x555555554000 + rva when ASLR is off.)
    return base() + int(rva)

def hook(where, fn, once=False):
    # Break, run fn(bp) when hit, then CONTINUE (fn returns True to STOP instead).
    # `where`: an int is treated as an RVA (base is added); a str is a gdb location
    #   ("*0x555555....", "*main", "func", "file.c:42").
    loc = ("*%d" % at(where)) if isinstance(where, int) else str(where)
    class _H(gdb.Breakpoint):
        def stop(self):
            try:
                stop = bool(fn(self))
            except Exception:
                import traceback; traceback.print_exc(); stop = True
            if once and not stop:
                self.enabled = False
            return stop
    return _H(loc, gdb.BP_BREAKPOINT)

def starti(): gdb.execute("starti")   # stop at the very first instruction
def cont():   gdb.execute("continue")
def run():    gdb.execute("run")      # start and run (breakpoints still fire)
'''


# Tail appended after the Brain's script is exec'd - so its OWN line numbers show up in a
# traceback (as "brain_gdb_script"), not offset by the prelude length.
_GDB_RUN_TAIL = (
    "\ntry:\n"
    "    with open('_gdb_user.py', 'r') as _f:\n"
    "        _src = _f.read()\n"
    "    exec(compile(_src, 'brain_gdb_script', 'exec'), globals())\n"
    "except Exception:\n"
    "    import traceback as _tb; _tb.print_exc()\n"
    "    log('__GDB_USER_EXC__')\n"
)


def linux_gdb(prog, script, args="", gui=True, screen="1280x800x24",
              max_lines=800, timeout=120):
    if not (script or "").strip():
        return VMResult(False, "linux_gdb: `script` is empty - pass a gdb-python "
                                 "snippet, e.g. hook(0x1234, lambda b: log(reg('rdi'))); run()")
    blocked = _security_block(script)
    if blocked:
        return VMResult(False, f"linux_gdb: refused - {blocked}")
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 120
    if isinstance(args, (list, tuple)):
        argstr = " ".join(shlex.quote(str(a)) for a in args)
    else:
        argstr = str(args or "")

    # Write the driver (prelude + exec-tail) and the Brain's script as SEPARATE files, so
    # a Python error in the Brain's code reports ITS line number, not a prelude-shifted one.
    remote.write_remote("samples/_gdb_run.py", _GDB_PRELUDE + _GDB_RUN_TAIL, host="kali")
    remote.write_remote("samples/_gdb_user.py", script, host="kali")

    # gdb --args sets the inferior + argv but does NOT start it; the Brain's run()/starti()
    # does. timeout -k 10 tree-kills gdb (and the inferior in its process group) so a hung
    # target or an infinite continue cannot outlive the call and clog the VM.
    gdb_cmd = (f"timeout -k 10 {timeout} gdb -q -batch -x _gdb_run.py "
               f"--args ./{shlex.quote(prog)}" + (f" {argstr}" if argstr else ""))
    if gui:
        inner = ("cd samples 2>/dev/null; "
                 "xvfb-run -a -s " + shlex.quote("-screen 0 " + str(screen))
                 + " bash -c " + shlex.quote(gdb_cmd) + "; echo __GDB_RC=$?")
    else:
        inner = "cd samples 2>/dev/null; " + gdb_cmd + "; echo __GDB_RC=$?"
    cmd = f"bash -c {shlex.quote(inner)}"

    out, errtxt = remote.ssh_exec(cmd, host="kali", read_timeout=timeout + 30)
    combined = ((out or "") + (("\n" + errtxt) if (errtxt or "").strip() else "")).strip()

    rc = None
    mrc = re.search(r"__GDB_RC=(\d+)\s*$", combined)
    if mrc:
        rc = int(mrc.group(1)); combined = combined[:mrc.start()].rstrip()
    timed_out = rc == 124
    if re.search(r"gdb:\s*(command )?not found|No such file.*gdb", combined) and rc:
        return VMResult(False, "linux_gdb: gdb did not run on Kali - measured output: "
                                 + combined[:400])
    user_exc = "__GDB_USER_EXC__" in combined
    combined = combined.replace("__GDB_USER_EXC__", "").strip()

    lines = combined.splitlines()
    capped = ""
    if len(lines) > int(max_lines):
        capped = f"\n[... {len(lines) - int(max_lines)} more lines - raise max_lines or log() a summary]"
        combined = "\n".join(lines[:int(max_lines)])
    combined = (combined + capped) if combined else "(gdb produced no output)"

    detail, saved = _spill("kali", combined, "gdb_" + re.sub(r"[^A-Za-z0-9]", "_", prog)[:20])
    flag = _find_match(combined)
    n = len(combined)

    note = ""
    if timed_out:
        note = (f" - KILLED after {timeout}s (raise `timeout`, or don't run() an app "
                "that never exits: set your hooks and let them fire, or kill() in a hook)")
    elif user_exc:
        note = " - your script raised a Python exception (traceback above; line = your script)"
    ok = not timed_out
    known = [f"linux_gdb on {prog} -> {n} chars"
             + (f" (full -> {saved}, page with read_file)" if saved else "")
             + (" [TIMEOUT]" if timed_out else "")]
    return VMResult(ok, f"linux_gdb on {prog}: {n} chars{note}" + ("; FLAG" if flag else ""),
                      detail=f"[linux_gdb @ {prog}]\n" + detail,
                      known=known if ok else [], flag=flag, info=detail)
