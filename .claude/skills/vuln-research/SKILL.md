---
name: vuln-research
description: >-
  Methodology for finding and triaging memory-safety bugs in a binary you are
  analysing on the dual-VM lab: establish the mitigation state, surface crashes
  (static recognition + fuzzing), and triage each crash for whether it is a real,
  reachable, attacker-influenced defect — mapped to the dualvm tools. Load it for a
  pwn-style target, a crash to assess, or when deciding if a binary has a
  memory-safety vulnerability. Triage and analysis only; it stops at "is this a real
  primitive", not weaponisation.
---

# vuln-research

A disciplined way to answer one question about a binary: **does it have a real,
reachable memory-safety defect, and what kind?** This is triage and analysis — the
same measure-before-you-conclude discipline as `binary-analysis`, applied to crashes
and bug classes. It stops at characterising a primitive; building an exploit is a
separate, later decision.

Pair it with `binary-analysis` (the general discipline R1-R10). Everything here runs
on the Kali VM through `dualvm`.

## Order of work (cheapest signal first)

1. **Mitigation state before anything.** It bounds what any bug can even mean.
   `shell.run_cmd("checksec --file=./target")` — note NX, PIE, RELRO, stack canary,
   Fortify. A write bug under full RELRO + canary + PIE is a very different finding
   from the same bug with none of them. State it as a KNOWN fact, don't assume.
2. **Recognise the likely class statically** (see the cheatsheet) from imports and
   decompilation — `gets`/`strcpy`/`sprintf`, unchecked `memcpy` length, a `free`
   then reuse, a `%s`/`%n` reachable from input, a size computed by `a*b` with no
   overflow check. This tells you what to look for, not that it is there.
3. **Surface crashes.** Two paths, cheapest first:
   - **Targeted inputs**: feed the specific malformed input the static read suggests
     (`shell.run_script` building the input, then run the target).
   - **Fuzzing** when the input space is non-trivial: build with sanitizers and fuzz.
     `shell.run_cmd` to compile (`clang -fsanitize=address,undefined -g` for source;
     for a blackbox binary use AFL++ in QEMU mode), then run the campaign as a
     background job: `jobs.job_start(name="fuzz", command="afl-fuzz -i in -o out -Q -- ./target @@")`
     and watch it with `jobs.job_poll("fuzz", grep="SAVING|crashes")`. ASAN/UBSAN
     turn a silent corruption into a precise, located report — prefer them to a bare
     segfault.

## Triage a crash (`linux.linux_gdb`) — the five questions

A crash is not a vulnerability until you have answered these. Run the target under
the scripted debugger and read, don't guess:

1. **What faulted?** The instruction and the access (read/write/exec), the faulting
   address. `hook` the crash site or let it trap; `log(reg('rip'))`, the signal.
2. **Is the faulting value / pointer input-controlled?** Compare the faulting
   address or overwritten data to bytes from your input (a cyclic pattern makes the
   offset obvious). "Controlled" is the dividing line between a bug and a vulnerability.
3. **Which class?** Map the fault to a class (cheatsheet below) from what the sanitizer
   or the memory map says — a write past a stack frame vs a heap chunk vs a freed
   chunk are different findings.
4. **Is it reachable** on a normal/plausible input path, or only via a state the
   program never enters? An unreachable crash is not a finding (this is R9 from the
   discipline: verify the path is real).
5. **What primitive, at the level of a sentence?** "N-byte controlled write past a
   64-byte stack buffer, no canary" or "controlled free of an attacker-sized chunk".
   Stop here. Whether that primitive is usable, and how, is a separate exercise — and
   in a challenge, first ask (binary-analysis): is the goal computed from the input,
   or only gated by a check you could instead satisfy?

## Bug-class cheatsheet (the tell, not the exploit)

- **Stack/heap buffer overflow** — a copy whose length is not bounded by the
  destination size; canary/ASAN flags it; offset found with a cyclic pattern.
- **Use-after-free / double-free** — a pointer used after `free`, or freed twice;
  ASAN names it precisely; look at object lifetime, not just the crash site.
- **Integer overflow → short allocation** — `malloc(a*b)` or `len+k` wrapping, then a
  write sized by the pre-wrap value. The multiply/add is the tell, the overflow the bug.
- **Type confusion** — one memory region interpreted as two incompatible types
  (a tag/union mismatch, a vtable from the wrong class). Usually a logic flaw upstream.
- **Format string** — a user string reaching the format argument of `printf`-family;
  `%p`/`%x` leak, `%n` writes. Grep the decompilation for the call shape.
- **OOB read / info leak** — a read past bounds; often the first step that defeats
  PIE/canary by leaking an address. Treat a leak as a finding in its own right.

## Discipline (inherits binary-analysis R1-R10)

- A crash ≠ a vulnerability ≠ an exploitable vulnerability. Say which one you have
  evidence for, and name the gap to the next.
- Measure control, don't infer it: a cyclic pattern and the real faulting bytes beat
  "it looks like my input reached it".
- Mitigations are facts to measure (`checksec`), not assumptions.
- Reproduce before concluding: a crash that only happens once, or only under the
  fuzzer's instrumentation, may be a harness artefact (R6: corrupted vs empty vs the
  tool failing).
- This skill ends at the primitive. Keep raw measurement in `dualvm`; the decision of
  what to do with a primitive is the caller's.
