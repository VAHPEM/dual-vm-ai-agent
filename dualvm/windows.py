# -*- coding: utf-8 -*-
"""dualvm.windows — split from the original monorepo (verbatim).
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
import os, re, json, shlex
from . import remote
from ._result import VMResult, _spill, _find_match  # noqa
from .safety import _security_block
from .images import _prepare_image


# --- Windows GUI: the interactive desktop (session 1) ------------------------------
# See remote.run_in_session1 for WHY none of this can go through a plain SSH command.

_PS_LIST_WINDOWS = r"""
Get-Process | Where-Object { $_.MainWindowTitle } | Sort-Object ProcessName | ForEach-Object {
  Write-Output ("PROC pid=" + $_.Id + " " + $_.ProcessName + " | " + $_.MainWindowTitle)
}
try {
  Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes -ErrorAction Stop
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $kids = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                        [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($k in $kids) {
    $n = $k.Current.Name; $cls = $k.Current.ClassName
    $ct = $k.Current.ControlType.ProgrammaticName -replace "ControlType\.",""
    if ($n -or $cls) { Write-Output ("WIN [" + $ct + "] class=" + $cls + " name=" + $n) }
  }
} catch { Write-Output ("UIA_UNAVAILABLE=" + $_.Exception.Message) }
"""


_PS_DEEP_TEXT = r"""
try {
  Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes -ErrorAction Stop
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $kids = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                        [System.Windows.Automation.Condition]::TrueCondition)
  $n = 0
  foreach ($k in $kids) {
    if ($k.Current.Name -like "*__MATCH__*" -or $k.Current.ClassName -like "*__MATCH__*") {
      Write-Output ("=== " + $k.Current.Name + " (" + $k.Current.ClassName + ") ===")
      $all = $k.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                        [System.Windows.Automation.Condition]::TrueCondition)
      foreach ($d in $all) {
        $t = $d.Current.ControlType.ProgrammaticName -replace "ControlType\.",""
        $nm = $d.Current.Name
        if ($nm) { Write-Output ("  [" + $t + "] " + $nm); $n++ }
        if ($n -gt 250) { Write-Output "  ...(cut at 250 elements)"; break }
      }
    }
  }
  if ($n -eq 0) { Write-Output "no window matched, or it exposes no accessible text" }
} catch { Write-Output ("UIA_UNAVAILABLE=" + $_.Exception.Message) }
"""


_PS_SCREENSHOT = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
$bmp.Save("$S\_screen.png", [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ("SCREEN " + $b.Width + "x" + $b.Height + " -> _screen.png")
"""


def _win_shot_image(label):
    """Pull samples/_screen.png off the Windows VM and turn it into an image block."""
    try:
        raw = remote.read_bytes("samples/_screen.png", host="windows")
    except Exception as e:  # noqa: BLE001
        return None, f"(screenshot file could not be read back: {e})"
    media, b64, note = _prepare_image(raw, "png")
    if not media:
        return None, f"(screenshot unusable: {note})"
    return {"media_type": media, "data": b64}, f"screenshot of {label}{note}"


def win_windows(match=None, timeout=40):
    body = _PS_LIST_WINDOWS
    if match:
        body = body + "\n" + _PS_DEEP_TEXT.replace("__MATCH__", str(match).replace('"', ""))
    try:
        text, timed_out = remote.run_in_session1(body, timeout=int(timeout))
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_windows failed: {e.__class__.__name__}: {e}")
    if timed_out:
        return VMResult(False, "win_windows: the interactive task did not finish in "
                                 f"{int(timeout)}s (is anyone logged on at the console?)")
    if not text.strip():
        return VMResult(True, "no windows with a title are open on the Windows desktop",
                          detail="(empty desktop)", known=["Windows desktop: no titled windows open"])
    n = len(text.splitlines())
    detail, _ = _spill("windows", text, "windows")
    return VMResult(True, f"Windows desktop: {n} window line(s)", detail=detail,
                      known=[f"Windows desktop windows: {text[:400]}"], info=detail,
                      flag=_find_match(text))


def win_screenshot(timeout=40):
    try:
        text, timed_out = remote.run_in_session1(_PS_SCREENSHOT, timeout=int(timeout))
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_screenshot failed: {e.__class__.__name__}: {e}")
    if timed_out or "SCREEN " not in text:
        return VMResult(False, f"win_screenshot: capture did not report success - {text[:300]}")
    img, note = _win_shot_image("the Windows desktop")
    if not img:
        return VMResult(False, f"win_screenshot: {note}")
    return VMResult(True, note + " - it appears on your NEXT turn", detail=text[:400],
                      known=[note], image=img)


def _win_exists(win_path):
    try:
        o, _ = remote.ssh_exec(f'if exist "{win_path}" (echo __YES__) else (echo __NO__)',
                               host="windows", read_timeout=20)
    except Exception:  # noqa: BLE001 - VM down: let the caller's real call surface it
        return None
    return "__YES__" in (o or "")


def _sync_to_windows(win_path):
    """A file the Brain wants to run on Windows is often only on KALI: it was produced
    there (7z x of a WIM into a subdir, a header-fixed .exe written with run_script).
    Spawning it then fails with ExecutableNotFoundError and the Brain wastes stalls on
    a path/slash theory (MEASURED ch2 GhostStream: 5 frida calls, then STOP). If the
    file exists on Kali under samples/, copy the whole containing directory across -
    INCLUDING NTFS alternate data streams, which 7z leaves on Kali as separate
    'name:stream' files and which we recreate as real ADS on Windows (verified: an
    SFTP write to 'dir/base:stream' lands as base:stream:$DATA on NTFS). Returns
    (ok, note): ok=False means neither VM has it (note explains); ok=True with a
    non-empty note means we copied and the Brain should know what/where.
    """
    try:
        win_samples = remote.win_samples()
    except Exception as e:  # noqa: BLE001
        return True, ""      # can't reach Windows to check - don't block; real call errors
    prefix = win_samples + "\\"
    if not win_path.startswith(prefix):
        # Not under samples/ - we have no Kali mapping. Only report existence.
        ex = _win_exists(win_path)
        if ex is False:
            return False, (f"win_frida/win_gui_run: '{win_path}' is not on the Windows "
                           "VM and is outside samples/, so it can't be auto-copied from "
                           "Kali. Put it under samples/ (triage/extract do this) first.")
        return True, ""
    rel = win_path[len(prefix):].replace("\\", "/")
    if _win_exists(win_path):
        return True, ""      # already there, nothing to do
    # Missing on Windows. Is it on Kali?
    kali_rel = "samples/" + rel
    try:
        o, _ = remote.ssh_exec(f'test -f "{kali_rel}" && echo __Y__ || echo __N__',
                               host="kali", read_timeout=20)
    except Exception as e:  # noqa: BLE001
        return False, (f"win_frida/win_gui_run: '{rel}' is not on the Windows VM and "
                       f"Kali could not be checked ({e.__class__.__name__}).")
    if "__Y__" not in o:
        return False, (f"win_frida/win_gui_run: '{rel}' is on NEITHER VM. It was never "
                       "uploaded/extracted there. If it lives inside an archive, "
                       "extract(...) it (WIM/zip/7z) first - that registers each file "
                       "and, for WIM, unpacks on Windows with ADS preserved.")
    # Copy the whole containing dir (root file -> just the file + its ADS companions).
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    base = rel.rsplit("/", 1)[-1]
    if parent:
        listing = f'find "samples/{parent}" -maxdepth 1 -type f'
    else:
        # samples root: only the file itself and any 'base:stream' companions of it,
        # NOT the entire samples/ tree.
        listing = (f'find samples -maxdepth 1 -type f \\( -name "{base}" -o '
                   f'-name "{base}:*" \\)')
    try:
        lo, _ = remote.ssh_exec(listing, host="kali", read_timeout=25)
    except Exception as e:  # noqa: BLE001
        return False, f"win sync: could not list Kali source dir ({e.__class__.__name__})"
    kfiles = [l.strip() for l in (lo or "").splitlines() if l.strip().startswith("samples/")]
    if not kfiles:
        kfiles = [kali_rel]
    if len(kfiles) > 300:
        return False, (f"win sync: {len(kfiles)} files under samples/{parent} - too many "
                       "to auto-copy; copy the specific file on Windows yourself.")
    # Make the Windows parent dir once.
    win_parent = win_samples + (("\\" + parent.replace("/", "\\")) if parent else "")
    try:
        remote.ssh_exec(f'if not exist "{win_parent}" mkdir "{win_parent}"',
                        host="windows", read_timeout=20)
    except Exception:  # noqa: BLE001
        pass
    copied, ads, skipped = 0, 0, 0
    for kf in kfiles:
        krel = kf[len("samples/"):]                 # e.g. GhostStream_ex/.../x.exe or ...txt:LordVoldemort
        try:
            data = remote.read_bytes(kf, host="kali")
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if len(data) > 100 * (1 << 20):
            skipped += 1
            continue
        # write_remote takes a remote path relative to the Windows home; keep the same
        # samples-relative layout. A ':' in krel makes it an ADS on NTFS (that is the point).
        try:
            remote.write_remote("samples/" + krel, data, host="windows")
            if ":" in krel.rsplit("/", 1)[-1]:
                ads += 1
            else:
                copied += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    if copied == 0 and ads == 0:
        return False, (f"win sync: found '{rel}' on Kali but every copy to Windows "
                       f"failed ({skipped} file(s)).")
    note = (f"[sync] '{rel}' was only on Kali; copied {copied} file(s)"
            + (f" + {ads} ADS stream(s)" if ads else "")
            + (f" ({skipped} skipped)" if skipped else "")
            + f" to samples/ on Windows so it can run.")
    return True, note


def win_gui_run(program, args="", wait=6, kill=True, screenshot=True, timeout=60):
    prog = str(program).replace('"', "")
    if not re.match(r"^([A-Za-z]:|[\\/])", prog):
        prog = "$S\\" + prog
    gui_sync_note = ""
    try:
        _absprog = (remote.win_samples() + prog[2:]) if prog.startswith("$S\\") else prog
        _ok, gui_sync_note = _sync_to_windows(_absprog)
        if not _ok:
            return VMResult(False, gui_sync_note)
    except Exception:  # noqa: BLE001 - never let the sync check itself break the tool
        gui_sync_note = ""
    argl = str(args or "").replace('"', "'")
    body = (
        'if (-not (Test-Path "' + prog + '")) { Write-Output "NOT_FOUND=' + prog + '"; return }\n'
        '$sp = @{ FilePath = "' + prog + '"; PassThru = $true; WorkingDirectory = $S }\n'
        + ('$sp["ArgumentList"] = "' + argl + '"\n' if argl else '')
        + '$p = Start-Process @sp\n'
        'Start-Sleep -Seconds ' + str(int(wait)) + '\n'
        '$p.Refresh()\n'
        'Write-Output ("PID=" + $p.Id + " EXITED=" + $p.HasExited)\n'
        'if ($p.HasExited) { Write-Output ("EXITCODE=" + $p.ExitCode) } '
        'else { Write-Output ("MAINWINDOW=" + $p.MainWindowTitle) }\n'
        + _PS_LIST_WINDOWS
        + (_PS_SCREENSHOT if screenshot else "")
        + ('\nif (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; '
           'Write-Output "KILLED" }\n' if kill else '\n')
    )
    try:
        text, timed_out = remote.run_in_session1(body, timeout=int(timeout))
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_gui_run failed: {e.__class__.__name__}: {e}")
    if "NOT_FOUND=" in text:
        return VMResult(False, f"win_gui_run: {prog} is not on the Windows VM - "
                                 "triage uploads artifacts there, or copy it first")
    if timed_out:
        return VMResult(False, f"win_gui_run: no result within {int(timeout)}s. The "
                                 "program may be waiting on something; raise `timeout`, "
                                 "or use kill=false and look with win_windows.")
    if gui_sync_note:
        text = gui_sync_note + "\n" + text
    img = None
    note = ""
    if screenshot and "SCREEN " in text:
        img, note = _win_shot_image(f"the desktop after running {program}")
    summ = f"ran {program} on the Windows desktop" + ("; picture on your next turn" if img else "")
    detail, _ = _spill("windows", text, "gui_run")
    return VMResult(True, summ, detail=detail,
                      known=[f"win_gui_run({program}) -> {text[:400]}"], info=detail,
                      flag=_find_match(text), image=img)


# --- Windows: Frida, the scriptable debugger ----------------------------------------
# Measured 2026-09-23 on the Windows VM (Win 11 ARM64, build 26200):
#   * frida 17.18.0 installs from pip and RUNS.
#   * It spawns and instruments an x86-64 PE running under the ARM64 x64 emulator:
#       Process.arch == 'x64', pointerSize 8, 30 modules enumerated (the emulator's
#       own xtajit64se.dll shows up among them), module memory is readable, and
#       Interceptor.attach at an ABSOLUTE address (base+0x1000) installs with no error.
#   * So the VM needs no x64dbg, no cdb, no Windows SDK: breakpoints, argument/return
#     inspection, memory dumps and patching are all reachable from a JS string the
#     Brain writes. This is the muscle that replaces "step it in a debugger".
# The driver below is deliberately dumb: it spawns/attaches, loads the Brain's script,
# prints every send() payload as one raw line, and stops. No interpretation.

_FRIDA_DRIVER = r'''# -*- coding: utf-8 -*-
import json, sys, time, os
_HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(_HERE, "_frida_cfg.json"), "r", encoding="utf-8"))
MAXL = int(CFG.get("max_lines", 800))
try:
    import frida
except Exception as e:
    print("FRIDA_MISSING %s: %s" % (type(e).__name__, e))
    print("(install it on the Windows VM with:  python -m pip install frida frida-tools)")
    print("__FRIDA_END__"); sys.exit(0)
print("FRIDA_VERSION=%s" % frida.__version__)
js = open(CFG["js_path"], "r", encoding="utf-8").read()
lines = []
def _empty_obj(v):
    if isinstance(v, dict):
        return (not v) or any(_empty_obj(x) for x in v.values())
    if isinstance(v, list):
        return any(_empty_obj(x) for x in v)
    return False
def on_message(message, data):
    t = message.get("type")
    if t == "send":
        pl = message.get("payload")
        try:
            s = json.dumps(pl, ensure_ascii=False, default=str)
        except Exception:
            s = repr(pl)
        if data:
            s += "  +DATA[%d]=%s" % (len(data), data[:96].hex())
        lines.append("SEND " + s)
        if _empty_obj(pl) and not data:
            lines.append("  !! a value in that payload arrived EMPTY ({}). An ArrayBuffer"
                         " (readByteArray) does NOT survive inside a payload - it is"
                         " silently dropped. Use hex(ptr, n) instead, or"
                         " send({...}, buf) with the buffer as the SECOND argument.")
    elif t == "error":
        lines.append("JS_ERROR line %s: %s" % (message.get("lineNumber"), message.get("description")))
        st = message.get("stack")
        if st:
            lines.append("  stack: " + str(st)[:400])
    else:
        lines.append(str(t).upper() + " " + json.dumps(message, ensure_ascii=False, default=str)[:600])
pid = None; spawned = False
try:
    if CFG.get("pid"):
        pid = int(CFG["pid"]); print("MODE=attach pid=%d" % pid)
    else:
        argv = [CFG["program"]] + [str(a) for a in (CFG.get("args") or [])]
        pid = frida.spawn(argv); spawned = True
        print("MODE=spawn pid=%d program=%s" % (pid, CFG["program"]))
    session = frida.attach(pid)
    script = session.create_script(js)
    script.on("message", on_message)
    script.load()
    print("SCRIPT_LOADED=yes")
    if spawned:
        frida.resume(pid); print("RESUMED=yes")
    _dps = CFG.get("drive_ps1")
    if _dps:
        time.sleep(float(CFG.get("drive_delay", 1.2)))  # let the GUI paint before we click
        try:
            import subprocess
            _dp = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _dps],
                capture_output=True, text=True, timeout=int(CFG.get("drive_timeout", 90)))
            print("DRIVE_BEGIN")
            _so = (_dp.stdout or "").strip()
            if _so:
                print(_so)
            _se = (_dp.stderr or "").strip()
            if _se:
                print("DRIVE_STDERR " + _se[:800])
            print("DRIVE_END")
        except Exception as _de:
            print("DRIVE_ERROR %s: %s" % (type(_de).__name__, _de))
    t0 = time.time(); wait = float(CFG.get("wait", 8))
    while time.time() - t0 < wait:
        time.sleep(0.2)
    time.sleep(0.4)
except Exception as e:
    print("FRIDA_ERROR %s: %s" % (type(e).__name__, e))
for l in lines[:MAXL]:
    print(l)
if len(lines) > MAXL:
    print("...(%d further send() lines DROPPED - aggregate in the script and send once,"
          " or raise max_lines)" % (len(lines) - MAXL))
print("SEND_COUNT=%d" % len(lines))
if pid is not None:
    if CFG.get("kill", True):
        try:
            frida.kill(pid); print("KILLED=%d" % pid)
        except Exception as e:
            print("KILL_FAILED %s" % e)
    else:
        print("LEFT_RUNNING=%d  (a later win_frida can attach with pid=%d)" % (pid, pid))
print("__FRIDA_END__")
'''


# One line, so a JS_ERROR line number is exactly 1 more than the Brain's own line;
# _fix_js_lines() puts it back before the Brain ever sees it. Gives every script
# hex(p,n) - because readByteArray inside a send() payload is silently dropped -
# plus at(mod,off) for "the address Ghidra showed me".
_FRIDA_PRELUDE_TMPL = (
    "function hex(p,n){var u=new Uint8Array(ptr(p).readByteArray(n));var o='';"
    "for(var i=0;i<u.length;i++){o+=('0'+u[i].toString(16)).slice(-2);}return o;} "
    "function at(m,o){return Process.getModuleByName(m).base.add(o);} "
    "function mod(m){return Process.getModuleByName(m);} "
    # exp(dll, ...parts): resolve an export by SUBSTRING match on its (mangled) name -
    # so you hook `exp('Qt6Widgets.dll','QLineEdit','setText')` instead of guessing the
    # exact C++ decoration (which was the repeated JS_ERROR on ch8). Returns the address
    # or null.
    "function exp(m){var a=[].slice.call(arguments,1);var es=Process.getModuleByName(m).enumerateExports();"
    "for(var i=0;i<es.length;i++){var n=es[i].name.toLowerCase();var ok=true;"
    "for(var j=0;j<a.length;j++){if(n.indexOf((''+a[j]).toLowerCase())<0){ok=false;break;}}"
    "if(ok)return es[i].address;}return null;} "
    # key(vk) / click(x,y): DRIVE the GUI from inside THIS call so a hooked handler
    # actually fires (hooks die when the call returns). Needs desktop=true so the window
    # is real. VK digits '0'-'9' = 0x30-0x39; get x,y from a win_ui_tree rect (centre of
    # the button). One press/click with your hook installed catches the handler.
    "var _NF={};function _nf(n,r,a){if(!_NF[n]){_NF[n]=new NativeFunction(Module.getGlobalExportByName(n),r,a);}return _NF[n];} "
    "function key(vk){var f=_nf('keybd_event','void',['uint8','uint8','uint32','pointer']);f(vk,0,0,ptr(0));Thread.sleep(0.03);f(vk,0,2,ptr(0));} "
    "function click(x,y){_nf('SetCursorPos','int',['int','int'])(x,y);var m=_nf('mouse_event','void',['uint32','uint32','uint32','uint32','pointer']);m(2,0,0,0,ptr(0));Thread.sleep(0.03);m(4,0,0,0,ptr(0));} "
    "var LOG_PATH='__LOGPATH__';var _LF=null;"
    "function log(s){try{if(!_LF){_LF=new File(LOG_PATH,'a');}_LF.write(''+s+'\\n');"
    "_LF.flush();}catch(e){send({log_failed:''+e,path:LOG_PATH});}}\n"
)


# MEASURED ch8 run 1, steps 27-38: the Brain tried to have its hook write
# `samples/hook_log.txt` - a RELATIVE path, which frida resolves against the TARGET
# process's working directory, not samples/ - and got "No such file or directory".
# Four steps went into rediscovering that only an absolute forward-slash path works.
# log() now has that path baked in and the tool reads the file back by itself.
_FRIDA_PRELUDE = _FRIDA_PRELUDE_TMPL           # kept for any existing reference


# _fix_js_lines must shift by however many lines the prelude really adds - hardcoding
# "1" would silently mis-number every JS error the day someone adds a second line.
_PRELUDE_LINES = _FRIDA_PRELUDE_TMPL.count("\n")


def _fix_js_lines(text):
    """Report the Brain's OWN line numbers: subtract the lines the prelude added."""
    def shift(m):
        try:
            return m.group(1) + str(int(m.group(2)) - _PRELUDE_LINES) + m.group(3)
        except Exception:  # noqa: BLE001
            return m.group(0)
    text = re.sub(r"(JS_ERROR line )(\d+)(:)", shift, text or "")
    # the raw stack frames carry the same off-by-one; leaving them un-shifted makes
    # the Brain doubt the corrected number right above them
    return re.sub(r"(/script\d*\.js:)(\d+)(\b)", shift, text)


def _frida_timeout_msg(text, timeout, desktop, wait, driven):
    """The message shown when the Frida driver did not finish in time. It used to
    ALWAYS end with 'a GUI program needs desktop=true', which sent the Brain chasing a
    GUI theory even when the driver plainly reported it had spawned, loaded the script
    and resumed the target (GhostStream ch2: a console PE that simply runs past `wait`).
    Read the stage markers the driver already printed and name the REAL possibilities
    instead of guessing one."""
    t = text or ""
    loaded = "SCRIPT_LOADED=yes" in t
    running = ("RESUMED=yes" in t) or ("MODE=attach" in t)
    if loaded and running:
        # Hooks are in and the target is live: the timeout is the WAIT loop, i.e. the
        # program itself did not return / reach the hook inside the window. This is the
        # common, misdiagnosed case.
        msg = (f"win_frida: hooks loaded and the target was running, but the call did "
               f"not finish within {timeout}s (wait={wait}s). This usually means the "
               "PROGRAM is still busy, NOT that plumbing failed. Likely one of: (a) it "
               "is waiting for input (stdin / a prompt / a socket / a keypress) - feed "
               "it, or drive its GUI with drive=/win_ui_*; (b) it Sleep()s or loops for "
               "a long time before the hooked code runs - RAISE `timeout` (and `wait`) "
               "to cover it; (c) it does heavy work - same, give it longer; (d) it is "
               "blocked on a message box / modal dialog you must dismiss.")
        if not desktop and not driven:
            msg += (" If it is a GUI app whose window never appeared on the service "
                    "desktop, add desktop=true so the window really runs.")
        return msg
    if loaded and not running:
        return (f"win_frida: the script loaded but the target was never resumed within "
                f"{timeout}s - the spawn/attach step is stuck. Check the program path "
                "and that the process is startable; for a GUI/launcher app prefer "
                "launch=<run.bat> or win_gui_run(kill=false)+pid=.")
    # never got to SCRIPT_LOADED: attach/spawn or JS compile stage
    hint = ""
    if not desktop:
        hint = (" If the target is a GUI or launcher app it may be crashing on the "
                "invisible service desktop; try desktop=true, or launch=<run.bat>.")
    return (f"win_frida: the driver never reached SCRIPT_LOADED within {timeout}s - it "
            "did not finish spawning/attaching or compiling the JS (this is a startup "
            "problem, not a result)." + hint)


def win_frida(script, program=None, args="", pid=None, wait=8, kill=True,
              desktop=False, max_lines=800, timeout=90,
              drive=None, drive_window=None, drive_pause=250, drive_after=2,
              launch=None, launch_wait=5, bat_text="", bat_names=()):
    # `wait` is time spent INSIDE the driver letting hooks run; `timeout` bounds the whole
    # driver from outside. MEASURED ch8 run 5 s58: the Brain asked for wait=200 (to press
    # 25x10 keys and measure) but left timeout=90, so the driver was killed at 90 s every
    # time and the measurement never completed. wait can never be honoured beyond timeout,
    # so lift timeout to cover it instead of failing.
    try:
        wait = float(wait); timeout = int(timeout)
    except (TypeError, ValueError):
        wait, timeout = 8.0, 90
    if wait + 40 > timeout:
        timeout = int(wait) + 40
    if not (script or "").strip():
        return VMResult(False, "win_frida: `script` is empty - give me the JavaScript "
                                 "to run inside the process")
    drive_names = []
    if drive:
        _raw = str(drive)
        drive_names = [x.strip() for x in _raw.split(",")] if "," in _raw else list(_raw)
        drive_names = [n for n in drive_names if n]
        _bad = [n for n in drive_names if '"' in n or "`" in n or "'" in n or "\n" in n]
        if _bad:
            return VMResult(False, f"win_frida: drive button {_bad[0]!r} has a quote/"
                                     "backtick and cannot be passed through")
        if len(drive_names) > 200:
            return VMResult(False, f"win_frida: {len(drive_names)} drive presses is over "
                                     "the 200 cap - split it")
        if not desktop:
            desktop = True  # UIA Invoke needs the real session-1 desktop the app lives on
    launch_bat_win = None
    target_exe = None
    if launch:
        desktop = True  # the launched GUI app lives on the session-1 desktop
        _lb = str(launch).replace('"', "").replace("'", "")
        launch_bat_win = remote.win_samples() + "\\" + _lb.replace("/", "\\")
        _bat_txt = bat_text
        _m = re.search(r"([A-Za-z0-9_.\- ]+\.exe)", _bat_txt)
        target_exe = _m.group(1).strip() if _m else None
        if not target_exe:
            return VMResult(False, f"win_frida(launch): could not find a .exe to "
                                     f"attach to inside '{_lb}'. Launch it with "
                                     "win_gui_run and pass pid= instead.")
        # Launch in a SEPARATE session-1 task (Start-Process returns; the GUI persists),
        # then resolve the real target pid. Doing this inside the frida driver made the
        # GUI a child of the driver task, so run_in_session1 waited for the whole tree
        # and timed out (ch8 run 10). This mirrors the proven win_gui_run + pid= path.
        _stem = target_exe[:-4] if target_exe.lower().endswith(".exe") else target_exe
        _ps = (
            f'taskkill /F /T /IM "{target_exe}" 2>$null | Out-Null\n'
            f'Start-Process -FilePath "{launch_bat_win}" -WorkingDirectory "{remote.win_samples()}"\n'
            f'Start-Sleep -Seconds {int(launch_wait)}\n'
            f'$p = Get-Process -Name "{_stem}" -ErrorAction SilentlyContinue | '
            'Sort-Object StartTime | Select-Object -Last 1\n'
            'if ($p) { Write-Output ("LAUNCH_PID=" + $p.Id) } '
            'else { Write-Output "LAUNCH_PID=none" }\n'
        )
        try:
            _lout, _lt = remote.run_in_session1(_ps, timeout=int(launch_wait) + 40,
                                                task="dualvm_frida_launch")
        except Exception as _e:  # noqa: BLE001
            return VMResult(False, f"win_frida(launch): launch step failed: "
                                     f"{_e.__class__.__name__}: {_e}")
        _lm = re.search(r"LAUNCH_PID=(\d+)", _lout)
        if not _lm:
            return VMResult(False, f"win_frida(launch): '{target_exe}' was not "
                                     f"running after launching '{_lb}'. Raise "
                                     "launch_wait, or use win_gui_run + pid=. Tail: "
                                     f"{_lout[-200:]}")
        pid = int(_lm.group(1))
    if not program and not pid and not launch:
        return VMResult(False, "win_frida: give `program` (to spawn), `pid` (to "
                                 "attach), or `launch` (a .bat that starts a GUI app "
                                 "- one call launches it, finds the real process and "
                                 "hooks it)")
    # A launcher-based GUI app (a run.bat sets env like QT_QPA_PLATFORM_PLUGIN_PATH
    # before starting the .exe) CRASHES when spawned directly - measured ch8 run 8 s39:
    # spawning FlareAuthenticator.exe gave "Application Error" and the driver hung the
    # full timeout. If we are about to SPAWN a GUI target (desktop/drive) and a .bat
    # launcher is among the inputs, redirect to the proven two-step instead of burning
    # `timeout` seconds on a crash.
    if program and not pid and not launch and (desktop or drive_names):
        _bat = next((n for n in bat_names
                     if n.lower().endswith(".bat")), None)
        if _bat:
            return VMResult(False,
                f"win_frida: '{program}' looks like a launcher-based GUI app ('{_bat}' "
                "is present and usually sets env the .exe needs). Spawning the .exe "
                "directly tends to crash with 'Application Error' and hang this call. Do "
                f"it in two steps: win_gui_run(program='{_bat}', kill=false) to launch "
                "it and note the PID it prints, then win_frida(pid=<that pid>, drive=..., "
                "...) to hook AND drive the LIVE process. OR simpler, ONE call: "
                f"win_frida(launch='{_bat}', drive='...', script='...') launches "
                "it, finds the real process, and hooks+drives it - no PID juggling.")
    blocked = _security_block(script)
    if blocked:
        return VMResult(False, f"blocked: this script touches {blocked}")

    prog = None
    if program:
        prog = str(program).replace('"', "")
        if not re.match(r"^([A-Za-z]:|[\\/])", prog):
            prog = remote.win_samples() + "\\" + prog
    frida_sync_note = ""
    if prog and not pid:
        _ok, frida_sync_note = _sync_to_windows(prog)
        if not _ok:
            return VMResult(False, frida_sync_note)
    if isinstance(args, str):
        arglist = shlex.split(args) if args.strip() else []
    else:
        arglist = list(args or [])

    cfg = {"program": prog, "args": arglist, "wait": float(wait),
           "kill": bool(kill), "max_lines": int(max_lines),
           "js_path": remote.win_samples() + "\\_frida.js"}
    if pid:
        cfg["pid"] = int(pid)
    drive_ps1_local = None
    if drive_names:
        _dwin = str(drive_window or "").replace('"', "").replace("`", "")
        _drive_body = _ui_drive_ps(drive_names, _dwin, drive_pause, drive_after, 250)
        drive_ps1_local = _drive_body
        cfg["drive_ps1"] = remote.win_samples() + "\\_frida_drive.ps1"
    log_win = remote.win_samples() + "\\_frida_log.txt"
    log_js = log_win.replace("\\", "/")
    try:                      # a stale log from an earlier call would read as fresh data
        remote.ssh_exec(f'del /q "{log_win}"', host="windows", read_timeout=20)
    except Exception:  # noqa: BLE001
        pass
    script = _FRIDA_PRELUDE_TMPL.replace("__LOGPATH__", log_js) + script
    try:
        remote.write_remote("samples/_frida.js", script, host="windows")
        remote.write_remote("samples/_frida_cfg.json", json.dumps(cfg), host="windows")
        remote.write_remote("samples/_frida_drv.py", _FRIDA_DRIVER, host="windows")
        if drive_ps1_local is not None:
            remote.write_remote("samples/_frida_drive.ps1", drive_ps1_local, host="windows")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_frida: could not upload the script/driver: "
                                 f"{e.__class__.__name__}: {e}")

    drv = remote.win_samples() + "\\_frida_drv.py"
    try:
        if desktop:
            text, timed_out = remote.run_in_session1(
                '& "' + remote.WIN_PYTHON_EXE + '" "' + drv + '" 2>&1',
                timeout=int(timeout), task="dualvm_frida")
        else:
            o, e = remote.ssh_exec('"' + remote.WIN_PYTHON_EXE + '" "' + drv + '"',
                                   host="windows", read_timeout=int(timeout) + 30)
            text = ((o or "") + (e or "")).strip()
            timed_out = "__FRIDA_END__" not in text
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_frida failed: {e.__class__.__name__}: {e}")

    text = _fix_js_lines(text.replace("__FRIDA_END__", "").strip())
    if frida_sync_note:
        text = frida_sync_note + "\n" + text
    if "FRIDA_MISSING" in text:
        return VMResult(False, "win_frida: frida is not importable on the Windows VM - "
                                 + text[:300])
    if timed_out:
        return VMResult(False, _frida_timeout_msg(text, int(timeout), desktop,
                                                    wait, bool(drive_names))
                                 + "\n" + text[-600:])
    if not text:
        return VMResult(False, "win_frida: the driver produced NO output at all - "
                                 "that is a plumbing failure, not a result")
    nsend = 0
    m = re.search(r"SEND_COUNT=(\d+)", text)
    if m:
        nsend = int(m.group(1))
    jserr = "JS_ERROR" in text
    ferr = "FRIDA_ERROR" in text
    logged = ""
    try:
        logged = remote.read_bytes("samples/_frida_log.txt",
                                   host="windows").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - no log file is the normal case
        logged = ""
    if logged.strip():
        text += (f"\n=== log() file: {len(logged)} chars, samples/_frida_log.txt on "
                 "windows (read_file it for the rest) ===\n" + logged)
    # The hook-lifetime trap, caught at the one moment it is provable.
    if not logged.strip() and nsend == 0 and "Interceptor.attach" in script \
            and not (jserr or ferr):
        text += ("\n=== NOTE FROM THE TOOL ===\nYour hooks installed but nothing ever "
                 "hit them, and they are GONE now: the script is unloaded when this "
                 "call returns. Whatever runs the hooked code must run INSIDE this "
                 "call (drive the program from your own JS and raise `wait`); a "
                 "win_ui_seq or a second win_frida afterwards cannot reach a hook "
                 "from this one.")
    detail, _ = _spill("windows", text, "frida")
    head = f"win_frida: {nsend} send() line(s)"
    if logged.strip():
        head += f", {len(logged.splitlines())} log() line(s)"
    if jserr:
        head += "; the SCRIPT threw (see JS_ERROR - check the Frida 17 API names)"
    if ferr:
        head += "; frida itself errored (see FRIDA_ERROR)"
    # ok=False when the script never ran: a JS_ERROR with nothing sent used to return
    # ok=True, so the error text itself scored as "novel information" -> progress, the
    # stall counter reset, and a KNOWN fact was recorded about a hook that never
    # installed. Wrong API name is the most common failure this tool has.
    ok = not ((ferr or jserr) and nsend == 0 and not logged.strip())
    return VMResult(ok, head, detail=detail,
                      known=[f"win_frida({program or ('pid ' + str(pid))}) -> {text[:400]}"],
                      info=detail, flag=_find_match(text))


# --- Windows: ACTING on the GUI, not just looking at it -----------------------------
# win_windows/win_screenshot can only READ the desktop. A challenge that wants a button
# pressed or a password typed needs these. Everything runs through run_in_session1 -
# session 0 has no desktop to click on.
#
# MEASURED 2026-09-23, and it changed the design: on this VM UI Automation sees only as
# far as the WINDOW. A plain WinForms dialog's own controls come back as
#     [Pane] id='459772' name='' class=WindowsForms10.EDIT... pat=
# - generic Panes, no AutomationId, and an EMPTY pattern list, so there is no Invoke to
# press and no Value to set. Loading UIAutomationClientsideProviders as well (the usual
# fix) changed nothing: measured identical output with all three assemblies loaded.
# So UIA alone would have given the Brain a tool that always answers "not found".
# The Win32 layer below does not depend on UIA at all: EnumChildWindows walks the real
# control tree, WM_SETTEXT writes into an edit box, BM_CLICK presses a button, and a
# synthetic mouse click at the control's rectangle is the last resort. Both layers are
# reported and both are accepted as targets, because UIA IS the better answer for WPF
# and modern apps - it just cannot be the only one.

_PS_UI_PRELUDE = r"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Windows.Forms -ErrorAction SilentlyContinue
try { Add-Type -AssemblyName UIAutomationClientsideProviders -ErrorAction SilentlyContinue } catch { }
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.Condition]::TrueCondition
function Get-Tops($match) {
  $out = @()
  try {
    foreach ($k in $AE::RootElement.FindAll($TS::Children, $CT)) {
      if ($match -eq "" -or $k.Current.Name -like "*$match*" -or $k.Current.ClassName -like "*$match*") { $out += $k }
    }
  } catch { }
  return $out
}
function Describe($e, $i) {
  $c = $e.Current
  $t = $c.ControlType.ProgrammaticName -replace "ControlType\.",""
  $r = $c.BoundingRectangle
  $p = ""
  try { $p = (($e.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName -replace "PatternIdentifiers\.Pattern","" }) -join ",") } catch { }
  $rect = "off-screen"
  if (-not [double]::IsInfinity($r.X)) { $rect = ("{0},{1} {2}x{3}" -f [int]$r.X,[int]$r.Y,[int]$r.Width,[int]$r.Height) }
  # MEASURED 2026-09-23 (ch8 FlareAuthenticator, Qt): a Qt app exposes its widgets
  # through UIA with EMPTY Name and EMPTY AutomationId - a 5x5 grid of entry boxes came
  # back as 25 identical "[Edit] name='' id=''" lines. Without the value there is no way
  # to tell them apart or to see what a click did, so the only alternative was a
  # screenshot every step (expensive, and vision is Anthropic-path only). The value is
  # free here: these controls support ValuePattern and reading it costs one call.
  $v = ""
  if ($p -match "Value") {
    try {
      $vp = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
      if ($vp -ne $null) {
        $raw = [string]$vp.Current.Value
        if ($raw.Length -gt 120) { $raw = $raw.Substring(0,120) + "...(cut)" }
        $v = " val='" + ($raw -replace "`r"," " -replace "`n"," ") + "'"
      }
    } catch { }
  }
  return ("  UIA #" + $i + " [" + $t + "] id='" + $c.AutomationId + "' name='" + $c.Name +
          "'" + $v + " class=" + $c.ClassName + " enabled=" + $c.IsEnabled +
          " rect=" + $rect + " pat=" + $p)
}
function Find-One($match, $target) {
  foreach ($w in (Get-Tops $match)) {
    $all = @($w)
    try { $all += @($w.FindAll($TS::Descendants, $CT)) } catch { }
    foreach ($pass in 1,2,3) {
      foreach ($e in $all) {
        $c = $e.Current
        if ($pass -eq 1 -and $c.AutomationId -eq $target) { return $e }
        if ($pass -eq 2 -and $c.Name -eq $target) { return $e }
        if ($pass -eq 3 -and ($c.Name -like "*$target*" -or $c.AutomationId -like "*$target*")) { return $e }
      }
    }
  }
  return $null
}
"""


# The Win32 layer. One C# class so the PowerShell above it stays readable.
_PS_WIN32 = r"""
if (-not ("VmWin32" -as [type])) {
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class VmWin32 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr SendMessageStr(IntPtr h, uint m, IntPtr w, string l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr SendMessageBuf(IntPtr h, uint m, IntPtr w, StringBuilder l);
  [DllImport("user32.dll", EntryPoint="SendMessageW")] public static extern IntPtr SendMessageNum(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, IntPtr e);
  public static string Cls(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, 256); return s.ToString(); }
  public static string Txt(IntPtr h) {
    var s = new StringBuilder(1024);
    if (GetWindowText(h, s, 1024) > 0) return s.ToString();
    var b = new StringBuilder(1024);
    SendMessageBuf(h, 0x000D, (IntPtr)1024, b);   // WM_GETTEXT
    return b.ToString();
  }
  public static string Line(IntPtr h, string tag) {
    RECT r; GetWindowRect(h, out r);
    uint pid; GetWindowThreadProcessId(h, out pid);
    return "  " + tag + " hwnd=0x" + h.ToInt64().ToString("X8") + " id=" + GetDlgCtrlID(h)
         + " class=" + Cls(h) + " text='" + Txt(h).Replace("\r"," ").Replace("\n"," ") + "'"
         + " visible=" + IsWindowVisible(h) + " enabled=" + IsWindowEnabled(h)
         + " rect=" + r.L + "," + r.T + " " + (r.R-r.L) + "x" + (r.B-r.T) + " pid=" + pid;
  }
  public static List<string> Dump(string match, int limit) {
    var outp = new List<string>();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      if (!IsWindowVisible(h)) return true;
      string t = Txt(h), c = Cls(h);
      if (t.Length == 0 && c.Length == 0) return true;
      if (match.Length > 0 && t.IndexOf(match, StringComparison.OrdinalIgnoreCase) < 0
                           && c.IndexOf(match, StringComparison.OrdinalIgnoreCase) < 0) return true;
      outp.Add("=== WIN32 WINDOW " + Line(h, "TOP").Trim() + " ===");
      EnumChildWindows(h, delegate(IntPtr ch, IntPtr l2) {
        if (outp.Count < limit) outp.Add(Line(ch, "W32"));
        return true;
      }, IntPtr.Zero);
      return outp.Count < limit;
    }, IntPtr.Zero);
    return outp;
  }
  public static IntPtr Find(string match, string target) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      if (!IsWindowVisible(h)) return true;
      string t = Txt(h), c = Cls(h);
      if (match.Length > 0 && t.IndexOf(match, StringComparison.OrdinalIgnoreCase) < 0
                           && c.IndexOf(match, StringComparison.OrdinalIgnoreCase) < 0) return true;
      var kids = new List<IntPtr>();
      EnumChildWindows(h, delegate(IntPtr ch, IntPtr l2) { kids.Add(ch); return true; }, IntPtr.Zero);
      for (int pass = 1; pass <= 3 && found == IntPtr.Zero; pass++) {
        foreach (IntPtr k in kids) {
          string kt = Txt(k), kc = Cls(k);
          int id = GetDlgCtrlID(k);
          if (pass == 1 && (kt == target || id.ToString() == target)) { found = k; break; }
          if (pass == 2 && kt.IndexOf(target, StringComparison.OrdinalIgnoreCase) >= 0 && kt.Length > 0) { found = k; break; }
          if (pass == 3 && kc.IndexOf(target, StringComparison.OrdinalIgnoreCase) >= 0) { found = k; break; }
        }
      }
      return found == IntPtr.Zero;
    }, IntPtr.Zero);
    return found;
  }
  public static void Click(int x, int y) {
    SetCursorPos(x, y);
    mouse_event(0x0002, 0, 0, 0, IntPtr.Zero);
    mouse_event(0x0004, 0, 0, 0, IntPtr.Zero);
  }
  public static void ClickCentre(IntPtr h) {
    RECT r; GetWindowRect(h, out r);
    Click((r.L + r.R) / 2, (r.T + r.B) / 2);
  }
  public static void BmClick(IntPtr h) { SendMessageNum(h, 0x00F5, IntPtr.Zero, IntPtr.Zero); }
  public static void SetText(IntPtr h, string s) { SendMessageStr(h, 0x000C, IntPtr.Zero, s); }
}
"@
}
"""


def win_ui_tree(match=None, limit=250, timeout=45):
    m = str(match or "").replace('"', "").replace("`", "")
    body = _PS_UI_PRELUDE + _PS_WIN32 + """
$lim = __LIM__
$i = 0
$tops = Get-Tops "__MATCH__"
foreach ($w in $tops) {
  Write-Output ("=== UIA WINDOW name='" + $w.Current.Name + "' class=" + $w.Current.ClassName + " ===")
  try {
    foreach ($e in $w.FindAll($TS::Descendants, $CT)) {
      Write-Output (Describe $e $i); $i++
      if ($i -ge $lim) { break }
    }
  } catch { Write-Output ("  UIA_ENUM_FAILED " + $_.Exception.Message) }
  if ($i -ge $lim) { Write-Output "  ...(UIA cut at limit)"; break }
}
Write-Output ("UIA_CONTROL_COUNT=" + $i)
$w32 = [VmWin32]::Dump("__MATCH__", $lim)
foreach ($l in $w32) { Write-Output $l }
Write-Output ("W32_LINE_COUNT=" + $w32.Count)
if ($tops.Count -eq 0 -and $w32.Count -eq 0) { Write-Output "NO_WINDOW_MATCHED" }
""".replace("__LIM__", str(int(limit))).replace("__MATCH__", m)
    try:
        text, timed_out = remote.run_in_session1(body, timeout=int(timeout),
                                                 task="dualvm_uitree")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_ui_tree failed: {e.__class__.__name__}: {e}")
    if timed_out:
        return VMResult(False, f"win_ui_tree: no result within {int(timeout)}s")
    if "NO_WINDOW_MATCHED" in text:
        # ok=False: nothing was measured. As ok=True this scored as progress every time
        # (the text differs per call), so a wrong `match` string could be repeated
        # forever without ever tripping the stall detector.
        return VMResult(False, f"no window matches '{match}' - is the program running? "
                                 "(win_gui_run starts one on the real desktop)",
                          detail=text[:600],
                          known=[f"win_ui_tree({match}): no window matched"])
    def _count(rx):
        m = re.search(rx, text)
        return int(m.group(1)) if m else 0
    nu = _count(r"UIA_CONTROL_COUNT=(\d+)")
    nw = _count(r"W32_LINE_COUNT=(\d+)")
    detail, _ = _spill("windows", text, "uitree")
    return VMResult(True, f"win_ui_tree: {nu} UIA control(s), {nw} Win32 line(s)",
                      detail=detail,
                      known=[f"win_ui_tree({match or 'all'}) -> {text[:400]}"],
                      info=detail, flag=_find_match(text))


def win_ui_click(hwnd=None, target=None, window=None, x=None, y=None, after=1,
                 timeout=45):
    if hwnd is None and target is None and (x is None or y is None):
        return VMResult(False, "win_ui_click: give `hwnd` (from win_ui_tree's W32 "
                                 "lines), or `target`, or both `x` and `y`")
    w = str(window or "").replace('"', "").replace("`", "")
    t = str(target or "").replace('"', "").replace("`", "")
    hw = str(hwnd or "").replace('"', "").replace("`", "").strip()
    if hw:
        try:
            int(hw, 16) if hw.lower().startswith("0x") else int(hw)
        except ValueError:
            return VMResult(False, f"win_ui_click: hwnd='{hwnd}' is not a number - "
                                     "copy it verbatim from a W32 line (e.g. 0x000A1B2C)")
    act = _PS_UI_PRELUDE + _PS_WIN32 + """
$h = [IntPtr]::Zero
$did = ""
if ("__HWND__" -ne "") { $h = [IntPtr]([int64]("__HWND__")) }
if ($h -eq [IntPtr]::Zero -and "__TARGET__" -ne "") {
  $el = Find-One "__WINDOW__" "__TARGET__"
  if ($el -ne $null) {
    Write-Output ("TARGET_UIA " + (Describe $el 0).Trim())
    foreach ($pn in @("Invoke","Toggle","SelectionItem","ExpandCollapse")) {
      if ($did -ne "") { break }
      try {
        $pt = ([type]("System.Windows.Automation." + $pn + "Pattern"))::Pattern
        $p = $el.GetCurrentPattern($pt)
        if ($p -ne $null) {
          switch ($pn) {
            "Invoke"         { $p.Invoke() }
            "Toggle"         { $p.Toggle() }
            "SelectionItem"  { $p.Select() }
            "ExpandCollapse" { $p.Expand() }
          }
          $did = "CLICKED_UIA_" + $pn
        }
      } catch { }
    }
  }
  if ($did -eq "") { $h = [VmWin32]::Find("__WINDOW__", "__TARGET__") }
}
if ($did -eq "" -and $h -ne [IntPtr]::Zero) {
  Write-Output ("TARGET_W32 " + [VmWin32]::Line($h, "W32").Trim())
  $cls = [VmWin32]::Cls($h)
  if ($cls -match "(?i)button") { [VmWin32]::BmClick($h); $did = "CLICKED_BM_CLICK" }
  else { [VmWin32]::ClickCentre($h); $did = "CLICKED_MOUSE_ON_CONTROL" }
}
if ($did -eq "" -and "__XY__" -ne "") {
  [VmWin32]::Click(__X__, __Y__); $did = "CLICKED_MOUSE_AT___X__,__Y__"
}
if ($did -eq "") { Write-Output "NOT_FOUND" } else { Write-Output $did }
Start-Sleep -Seconds __AFTER__
"""
    act = (act.replace("__HWND__", hw).replace("__TARGET__", t)
              .replace("__WINDOW__", w).replace("__AFTER__", str(int(after)))
              .replace("__XY__", "yes" if (x is not None and y is not None) else "")
              .replace("__X__", str(int(x)) if x is not None else "0")
              .replace("__Y__", str(int(y)) if y is not None else "0"))
    what = (f"hwnd={hwnd}" if hw else f"target='{target}'" if t else f"({x},{y})")
    try:
        text, timed_out = remote.run_in_session1(act + _PS_LIST_WINDOWS,
                                                 timeout=int(timeout),
                                                 task="dualvm_uiclick")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_ui_click failed: {e.__class__.__name__}: {e}")
    if timed_out:
        return VMResult(False, f"win_ui_click: no result within {int(timeout)}s")
    if "NOT_FOUND" in text:
        return VMResult(False, f"win_ui_click: nothing matched {what}"
                                 + (f" in windows matching '{window}'" if window else "")
                                 + ". Run win_ui_tree first and target whichever layer "
                                   "came back non-empty: an hwnd from a W32 line for a "
                                   "WinForms/Win32 dialog, or the AutomationId/Name from a "
                                   "UIA line for a Qt app (Qt has no child hwnds at all). "
                                   "If neither layer names the control, click it by the "
                                   "x/y centre of the rect win_ui_tree printed.")
    m = re.search(r"CLICKED_[A-Za-z_0-9,]+", text)
    how = m.group(0) if m else "?"
    detail, _ = _spill("windows", text, "uiclick")
    return VMResult(True, f"clicked {what} via {how}", detail=detail,
                      known=[f"win_ui_click({what}) -> {text[:400]}"],
                      info=detail, flag=_find_match(text))


_SENDKEYS_SPECIAL = "+^%~(){}[]"


def win_ui_type(text, hwnd=None, target=None, window=None, enter=False,
                sendkeys=False, after=1, timeout=45):
    if text is None:
        return VMResult(False, "win_ui_type: `text` is required")
    raw = str(text)
    keys = raw if sendkeys else "".join(
        ("{" + c + "}") if c in _SENDKEYS_SPECIAL else c for c in raw)
    keys = keys.replace('"', "").replace("`", "")
    val = raw.replace('"', "").replace("`", "")
    w = str(window or "").replace('"', "").replace("`", "")
    t = str(target or "").replace('"', "").replace("`", "")
    hw = str(hwnd or "").replace('"', "").replace("`", "").strip()
    if hw:
        try:
            int(hw, 16) if hw.lower().startswith("0x") else int(hw)
        except ValueError:
            return VMResult(False, f"win_ui_type: hwnd='{hwnd}' is not a number - "
                                     "copy it verbatim from a W32 line")
    body = _PS_UI_PRELUDE + _PS_WIN32 + """
$h = [IntPtr]::Zero
$did = ""
if ("__HWND__" -ne "") { $h = [IntPtr]([int64]("__HWND__")) }
if ($h -eq [IntPtr]::Zero -and "__TARGET__" -ne "") {
  $el = Find-One "__WINDOW__" "__TARGET__"
  if ($el -ne $null) {
    Write-Output ("TARGET_UIA " + (Describe $el 0).Trim())
    try {
      $p = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
      if ($p -ne $null -and -not $p.Current.IsReadOnly) { $p.SetValue("__VAL__"); $did = "TYPED_UIA_VALUE" }
    } catch { }
    if ($did -eq "") { try { $el.SetFocus(); Start-Sleep -Milliseconds 250 } catch { } }
  }
  if ($did -eq "") { $h = [VmWin32]::Find("__WINDOW__", "__TARGET__") }
}
if ($did -eq "" -and $h -ne [IntPtr]::Zero) {
  Write-Output ("TARGET_W32 " + [VmWin32]::Line($h, "W32").Trim())
  [VmWin32]::SetText($h, "__VAL__")
  $back = [VmWin32]::Txt($h)
  Write-Output ("READBACK='" + $back + "'")
  if ($back -eq "__VAL__") { $did = "TYPED_WM_SETTEXT" }
  else {
    [VmWin32]::ClickCentre($h); Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait("__KEYS__")
    $did = "TYPED_SENDKEYS_AFTER_WM_SETTEXT_FAILED"
  }
}
if ($did -eq "") {
  [System.Windows.Forms.SendKeys]::SendWait("__KEYS__")
  $did = "TYPED_SENDKEYS_TO_FOCUS"
}
Write-Output $did
if (__ENTER__) {
  if ($h -ne [IntPtr]::Zero) { [VmWin32]::SetForegroundWindow($h) | Out-Null; [VmWin32]::SetFocus($h) | Out-Null }
  [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  Write-Output "PRESSED_ENTER"
}
Start-Sleep -Seconds __AFTER__
"""
    body = (body.replace("__HWND__", hw).replace("__TARGET__", t)
                .replace("__WINDOW__", w).replace("__VAL__", val)
                .replace("__KEYS__", keys).replace("__AFTER__", str(int(after)))
                .replace("__ENTER__", "$true" if enter else "$false"))
    where = (f"hwnd={hwnd}" if hw else f"target='{target}'" if t else "the focused control")
    try:
        out, timed_out = remote.run_in_session1(body + _PS_LIST_WINDOWS,
                                                timeout=int(timeout),
                                                task="dualvm_uitype")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_ui_type failed: {e.__class__.__name__}: {e}")
    if timed_out:
        return VMResult(False, f"win_ui_type: no result within {int(timeout)}s")
    m = re.search(r"TYPED_[A-Z_]+", out)
    how = m.group(0) if m else "?"
    if (t or hw) and "TARGET_UIA" not in out and "TARGET_W32" not in out \
            and how == "TYPED_SENDKEYS_TO_FOCUS":
        # The hwnd branch had no such guard: a stale/closed handle fell through to
        # SendKeys and the tool still reported "typed 'xyz' into hwnd=0x...". The Brain
        # then filed a KNOWN fact that it had filled a field it had not touched.
        aimed = f"target='{target}'" if t else f"hwnd={hwnd}"
        return VMResult(False, f"win_ui_type: nothing matched {aimed} - the text went "
                                 "to whatever had focus instead, which is probably not "
                                 "what you wanted. Run win_ui_tree again (the handle may "
                                 "be stale) and use a CURRENT hwnd or AutomationId.")
    detail, _ = _spill("windows", out, "uitype")
    return VMResult(True, f"typed '{raw[:40]}' into {where} via {how}", detail=detail,
                      known=[f"win_ui_type({where}) -> {out[:400]}"],
                      info=detail, flag=_find_match(out))


def win_key(keys, window=None, after=1, timeout=45):
    raw = str(keys or "")
    if not raw.strip():
        return VMResult(False, "win_key: `keys` is empty - give SendKeys text, "
                                 "e.g. keys='{ENTER}' to accept a dialog or '{ESC}' to "
                                 "cancel one")
    if '"' in raw or "`" in raw:
        return VMResult(False, "win_key: `keys` cannot contain a double-quote or "
                                 "backtick - those are not valid SendKeys anyway")
    w = str(window or "").replace('"', "").replace("`", "")
    body = _PS_UI_PRELUDE + _PS_WIN32 + """
$foc = ""
if ("__WINDOW__" -ne "") {
  $tops = Get-Tops "__WINDOW__"
  if ($tops.Count -gt 0) {
    try {
      $hwnd = [IntPtr]$tops[0].Current.NativeWindowHandle
      [VmWin32]::SetForegroundWindow($hwnd) | Out-Null
      Start-Sleep -Milliseconds 250
      $foc = "FOCUSED '" + $tops[0].Current.Name + "'"
    } catch { $foc = "FOCUS_FAILED " + $_.Exception.Message }
  } else { $foc = "NO_WINDOW_MATCHED" }
}
if ($foc -ne "") { Write-Output $foc }
if ($foc -ne "NO_WINDOW_MATCHED") {
  [System.Windows.Forms.SendKeys]::SendWait("__KEYS__")
  Write-Output "SENT __KEYS__"
}
Start-Sleep -Seconds __AFTER__
"""
    body = (body.replace("__WINDOW__", w).replace("__KEYS__", raw)
                .replace("__AFTER__", str(int(after))))
    try:
        out, timed_out = remote.run_in_session1(body + _PS_LIST_WINDOWS,
                                                timeout=int(timeout), task="dualvm_key")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_key failed: {e.__class__.__name__}: {e}")
    if timed_out:
        return VMResult(False, f"win_key: no result within {int(timeout)}s")
    if "NO_WINDOW_MATCHED" in out:
        return VMResult(False, f"win_key: no window matches '{window}' - run "
                                 "win_ui_tree to see the exact title, or omit `window` "
                                 "to send to the foreground window.")
    detail, _ = _spill("windows", out, "winkey")
    return VMResult(True, f"sent keys '{raw[:40]}'"
                      + (f" to '{window}'" if window else " to the foreground window"),
                      detail=detail, known=[f"win_key({raw[:40]}) -> {out[:300]}"],
                      info=detail, flag=_find_match(out))


# Build the proven UIA InvokePattern drive PowerShell (used by win_ui_seq, and by
# win_frida's `drive` so the app is clicked WHILE the Frida hooks are still live).
_UI_DRIVE_TMPL = """
$names = @(__SEQ__)
$tops = Get-Tops "__WINDOW__"
if ($tops.Count -eq 0) { Write-Output "NO_WINDOW_MATCHED" } else {
  $win = $tops[0]
  Write-Output ("SEQ WINDOW name='" + $win.Current.Name + "' class=" +
                $win.Current.ClassName + " matched=" + $tops.Count)
  $map = @{}
  try {
    foreach ($e in $win.FindAll($TS::Descendants, $CT)) {
      $id = $e.Current.AutomationId; $nm = $e.Current.Name
      if ($id -and -not $map.ContainsKey($id)) { $map[$id] = $e }
      if ($nm -and -not $map.ContainsKey($nm)) { $map[$nm] = $e }
    }
  } catch { Write-Output ("UIA_ENUM_FAILED " + $_.Exception.Message) }
  $ok = 0; $fail = 0
  foreach ($n in $names) {
    $e = $map[$n]
    if ($e -eq $null) {
      Write-Output ("FAILED '" + $n + "' - no control with that AutomationId or Name")
      $fail++
      continue
    }
    try {
      $ip = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
      if ($ip -eq $null) { throw "no InvokePattern" }
      $ip.Invoke()
      $ok++
    } catch {
      Write-Output ("FAILED '" + $n + "' - " + $_.Exception.Message)
      $fail++
      continue
    }
    Start-Sleep -Milliseconds __PAUSE__
  }
  Write-Output ("PRESSED=" + $ok + " FAILED=" + $fail + " OF=" + $names.Count)
  Start-Sleep -Seconds __AFTER__
  Write-Output ("=== UIA WINDOW AFTER SEQUENCE name='" + $win.Current.Name + "' ===")
  $i = 0
  try {
    foreach ($e in $win.FindAll($TS::Descendants, $CT)) {
      Write-Output (Describe $e $i); $i++
      if ($i -ge __LIM__) { Write-Output "  ...(dump cut at limit)"; break }
    }
  } catch { Write-Output ("UIA_ENUM_FAILED " + $_.Exception.Message) }
  Write-Output ("UIA_CONTROL_COUNT=" + $i)
}
"""


def _ui_drive_ps(names, window, pause=250, after=2, limit=250):
    seq = ",".join("'" + n + "'" for n in names)
    return _PS_UI_PRELUDE + _UI_DRIVE_TMPL.replace("__SEQ__", seq) \
        .replace("__WINDOW__", str(window)).replace("__PAUSE__", str(int(pause))) \
        .replace("__AFTER__", str(int(after))).replace("__LIM__", str(int(limit)))


def win_ui_seq(clicks, window=None, pause=250, after=2, limit=250, timeout=120):
    raw = str(clicks or "")
    names = [x.strip() for x in raw.split(",")] if "," in raw else list(raw)
    names = [n for n in names if n]
    if not names:
        return VMResult(False, "win_ui_seq: `clicks` is empty - give e.g. '1,2,3,OK'")
    if len(names) > 200:
        return VMResult(False, f"win_ui_seq: {len(names)} presses in one call is over "
                                 "the 200 cap - split the sequence")
    bad = [n for n in names if '"' in n or "`" in n or "'" in n or "\n" in n]
    if bad:
        return VMResult(False, f"win_ui_seq: button name {bad[0]!r} contains a quote or "
                                 "backtick and cannot be passed through - click that one "
                                 "with win_ui_click")
    w = str(window or "").replace('"', "").replace("`", "")
    seq = ",".join("'" + n + "'" for n in names)
    body = _PS_UI_PRELUDE + """
$names = @(__SEQ__)
$tops = Get-Tops "__WINDOW__"
if ($tops.Count -eq 0) { Write-Output "NO_WINDOW_MATCHED" } else {
  $win = $tops[0]
  Write-Output ("SEQ WINDOW name='" + $win.Current.Name + "' class=" +
                $win.Current.ClassName + " matched=" + $tops.Count)
  # Resolve every button ONCE up front. Re-running FindAll per press would multiply the
  # cost of a 25-press sequence by 25 for no new information - the control set of a
  # keypad does not change while you press it.
  $map = @{}
  try {
    foreach ($e in $win.FindAll($TS::Descendants, $CT)) {
      $id = $e.Current.AutomationId; $nm = $e.Current.Name
      if ($id -and -not $map.ContainsKey($id)) { $map[$id] = $e }
      if ($nm -and -not $map.ContainsKey($nm)) { $map[$nm] = $e }
    }
  } catch { Write-Output ("UIA_ENUM_FAILED " + $_.Exception.Message) }
  $ok = 0; $fail = 0
  foreach ($n in $names) {
    $e = $map[$n]
    if ($e -eq $null) {
      Write-Output ("FAILED '" + $n + "' - no control with that AutomationId or Name")
      $fail++
      continue
    }
    try {
      $ip = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
      if ($ip -eq $null) { throw "no InvokePattern" }
      $ip.Invoke()
      $ok++
    } catch {
      Write-Output ("FAILED '" + $n + "' - " + $_.Exception.Message)
      $fail++
      continue
    }
    Start-Sleep -Milliseconds __PAUSE__
  }
  Write-Output ("PRESSED=" + $ok + " FAILED=" + $fail + " OF=" + $names.Count)
  Start-Sleep -Seconds __AFTER__
  Write-Output ("=== UIA WINDOW AFTER SEQUENCE name='" + $win.Current.Name + "' ===")
  $i = 0
  try {
    foreach ($e in $win.FindAll($TS::Descendants, $CT)) {
      Write-Output (Describe $e $i); $i++
      if ($i -ge __LIM__) { Write-Output "  ...(dump cut at limit)"; break }
    }
  } catch { Write-Output ("UIA_ENUM_FAILED " + $_.Exception.Message) }
  Write-Output ("UIA_CONTROL_COUNT=" + $i)
}
""".replace("__SEQ__", seq).replace("__WINDOW__", w) \
   .replace("__PAUSE__", str(int(pause))).replace("__AFTER__", str(int(after))) \
   .replace("__LIM__", str(int(limit)))
    try:
        text, timed_out = remote.run_in_session1(body, timeout=int(timeout),
                                                 task="dualvm_uiseq")
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"win_ui_seq failed: {e.__class__.__name__}: {e}")
    if timed_out:
        budget = len(names) * int(pause) / 1000.0 + int(after)
        return VMResult(False, f"win_ui_seq: no result within {int(timeout)}s - "
                                 f"{len(names)} presses at {int(pause)}ms plus the read "
                                 f"needs at least ~{budget:.0f}s, so raise `timeout` or "
                                 "lower `pause`")
    if "NO_WINDOW_MATCHED" in text:
        return VMResult(False, f"win_ui_seq: no window matches '{window}' - is the "
                                 "program running? win_gui_run(kill=false) starts one",
                          detail=text[:600],
                          known=[f"win_ui_seq({window or 'all'}): no window matched"])
    m = re.search(r"PRESSED=(\d+) FAILED=(\d+) OF=(\d+)", text)
    if m:
        nok, nfail, ntot = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        nok, nfail, ntot = 0, 0, len(names)
    detail, _ = _spill("windows", text, "uiseq")
    summary = f"win_ui_seq: pressed {nok}/{ntot}" + (f", {nfail} FAILED" if nfail else "")
    return VMResult(nok > 0, summary, detail=detail,
                      known=[f"win_ui_seq({raw[:60]}) -> {text[:400]}"],
                      info=detail, flag=_find_match(text))
