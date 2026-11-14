"""beacon — a supervisor loop around Claude Code (the Brain-side harness).

`labhand` is the Muscle and never interprets. `beacon` is where the interpreting
*bookkeeping* lives: a tamper-evident ledger of problems -> solutions ->
approaches with evidence, a Stop-hook gate that refuses to let the worker quit
while workable nodes remain, and a "lighthouse" reviewer (a fresh-context model
call) that is consulted only when the worker stalls.

Standard library only (sqlite3, urllib) so the hooks run under a plain
`python3` without a virtualenv. The worker-facing MCP server needs the optional
`mcp` package, like `labhand.mcp_server`.

Read beacon/README.md first.
"""
__version__ = "0.1.0"
