# -*- coding: utf-8 -*-
"""A scripted stand-in for the Messages API, so the REAL Claude Code CLI can be run
end to end (hooks, MCP server, Stop gate, watchdog) without a model or an account.

Point Claude Code at it with ANTHROPIC_BASE_URL=http://127.0.0.1:<port> and any
ANTHROPIC_API_KEY. Main-loop requests (they carry `tools`) get the next scripted
step; the step number is recovered from markers in the conversation itself
("[step N]" in assistant text, tool_use ids "toolu_sN"), so retries and side
requests do not desynchronise the script. Requests from beacon's own classifier,
lighthouse and decider (recognised by their system prompts) get canned JSON. Everything else
(titles, summaries, quota probes) gets a short text.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockAPI:
    def __init__(self, steps, classifier_reply=None, lighthouse_reply=None, fail_main=None, decider_reply=None,
                 followup_reply=None, sample_reply=None):
        self.steps = steps
        self.followup_reply = followup_reply or {"items": []}
        self.sample_reply = sample_reply or {"real_sample": False, "why": "mock: a CTF binary"}
        self.classifier_reply = classifier_reply or {"kind": "rhetorical", "question": "continue?",
                                                     "confidence": 0.95}
        self.lighthouse_reply = lighthouse_reply or {"suggestions": [], "notes": "nothing new"}
        self.decider_reply = decider_reply or {"ranking": [2, 1], "why": "mock decision", "first_step": "echo",
                                               "switch_if": "never", "new_option": None, "confidence": 0.5}
        self.fail_main = fail_main          # (status, error_type, message) -> every main request fails
        self.requests = []                  # (path, headers, body) of everything received
        self.main = []                      # bodies of main-loop requests
        self.lock = threading.Lock()
        self.server = None

    # ------------------------------------------------------------ script
    @staticmethod
    def progress(messages):
        n = -1
        for m in messages or []:
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
            for b in blocks:
                if b.get("type") == "text":
                    for x in re.findall(r"\[step (\d+)\]", b.get("text", "")):
                        n = max(n, int(x))
                elif b.get("type") == "tool_use":
                    mm = re.match(r"toolu_s(\d+)", b.get("id", ""))
                    if mm:
                        n = max(n, int(mm.group(1)))
        return n + 1

    def step_message(self, body):
        idx = self.progress(body.get("messages"))
        if idx < len(self.steps):
            step = self.steps[idx]
        else:
            step = {"text": "Finished."}
        step = step(body) if callable(step) else step
        content = [{"type": "text", "text": f"[step {idx}] {step.get('text', '')}".strip()}]
        if step.get("tool"):
            content.append({"type": "tool_use", "id": f"toolu_s{idx}", "name": step["tool"]["name"],
                            "input": step["tool"].get("input", {})})
        return content, ("tool_use" if step.get("tool") else "end_turn")

    # ------------------------------------------------------------ server
    def start(self):
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send_json(self, code, obj, extra=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with api.lock:
                    api.requests.append(("GET " + self.path, dict(self.headers), {}))
                self._send_json(200, {"data": [], "has_more": False})

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                with api.lock:
                    api.requests.append((self.path, dict(self.headers), body))
                if "count_tokens" in self.path:
                    return self._send_json(200, {"input_tokens": 100})
                if "/v1/messages" not in self.path:
                    return self._send_json(404, {"type": "error", "error": {"type": "not_found_error",
                                                                            "message": "mock"}})
                system = json.dumps(body.get("system", ""))
                if "You classify the question" in system:
                    return self._reply(body, [{"type": "text", "text": json.dumps(api.classifier_reply)}], "end_turn")
                if "You are the decider for an AI analysis agent" in system:
                    text = "<reasoning>mock</reasoning>\n```json\n" + json.dumps(api.decider_reply) + "\n```"
                    return self._reply(body, [{"type": "text", "text": text}], "end_turn")
                if "pull out the work it says is NOT done" in system:
                    return self._reply(body, [{"type": "text", "text": json.dumps(api.followup_reply)}], "end_turn")
                if "Decide whether carrying it out would EXECUTE a real malware sample" in system:
                    return self._reply(body, [{"type": "text", "text": json.dumps(api.sample_reply)}], "end_turn")
                if "lighthouse reviewer" in system:
                    return self._reply(body, [{"type": "text", "text": json.dumps(api.lighthouse_reply)}], "end_turn")
                if body.get("tools"):
                    with api.lock:
                        api.main.append(body)
                    if api.fail_main:
                        code, etype, msg = api.fail_main
                        return self._send_json(code, {"type": "error", "error": {"type": etype, "message": msg}},
                                               {"x-should-retry": "false", "retry-after": "0"})
                    content, stop = api.step_message(body)
                    return self._reply(body, content, stop)
                return self._reply(body, [{"type": "text", "text": "ok"}], "end_turn")

            def _reply(self, body, content, stop):
                model = body.get("model", "mock-model")
                usage = {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0}
                if not body.get("stream"):
                    return self._send_json(200, {"id": "msg_mock", "type": "message", "role": "assistant",
                                                 "model": model, "content": content, "stop_reason": stop,
                                                 "stop_sequence": None, "usage": usage})
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()

                def ev(name, obj):
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())

                ev("message_start", {"type": "message_start", "message": {
                    "id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": dict(usage, output_tokens=1)}})
                for i, b in enumerate(content):
                    if b["type"] == "text":
                        ev("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "text", "text": ""}})
                        ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "text_delta", "text": b["text"]}})
                    else:
                        ev("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "tool_use", "id": b["id"],
                                                                     "name": b["name"], "input": {}}})
                        ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "input_json_delta",
                                                             "partial_json": json.dumps(b["input"])}})
                    ev("content_block_stop", {"type": "content_block_stop", "index": i})
                ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 20}})
                ev("message_stop", {"type": "message_stop"})
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    # ------------------------------------------------------------ helpers for assertions
    def main_text(self, i):
        return json.dumps(self.main[i]) if i < len(self.main) else ""

    def all_main_text(self):
        return "\n".join(json.dumps(b) for b in self.main)
