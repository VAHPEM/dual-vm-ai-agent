---
name: binary-analysis
description: >-
  Disciplined methodology for analysing an unknown file, binary or program on the
  dual-VM lab: how to reason (state assumptions, measure before reading, static vs
  dynamic, invert instead of brute-force) and which VM tool to reach for at each
  step. Load it before analysing a target with dualvm. Covers reverse engineering,
  crypto, parameter reasoning, Python bytecode, large traces, and tooling gotchas.
---

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

=== MANDATORY RULES — DO NOT DERAIL ===
(Violating any of these causes more damage than any gap in knowledge.)

R1 STATE YOUR ASSUMPTIONS. Every proposed step carries a line
   "This step assumes: ...". An untested belief is named as an assumption,
   never stated with false confidence.

R2 TWO-DEAD-END RULE. After two consecutive dead ends, do NOT propose another
   step. Instead, list every assumption you are relying on and propose a
   direct test for the one most likely to be wrong.

R3 THREE-COLUMN LEDGER (maintain it; reprint on request):
     KNOWN     — measured and reproducible
     ASSUMED   — believed but not yet verified
     RULED OUT — tested and excluded, with the evidence.

R4 PARAMETERS HAVE TWO DIRECTIONS. For any number acting as a parameter
   (a filename, CLI arg, header field), state BOTH readings aloud:
   "steps already taken" vs "steps remaining". Reading the direction wrong is
   the classic trap.

R5 CHECK THE OPERATION, NOT WHETHER THE OUTPUT LOOKS PRETTY. For a repeated
   transform, test the CONSISTENCY of the operation (XOR? add? sub? derivable
   from a step index?). A delta that is not ASCII is not evidence of nothing.

R6 DISTINGUISH: corrupted vs. empty vs. the tool failed to read it.

R7 VERIFY FILE IDENTITY BEFORE CONCLUDING. Do not trust the filename — check
   a hash or a byte-diff to be sure you are analyzing the right copy.

R8 MEASURE BEFORE YOU READ. For binaries from unusual compilers (VB6/twinBASIC,
   Delphi, Go, packers), decompiled output is mostly compiler-generated memory
   management. Prefer: run twice and diff, diff states byte by byte, count what
   changed and in which section — BEFORE reading assembly.

R9 THE OBVIOUS FLAG CAN BE A DECOY. A string shown at the "win"/success screen may be
   bait - verify the flag DOMAIN character by character (f1are != flare) and be suspicious
   when the win path is trivially reachable; the real flag often sits on a hidden/unused
   code path (a registered-but-uncalled handler, a branch the happy path never takes).

R10 EMBEDDED RUNTIME => GO DYNAMIC. When the target embeds its own engine (V8/JS via
   Tauri/Electron/nexe, a WASM module, a bundled Python/Lua, a big Go/Rust host), the check
   logic runs at RUNTIME, not in the host's static code, and Ghidra will time out on the
   size. Break in a debugger or run the payload in its own engine, and prefer REPLACING the
   script/input at the load boundary to measure native functions as black boxes.

=== DEFAULT ORDER OF OPERATIONS — cheapest first ===
Start with the cheap static survey below. Reach for dynamic analysis (running it,
a debugger, emulation) AS SOON AS static reading stops answering the question:
two empty static looks at the same thing is the signal to switch anchor or go
dynamic, not to look a third time. Cheap-first is about cost, not about avoiding
dynamic work.
  1. file, size, hashes — compare against what the extension claims.
  2. Magic bytes BY HAND (xxd -l 64) — extensions lie; a corrupted signature
     byte is a common trick. Read the actual first bytes and compare them,
     byte for byte, against the magic-byte table. If byte 0 differs from every
     known signature, treat the file as deliberately tampered and say so
     explicitly before choosing a strategy. NEVER report a magic value you did
     not literally see in the hex dump.
  3. Structure: sections, imports, exports, resources.
  4. Strings, then embedded-format signatures (PNG/GIF/JPEG/ZIP/zlib); note
     regions that are oversized or high-entropy.

=== TECHNIQUE PLAYBOOK (apply when it fits the situation at hand — not every
note is relevant to every challenge) ===

--- Debug / disassembly ---
- Strings in the register panel may be display-truncated — right-click ->
  "Follow in Dump" before concluding the data is "garbage".
- A breakpoint on a shared helper hits many times for many purposes — inspect
  the specific args (RCX/RDX...) at EACH hit; do not assume the first hit is
  the one you want.
- IDA "graph too big — switching to text mode": normal, not a broken binary.
- Ghidra "Response buffer" / "Flow exceeded maximum instructions" errors on
  C++ linked against heavy header-only template libs (cpp-httplib, boost...):
  this identifies the binary type, it is not a config error. Read
  RCX/RDX/R8/R9 right before the CALL, or breakpoint the child function,
  instead of trying to decompile the parent.
- PIC/ASLR binary: use relative breakpoints (module+offset) in x64dbg, not
  absolute virtual addresses.
- Decompiler heavily polluted by interleaved junk code (a global written/read
  over and over): fix the decompiler config, patch the microcode, or write a
  Hex-Rays hook that filters pseudocode by regex — more effective than reading
  thousands of noisy lines by hand.
- Obfuscated data/strings can be recovered with a debugger or emulation
  (e.g. flare-emu) instead of translating the decompiler output by hand,
  especially when the decompiler is buried in junk code.

--- Qt / GUI crackme ---
- Qt app with direct input (QLineEdit): breakpoint a Qt API the handler surely
  calls (e.g. QLineEdit::setText) to locate the handler — faster/surer than
  chasing connect()/constructor via static xrefs (two empty xrefs in a row =
  switch your anchor point).
- Indirect calls of the form DAT_global + constant -> called repeatedly with
  NO branch on user input: usually just obfuscation wrapping ordinary app-init
  code, not the check logic — prefer finding the real UI event handler.
- "Type N chars into N boxes" may be an accumulator summed as each char is
  entered, not a single compare at submit — set a hardware (read) breakpoint on
  the suspect variable instead of only trapping the result-display function.
- Before choosing to "bypass the condition" (patch the return, brute one
  branch), always ask: "is the flag COMPUTED from the valid input, or does it
  only depend on the input making a check pass/fail?" — bypassing is valid only
  once you've confirmed no later step reuses the real input as a key/seed.
- A counter used as an index may ACCUMULATE, not just overwrite — especially
  when elements depend on each other (a DAG), the processing order may be the
  unknown to solve, not something you get to choose freely.
- "Sum of N terms = constant", each term bounded to a discrete non-overlapping
  domain, domains sparse enough: usually a unique solution = take the max (or
  min) of every domain. Verify by random sampling first, then derive directly
  by argmax instead of solving a system.
- Unicorn Engine + pefile (map sections with correct permissions) to emulate a
  small child function directly and build the domain of feasible values — much
  cheaper than stepping through a debugger by hand.

--- Parameters / arithmetic reasoning ---
- A "counter" parameter: don't assume it's a loop count — try >=3 different
  values (not just 0 vs 1) to reveal it's only a boolean flag (this is R4:
  measure, don't infer).
- Recognize "modular exponentiation mod 2^n": compiler-generated 128-bit
  divide (__udivti3/__umodti3) called repeatedly, interleaved with accumulating
  mul/imul -> signature of square-and-multiply for a bignum modexp, NOT matrix
  multiplication just because of nested loops.
- Invert modexp mod 2^n (automorphism on odd numbers): save the LSB, force odd,
  raise to the inverse of e modulo lambda(2^n) (Carmichael, lambda(2^256)=2^254;
  phi(2^256)=2^255 works too), then restore the original LSB.
- Invert "M^e mod p" when M is an invertible NxN matrix mod p: Lagrange —
  group order n = product(p^N - p^k), k=0..N-1; if gcd(e,n)=1 then
  d=e^-1 mod n gives (M^e)^d=M. Applies to any "matrix raised to a power mod p".
- Known-plaintext + an invertible op (XOR) is a strong lever: one
  plaintext/ciphertext pair is enough to recover the secret parameter without
  knowing the original input that produced it.
- If a transcription of an algorithm from bytecode keeps failing verification
  even though the core formula is measured and certain: stop hunting for the
  transcription bug — switch to a pure-data attack if you have enough samples
  (e.g. break an unknown-modulus LCG via GCD of consecutive differences, needs
  >=5-6 samples), then cross-check against an independent source in the
  challenge (e.g. a public key it already contains) — cross-verification beats
  "the formula looks right".
- When you have real source to compare against (e.g. a Solidity contract),
  always cross-check conclusions derived from behavior — it reinforces
  "measure first, read second", it does not replace it.

--- Large traces / mass duplicates ---
- A code pattern repeated too many times to read by eye (e.g. tens of thousands
  of same-shaped cases): confirm the shape on 2-3 hand-read samples, then write
  a script to parse it automatically.
- A challenge with N copies of the same check function (randomized template +
  per-copy constants): do not read each copy — confirm the finite template set
  on a few samples, then write an automatic recognizer (regex on the
  disassembly, or symbolic execution via angr) applied to all of them.

--- Python / bytecode ---
- Python decompiler doesn't support the compile version (e.g. Python 3.12+,
  decompyle3/uncompyle6 say "unsupported version"): use pydisasm (the xdis
  package) to read the bytecode directly instead of forcing a decompiler.
- Always list the whole PyInstaller extraction dir (find ... ! -iname "*.pyc"),
  not just the .pyc — such extractions often embed real data (logs/keys/configs) as
  artifacts needed for the solution.

--- Crypto / data encoding ---
- Hand-rolled AES (not linked to OpenSSL/BCRYPT/CRYPT32): scan for the standard
  forward/inverse S-box byte signature directly in the file — cheaper than
  reading strings or blindly decompiling; confirm the algorithm before you know
  the key/mode.

--- Tooling / shell gotchas ---
- rabin2 -i ... | awk mis-columns easily when demangled C++ names contain
  spaces — use rabin2 -ij (JSON) + python3/jq instead of guessing columns from
  text.
- The '!' char in python3 -c "..." can be eaten by bash/zsh history expansion,
  causing confusing errors — write the script to a file via a quoted heredoc
  (<< 'EOF') and run python3 file.py; avoid -c when special chars are involved.
