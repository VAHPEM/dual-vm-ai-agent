# -*- coding: utf-8 -*-
"""beacon.board — the visual graph: one self-contained HTML page per run.

    python3 -m beacon graph --run RUN          writes <run>/graph.html and opens it
    beacon: graph                              the same, from the Claude Code chat

No external resources (no CDN, no fonts): it opens offline and in sandboxed previews.
Once graph.html exists, the hooks rewrite it after every change they see (each stop, each
beacon tool call, each chat command), and the page reloads itself every few seconds while
"live" is ticked, so it follows the run. Everything the worker or the lighthouse wrote is
embedded as JSON with `<` escaped and rendered with textContent only (never as HTML).
"""
import json
import os
import time

from .ledger import CLOSED

BOARD = "graph.html"


def board_path(L):
    return os.path.join(L.dir, BOARD)


def board_data(L):
    nodes = L.nodes()
    counts = {r[0]: (r[1], r[2]) for r in L.db.execute(
        "SELECT node_id, COUNT(*), SUM(duplicate_of IS NULL) FROM evidence GROUP BY node_id")}
    recent = {}
    for r in L.db.execute("SELECT id, node_id, summary, ref, duplicate_of FROM evidence ORDER BY id DESC LIMIT 3000"):
        lst = recent.setdefault(r[1], [])
        if len(lst) < 6:
            lst.append({"id": r[0], "summary": r[2][:300], "ref": r[3][:200], "dup": r[4]})
    nxt = L.next_node()
    out_nodes = []
    for n in nodes:
        c = counts.get(n["id"], (0, 0))
        out_nodes.append({
            "id": n["id"], "kind": n["kind"], "parent": n["parent"], "title": n["title"],
            "status": n["status"], "source": n["source"], "priority": n["priority"],
            "rationale": n["rationale"][:1500], "reason": n["reason"][:1500], "assumption": n["assumption"][:1500],
            "candidate": (n["candidate"] or "")[:500], "remaining": json.loads(n["remaining"] or "[]")[:10],
            "near": n["near_done_claims"], "blocked_by": n["blocked_by_question"],
            "ev": c[0], "ev_unique": int(c[1] or 0), "evidence": list(reversed(recent.get(n["id"], []))),
        })
    stops = []
    try:
        from .logview import stop_rows
        for o, d in stop_rows(L.dir)[-8:]:
            stops.append({"ts": o.get("ts", 0), "said": (o.get("text") or "")[:400],
                          "verdict": f"{d.get('action')}:{d.get('why')}" if d else "?",
                          "reply": ((d or {}).get("reason") or (d or {}).get("report") or "")[:600]})
    except Exception:  # noqa: BLE001  -- the graph must render without the logs
        pass
    try:
        from .decider import recent
        decisions = [{"ts": d.get("ts"), "order": d.get("order") or [], "why": (d.get("why") or "")[:600],
                      "question": (d.get("question") or "")[:300], "model": d.get("model"),
                      "first_step": (d.get("first_step") or "")[:300]} for d in recent(L, 5)]
    except Exception:  # noqa: BLE001
        decisions = []
    ok, why = L.verify_integrity()
    g = L.gate_state()
    return {
        "title": L.title(), "dir": L.dir, "generated": time.time(),
        "integrity": {"ok": ok, "why": why}, "paused": bool(L.human_flags().get("paused")),
        "next": nxt["id"] if nxt else None, "nodes": out_nodes,
        "questions": [{"id": q["id"], "kind": q["kind"], "node": q["node_id"], "question": q["question"][:800]}
                      for q in L.questions("open")],
        "gate": {k: g.get(k) for k in ("stops", "forced", "stall", "idle", "lighthouse_calls", "last_decision")},
        "hard": L.config().get("hard_forced"), "stops": stops, "decisions": decisions,
    }


def render_board(L):
    data = json.dumps(board_data(L), ensure_ascii=False).replace("<", "\\u003c")
    return PAGE.replace("/*DATA*/", data, 1)


def write_board(L):
    path = board_path(L)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(render_board(L))
    os.replace(tmp, path)          # a reloading browser never sees half a page
    return path


def refresh_if_open(L):
    """Rewrite graph.html if the human has created it once (hooks call this; never raises)."""
    try:
        if os.path.exists(board_path(L)):
            write_board(L)
    except Exception:  # noqa: BLE001
        pass


CLOSED_JS = json.dumps(list(CLOSED))

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>beacon graph</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1f2328;--mut:#656d76;--line:#d0d7de;--sel:#fff8c5;
--todo:#6e7781;--in_progress:#0969da;--pending_verification:#8250df;--done:#1a7f37;--dead_end:#cf222e;
--blocked:#bc4c00;--superseded:#8c959f;--abandoned:#57606a;--proposed:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--mut:#9198a1;--line:#30363d;
--sel:#3b2e00;--todo:#9198a1;--in_progress:#4493f8;--pending_verification:#ab7df8;--done:#3fb950;
--dead_end:#f85149;--blocked:#db6d28;--superseded:#6e7681;--abandoned:#8b949e;--proposed:#d29922}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 18px}
h1{font-size:17px;margin:0 8px 0 0;display:inline}.mut{color:var(--mut)}.small{font-size:12px}
.badge{display:inline-block;border-radius:10px;padding:0 8px;font-size:12px;border:1px solid var(--line);margin-left:4px}
.warn{background:#cf222e;color:#fff;border-color:#cf222e}.pause{background:#bc4c00;color:#fff;border-color:#bc4c00}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
.chip{cursor:pointer;user-select:none;border:1px solid var(--line);border-radius:14px;padding:2px 10px;background:var(--card);font-size:13px}
.chip.off{opacity:.35;text-decoration:line-through}.chip b{font-variant-numeric:tabular-nums}
.ctl{margin-left:auto;display:flex;gap:12px;align-items:center}input[type=search]{padding:3px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:16px 18px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.prob{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:12px}
.prog{height:6px;border-radius:3px;background:var(--line);overflow:hidden;display:flex;margin:6px 0 4px}
.prog span{display:block;height:100%}
ul.tree{list-style:none;margin:0;padding-left:18px;position:relative}
ul.tree li{position:relative;padding-left:14px}
ul.tree li::before{content:"";position:absolute;left:0;top:0;bottom:0;border-left:1px solid var(--line)}
ul.tree li:last-child::before{bottom:auto;height:13px}
ul.tree li::after{content:"";position:absolute;left:0;top:13px;width:11px;border-top:1px solid var(--line)}
.row{display:flex;gap:6px;align-items:baseline;padding:2px 6px;border-radius:5px;cursor:pointer}
.row:hover{background:color-mix(in srgb,var(--line) 40%,transparent)}.row.sel{background:var(--sel)}
.prob>.row{font-weight:600;font-size:15px;padding-left:0}
.st{white-space:nowrap;font-size:12px;font-weight:600;border-radius:4px;padding:0 5px;color:#fff}
.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--mut);white-space:nowrap}
.closed .ttl{color:var(--mut)}.st-superseded .ttl,.st-abandoned .ttl{text-decoration:line-through}
.st-dead_end .ttl{text-decoration:line-through;text-decoration-color:var(--dead_end)}
.tag{font-size:11px;border:1px solid var(--line);border-radius:8px;padding:0 6px;white-space:nowrap;color:var(--mut)}
.tag.next{border-color:var(--in_progress);color:var(--in_progress);font-weight:600}
.tag.lh{border-color:#bf8700;color:#bf8700}.tag.fu{border-color:#8250df;color:#8250df}.tag.hu{border-color:var(--pending_verification);color:var(--pending_verification)}
aside .box{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:12px}
aside h3{font-size:13px;margin:0 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
dl{margin:0}dt{font-size:12px;color:var(--mut);margin-top:6px}dd{margin:0;white-space:pre-wrap;word-break:break-word}
.stop{border-top:1px solid var(--line);padding:6px 0}.stop:first-of-type{border-top:0}
.q{padding:4px 0;border-top:1px solid var(--line)}.q:first-of-type{border-top:0}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:color-mix(in srgb,var(--line) 45%,transparent);padding:0 4px;border-radius:3px}
.hidden{display:none}
</style></head><body>
<header>
  <h1 id="title"></h1><span id="badges"></span>
  <div class="small mut" id="sub"></div>
  <div class="bar" id="chips"><div class="ctl">
    <input type="search" id="q" placeholder="filter by text or id">
    <label class="small"><input type="checkbox" id="hide"> hide closed</label>
    <label class="small"><input type="checkbox" id="live"> live</label>
  </div></div>
</header>
<main><section id="tree"></section><aside id="side"></aside></main>
<script id="data" type="application/json">/*DATA*/</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const CLOSED = new Set(%CLOSED%);
const ST = {todo:["○","todo"],in_progress:["◐","in progress"],pending_verification:["◎","awaiting verification"],
  done:["✓","done"],dead_end:["✕","dead end"],blocked:["‖","blocked"],superseded:["↷","superseded"],
  abandoned:["–","abandoned"], proposed:["◇","proposed by Claude"]};
const ORDER = Object.keys(ST);
const S = Object.assign({live:"1", hide:"", sel:"", off:"", q:"", y:"0"},
  Object.fromEntries(new URLSearchParams(location.hash.slice(1))));
function save(){ S.y = String(Math.round(scrollY));
  history.replaceState(null, "", "#" + new URLSearchParams(S).toString()); }
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text); return e; }
function color(st){ return "var(--" + (ST[st] ? st : "todo") + ")"; }
const byId = Object.fromEntries(D.nodes.map(n => [n.id, n]));
const kids = {}; D.nodes.forEach(n => { if (n.parent) (kids[n.parent] = kids[n.parent] || []).push(n); });
function sub(id){ const out = []; (kids[id] || []).forEach(k => { out.push(k); out.push(...sub(k.id)); }); return out; }
const off = new Set(S.off ? S.off.split(",") : []);

function header(){
  document.getElementById("title").textContent = D.title || "beacon";
  const b = document.getElementById("badges");
  if (!D.integrity.ok) b.append(el("span", "badge warn", "LEDGER INTEGRITY FAILED: " + D.integrity.why));
  if (D.paused) b.append(el("span", "badge pause", "paused"));
  const probs = D.nodes.filter(n => n.kind === "problem");
  const g = D.gate;
  document.getElementById("sub").textContent =
    probs.filter(p => p.status === "done").length + "/" + probs.length + " problems done · gate: " + g.stops +
    " stops, " + g.forced + "/" + D.hard + " forced, lighthouse " + g.lighthouse_calls + " calls · last: " +
    (g.last_decision || "-") + " · updated " + new Date(D.generated * 1000).toLocaleTimeString() + " · " + D.dir;
  const counts = {}; D.nodes.forEach(n => counts[n.status] = (counts[n.status] || 0) + 1);
  const bar = document.getElementById("chips"), ctl = bar.querySelector(".ctl");
  ORDER.filter(s => counts[s]).forEach(s => {
    const c = el("span", "chip" + (off.has(s) ? " off" : ""));
    const dot = el("span", null, ST[s][0] + " "); dot.style.color = color(s);
    c.append(dot, el("span", null, ST[s][1] + " "), el("b", null, counts[s]));
    c.title = "click to show/hide";
    c.onclick = () => { off.has(s) ? off.delete(s) : off.add(s); S.off = [...off].join(","); save(); render(); };
    bar.insertBefore(c, ctl);
  });
}

function visible(n){
  if (off.has(n.status)) return false;
  if (S.hide && CLOSED.has(n.status) && n.kind !== "problem") return false;
  if (S.q){ const q = S.q.toLowerCase(); const hit = m => (m.id + " " + m.title).toLowerCase().includes(q);
    if (!hit(n) && !sub(n.id).some(hit)) return false; }
  return true;
}

function row(n){
  const r = el("div", "row st-" + n.status + (CLOSED.has(n.status) ? " closed" : "") + (S.sel === n.id ? " sel" : ""));
  const st = el("span", "st", ST[n.status] ? ST[n.status][0] + " " + ST[n.status][1] : n.status);
  st.style.background = color(n.status);
  r.append(st, el("span", "id", n.id), el("span", "ttl", n.title));
  if (D.next === n.id) r.append(el("span", "tag next", "▶ next"));
  if (n.source === "lighthouse") r.append(el("span", "tag lh", "✦ lighthouse"));
  if (n.source === "human") r.append(el("span", "tag hu", "● you"));
  if (n.source === "followup") r.append(el("span", "tag fu", "↻ left unfinished"));
  if (n.priority >= 100 && !CLOSED.has(n.status)) r.append(el("span", "tag hu", "★ your pick"));
  if (D.decisions.some(d => d.order[0] === n.id)) r.append(el("span", "tag lh", "⚖ decider's choice"));
  if (n.ev) r.append(el("span", "tag", "evidence " + n.ev));
  if (n.near) r.append(el("span", "tag", "near-done ×" + n.near));
  if (n.blocked_by) r.append(el("span", "tag", "waits on question #" + n.blocked_by));
  if (n.candidate) r.append(el("span", "tag hu", "candidate"));
  r.onclick = ev => { ev.stopPropagation(); S.sel = S.sel === n.id ? "" : n.id; save(); render(); };
  return r;
}

function branch(id){
  const list = (kids[id] || []).filter(visible);
  if (!list.length) return null;
  const ul = el("ul", "tree");
  list.forEach(k => { const li = el("li"); li.append(row(k)); const b = branch(k.id); if (b) li.append(b); ul.append(li); });
  return ul;
}

function tree(){
  const t = document.getElementById("tree"); t.replaceChildren();
  D.nodes.filter(n => n.kind === "problem" && visible(n)).forEach(p => {
    const card = el("div", "prob"); card.append(row(p));
    const all = sub(p.id);
    if (all.length){
      const prog = el("div", "prog");
      ORDER.forEach(s => { const k = all.filter(n => n.status === s).length;
        if (k){ const sp = el("span"); sp.style.width = (100 * k / all.length) + "%"; sp.style.background = color(s);
          sp.title = k + " " + ST[s][1]; prog.append(sp); } });
      const closed = all.filter(n => CLOSED.has(n.status)).length;
      card.append(prog, el("div", "small mut", closed + "/" + all.length + " closed · " +
        all.filter(n => n.status === "in_progress").length + " in progress · " +
        all.filter(n => n.status === "todo").length + " todo"));
    } else card.append(el("div", "small mut", "no hypotheses yet"));
    const b = branch(p.id); if (b) card.append(b);
    t.append(card);
  });
  if (!t.children.length) t.append(el("p", "mut", "Nothing matches the filters."));
}

function details(n){
  const box = el("div", "box"); box.append(el("h3", null, "Node " + n.id));
  const dl = el("dl");
  const add = (k, v) => { if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) return;
    dl.append(el("dt", null, k), el("dd", null, Array.isArray(v) ? v.join("\n") : v)); };
  add("title", n.title); add("status", ST[n.status] ? ST[n.status][1] : n.status);
  add("kind / added by", n.kind + " / " + n.source); add("why", n.rationale); add("result", n.reason);
  add("assumes", n.assumption); add("candidate", n.candidate);
  if (n.near) add("remaining steps (near-done ×" + n.near + ")", n.remaining);
  if (n.blocked_by) add("waits on", "question #" + n.blocked_by + " — answer in the chat: #" + n.blocked_by + " <answer>");
  add("evidence", n.ev ? n.ev + " (" + n.ev_unique + " unique)" : "");
  add("latest evidence", n.evidence.map(e => "#" + e.id + " " + e.summary + (e.ref ? " @ " + e.ref : "") +
    (e.dup ? " (duplicate of #" + e.dup + ")" : "")));
  add("children", (kids[n.id] || []).map(k => k.id + " [" + (ST[k.status] ? ST[k.status][1] : k.status) + "] " + k.title));
  box.append(dl);
  const hint = el("div", "small mut");
  hint.append("In the chat: ", el("code", null, "beacon: show " + n.id));
  if (CLOSED.has(n.status)) hint.append(" · ", el("code", null, "beacon: reopen " + n.id));
  else hint.append(" · ", el("code", null, "beacon: add " + n.id + " <idea>"));
  if (n.status === "pending_verification") hint.append(" · ", el("code", null, "beacon: verify " + n.id));
  box.append(hint);
  return box;
}

function side(){
  const s = document.getElementById("side"); s.replaceChildren();
  if (S.sel && byId[S.sel]) s.append(details(byId[S.sel]));
  const pend = D.nodes.filter(n => n.status === "pending_verification");
  const prop = D.nodes.filter(n => n.status === "proposed");
  const w = el("div", "box"); w.append(el("h3", null, "Waiting on you"));
  if (!pend.length && !D.questions.length && !prop.length) w.append(el("div", "small mut", "Nothing."));
  prop.forEach(p => { const d = el("div", "q"); d.append(el("b", null, p.id + " proposed by Claude: "), el("span", null, p.title),
    el("div", "small mut", "beacon: accept " + p.id + "   |   beacon: reject " + p.id + " <why>")); w.append(d); });
  pend.forEach(p => { const d = el("div", "q"); d.append(el("b", null, p.id + " candidate: "), el("span", null, p.candidate),
    el("div", "small mut", "beacon: verify " + p.id + "   |   beacon: verify " + p.id + " reject <why>")); w.append(d); });
  D.questions.forEach(q => { const d = el("div", "q"); d.append(el("b", null, "#" + q.id + " "),
    el("span", null, q.question), el("div", "small mut", q.kind + (q.node ? ", " + q.node : "") + " — reply: #" + q.id + " <answer>"));
    w.append(d); });
  s.append(w);
  if (D.decisions.length){
    const dc = el("div", "box"); dc.append(el("h3", null, "Branch decisions (decider)"));
    D.decisions.forEach(x => { const d = el("div", "stop");
      d.append(el("div", "small mut", new Date(x.ts * 1000).toLocaleTimeString() + " · " + (x.model || "") + " · " + x.question));
      d.append(el("div", "small", x.order.join("  >  ")));
      d.append(el("div", "small mut", x.why));
      dc.append(d); });
    s.append(dc);
  }
  const h = el("div", "box"); h.append(el("h3", null, "Recent stops (newest first)"));
  if (!D.stops.length) h.append(el("div", "small mut", "No stops yet."));
  D.stops.slice().reverse().forEach(x => { const d = el("div", "stop");
    d.append(el("div", "small mut", new Date(x.ts * 1000).toLocaleTimeString() + " · beacon " + x.verdict));
    d.append(el("div", "small", "Claude: " + (x.said || "(no text)")));
    if (x.reply) d.append(el("div", "small mut", "beacon: " + x.reply));
    h.append(d); });
  s.append(h);
  const lg = el("div", "box small mut");
  lg.append("Statuses: ○ todo · ◐ in progress · ◎ awaiting your verification · ✓ done · ✕ dead end (with the assumption " +
    "it rests on) · ‖ blocked · ↷ superseded · – abandoned. ✦ = proposed by the lighthouse, ● = added by you, ↻ = work Claude listed as unfinished, " +
    "★ = your pick, ⚖ = the decider's choice at a branch. " +
    "Click a node for details. `beacon: help` in the chat lists the commands.");
  s.append(lg);
}

function render(){ tree(); side(); }
header(); render();
const q = document.getElementById("q"); q.value = S.q;
q.oninput = () => { S.q = q.value; save(); render(); };
const hide = document.getElementById("hide"); hide.checked = !!S.hide;
hide.onchange = () => { S.hide = hide.checked ? "1" : ""; save(); render(); };
const live = document.getElementById("live"); live.checked = !!S.live;
live.onchange = () => { S.live = live.checked ? "1" : ""; save(); };
addEventListener("scroll", () => { clearTimeout(window._st); window._st = setTimeout(save, 200); });
scrollTo(0, +S.y || 0);
setInterval(() => { if (S.live && document.activeElement !== q){ save(); location.reload(); } }, 5000);
</script></body></html>
""".replace("%CLOSED%", CLOSED_JS)
