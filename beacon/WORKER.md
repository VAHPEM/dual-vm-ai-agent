# Beacon worker rules (paste into the worker project's CLAUDE.md)

This session may be supervised by a **beacon run** (the SessionStart hook says so and
shows the ledger). When it is:

- **The ledger is the plan.** Work the node the ledger names next. Before working on a
  todo node, `start_node` it. Look at the whole ledger with `ledger_view`.
- **Record every option, don't offer it.** Whenever you think of another way to solve
  something — including "if you want, I could also …" — record it with `add_node`
  (a *solution* is a hypothesis under a problem; an *approach* is a concrete way to
  test it). At a branch, call `ask(kind="approach_choice", options=[...], node_id=<the
  problem or solution>, context=<what bears on the choice: evidence ids, what each option
  would show, its cost and risk>)` with the options **best first**. The human is not asked:
  the options become nodes and the decider (a separate Opus) ranks them from the ledger and
  this session; its reply names the option to start, the first step and the result that
  means switching. Follow it; if a result contradicts it, record that and take the next.
  Without a decider your own order stands: effectiveness first, then cost (most likely to
  reach the answer; a quick check that tells which option works goes first; on similar
  chances, the cheaper). Never end a turn on "A or B?": nobody will answer it.
- **Found a new problem of its own** (a second crash in another parser, another payload
  stage, a separate component)? `propose_problem(title, rationale, evidence_ids)`. It is open
  at once and part of the run: work on it like any other problem (if the reply says it waits
  for the human, leave it until they accept it). A sub-step of an existing problem
  is a solution or an approach (`add_node`), not a problem. You write code, scripts and notes
  in the project as usual; attach what they produced as evidence (`ref` = the file path).
- **Evidence for everything.** After a step that produced output, `attach_evidence`
  with the raw output (or the VM spill file path in `ref` plus its sha256 in
  `content`). Identical output does not count twice.
- **Closing nodes.** `set_status(done)` needs evidence in the node's subtree.
  `set_status(dead_end)` needs the reason **and the assumption it rests on** — what
  would have to be false for this approach to work after all. Assumptions get
  audited; write the real one.
- **Problems end only through `propose_done`** with the candidate and the evidence
  ids. If the checklist gave a verifier it runs at once; a rejected candidate is
  recorded and the problem stays open.
- **Nearly there?** `claim_near_done(node, remaining_steps=[...])` with the concrete
  steps. You will be held to them. Repeated claims without new evidence trigger a
  review of the assumption behind the claim.
- **Waiting on a background job** (`labhand.jobs.job_start`)? `mark_waiting(node, job)`
  so the wait is not counted as idle, then poll with `labhand.jobs.job_poll` and
  attach the output.
- **Keep the VM honest.** `vm_check(host, node_id)` before an approach that depends on a
  clean VM and before declaring such an approach a dead end: leftover processes, a bound
  port or changed files in samples/ can make a sound approach fail. The first check (or
  `set_baseline=true` on a freshly reverted VM) records the baseline.
- **Asking.** Only information the human alone has goes to the human:
  `ask(kind="need_info")` for a missing file, a password, a scope decision; the node waits
  for the answer, continue with other nodes. **Requests to run things are approved by the
  human's standing rule**: running a command or a tool, running a program on the Kali or
  Windows VM, installing an analysis tool — `ask(kind="need_permission")` answers at once,
  then go ahead and record what you ran as evidence. Running a binary is approved the same way;
  only a run with `ask_before_running_real_samples` on holds a request to execute a **real
  malware sample** for the human, so say "real malware sample" when that is what you ask.
  Choices between approaches are made for you (see above). Do not end a turn on a question
  that someone else has to answer.
- **Do not end a turn with a list of what is left.** "Still missing: not tested on Windows,
  X could also be checked, Y needs ..." describes work instead of doing it. Do each item
  now; record what you cannot do yet (`add_node` under its problem, `propose_problem` for
  new scope, `ask(kind="need_info")` for what only the human has, `set_status dead_end`
  with the assumption for what cannot be done). Beacon reads your final message: every
  unfinished item you list becomes a node you are kept working on (marked ↻), and only
  what truly needs the human goes to them. A follow-up problem ("Follow-ups of P1") closes
  with `propose_done` once every item in it is done or a dead end; no human check.
- **When you are stuck, step back** before trying more of the same: re-read the problem
  statement and the files you were given; re-read your evidence and list every step you took
  — what did you skip, assume or never check?; test the weakest assumption under your dead
  ends; widen the view (other files and components, other approach classes); check the VM
  state; the obvious answer may be a decoy. The nudge and the reviewers will remind you.
- The project's safety rules still apply in full. The gate keeps you working; it
  never makes a refused or out-of-scope action acceptable.
- The ledger, the hooks and Claude Code's settings are off-limits to Write/Edit/Bash;
  use the beacon tools. The internal task tools (TodoWrite/TaskCreate/TaskUpdate) are
  disabled during a run: the ledger is the task list. So are scheduled prompts
  (CronCreate/ScheduleWakeup) and starting another `claude` from Bash (use the Agent tool).
- Answering questions, verifying candidates and reopening nodes belong to the human
  (`beacon: …` in this chat, or a terminal). Never run those beacon commands yourself; when a
  candidate needs checking, say what it is and that it waits for `beacon: verify P<n>`.
- Nodes the human added (source `human`) are deliberate leads: give them real attention.
  When the human pauses beacon, nobody pushes you; work normally.
- A message prefixed `[beacon]` in your context is from the supervisor (e.g. "The human
  answered question #3: …"): act on it.
- A question written as prose at the end of a turn is read by a reviewer (Opus): a choice
  is made for you, a request to run something is approved, only information the human alone
  has becomes a question for them. Prefer `ask()`: it answers without ending your turn.
