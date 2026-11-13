# -*- coding: utf-8 -*-
"""beacon MCP server — the worker's only write path into the ledger.

Run (Claude Code starts it from .mcp.json; it inherits BEACON_RUN):
    python3 -m beacon.mcp_server            # stdio transport

Needs the `mcp` package (>= 1.7; 1.x FastMCP and 2.x MCPServer both work), the
same requirement as dualvm.mcp_server. Every tool returns text; a rule
violation comes back as "REFUSED: <the rule>".
"""
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.types import ToolAnnotations
except ImportError as _e1:
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as _Server
        from mcp_types import ToolAnnotations
    except ImportError as _e2:
        raise SystemExit("beacon.mcp_server needs the MCP SDK (mcp >= 1.7): pip install 'mcp>=1.7,<3'\n"
                         f"(1.x: {_e1}; 2.x: {_e2})")

from . import tools

mcp = _Server("beacon")

def _write(title, idempotent=False):
    # Writes only to the ledger (append-only; nothing is ever deleted).
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=False,
                           idempotentHint=idempotent, openWorldHint=False)


@mcp.tool(annotations=ToolAnnotations(title="View ledger", readOnlyHint=True, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=False))
def ledger_view(node_id: str = "") -> str:
    """Show the run's ledger (problems -> solutions -> approaches, status, reasons, assumptions,
    recent evidence) or one node's subtree, plus the next node by the ledger's ordering."""
    return tools.ledger_view(node_id or None)


@mcp.tool(annotations=_write("Add node"))
def add_node(parent: str, title: str, rationale: str = "", kind: str = "") -> str:
    """Record a new solution (hypothesis) under a problem, or an approach under a problem or solution.
    Record every option you think of here instead of offering it to the human. kind: "solution" or
    "approach" (default: solution under a problem, approach under a solution)."""
    return tools.add_node(parent, title, rationale=rationale, kind=kind or None)


@mcp.tool(annotations=_write("Start node"))
def start_node(node_id: str) -> str:
    """Mark a todo solution/approach in_progress before working on it."""
    return tools.start_node(node_id)


@mcp.tool(annotations=_write("Attach evidence"))
def attach_evidence(node_id: str, summary: str, content: str = "", ref: str = "",
                    kind: str = "output") -> str:
    """Record what a step produced. Put the raw output (or the VM spill file's sha256) in `content`:
    evidence is de-duplicated by content hash, so re-attaching the same output does not count as new.
    ref: where the full output lives (e.g. the VM file path). kind: output | file | flag_candidate | note."""
    return tools.attach_evidence(node_id, summary, content=content, ref=ref, kind=kind)


@mcp.tool(annotations=_write("Set status"))
def set_status(node_id: str, status: str, reason: str = "", assumption: str = "",
               superseded_by: str = "", evidence_ids: list[int] | None = None) -> str:
    """Close or block a solution/approach. status: done (needs evidence in its subtree) | dead_end
    (needs reason AND the assumption it rests on: what would have to be false for this to work after
    all?) | superseded (reason) | blocked (reason). A problem can only be marked blocked; it becomes
    done through propose_done."""
    return tools.set_status(node_id, status, reason=reason, assumption=assumption,
                            superseded_by=superseded_by, evidence_ids=evidence_ids or None)


@mcp.tool(annotations=_write("Propose done"))
def propose_done(problem_id: str, candidate: str, evidence_ids: list[int], summary: str = "") -> str:
    """Submit a problem's answer (flag / result) with the evidence ids showing how it was obtained.
    If the checklist gave a verifier, it runs now; otherwise the human verifies. A follow-up problem
    ("Follow-ups of P1", work you listed as unfinished) closes at once when every item in it is done or a
    dead end: candidate = one line on what was done."""
    return tools.propose_done(problem_id, candidate, evidence_ids, summary=summary)


@mcp.tool(annotations=_write("Claim near done"))
def claim_near_done(node_id: str, remaining_steps: list[str]) -> str:
    """Say a node is nearly finished, with the concrete remaining steps. The gate will hold you to
    them; repeated claims without new evidence trigger a review of the assumption behind the claim."""
    return tools.claim_near_done(node_id, remaining_steps)


@mcp.tool(annotations=_write("Ask"))
def ask(kind: str, question: str, node_id: str = "", options: list[str] | None = None, context: str = "") -> str:
    """kind: need_info (something only the human has: a file, a password, a scope decision; the node waits for
    the answer) | need_permission (may I run X / run this on the VM: approved at once by the human's standing
    rule, running a binary included; only a run with ask_before_running_real_samples on holds a request to
    execute a real malware sample for the human, so say "real malware sample" when that is what you ask) |
    approach_choice (choosing between
    approaches: NOT sent to the human — each option becomes an approach node and the decider model
    (Opus) ranks them, reading the ledger and this session; the reply says which to start. List the
    options best first; node_id = the problem or solution they belong to; context = what bears on the
    choice: evidence ids, what each option would show, its cost and risk)."""
    return tools.ask(kind, question, node_id=node_id, options=options or None, context=context)


@mcp.tool(annotations=_write("Propose a problem"))
def propose_problem(title: str, rationale: str, evidence_ids: list[int] | None = None) -> str:
    """A new problem of its own that you found while working (a second crash in another parser, a second
    payload stage, a separate component to analyse) — not a sub-step of an existing problem (use add_node for
    that). rationale: why it is separate and how you found it; evidence_ids: what shows it. It is open at once
    and part of the run (unless the human set problem_approval=human: then it waits for them)."""
    return tools.propose_problem(title, rationale, evidence_ids=evidence_ids or None)


@mcp.tool(annotations=_write("Mark waiting"))
def mark_waiting(node_id: str, job: str, host: str = "kali", max_minutes: int = 30) -> str:
    """Register a running background job (dualvm.jobs) so time spent waiting for it is not counted as
    idle. Poll it with dualvm.jobs.job_poll and attach its output as evidence."""
    return tools.mark_waiting(node_id, job, host=host, max_minutes=max_minutes)


@mcp.tool(annotations=_write("VM check"))
def vm_check(host: str = "kali", node_id: str = "", set_baseline: bool = False) -> str:
    """Fingerprint a VM (running processes, listening ports, a digest of samples/) and compare it with
    the run's baseline (the first check, or set_baseline=true, records the baseline). Run it before
    starting an approach that depends on a clean VM and before declaring a dead end; with node_id the
    result is attached as evidence, so a dead end shows whether the VM was clean when it was reached.
    Read-only on the VM."""
    return tools.vm_check(host=host, node_id=node_id, set_baseline=set_baseline)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
