"""On-device chat via Ollama (e.g. Nemotron 3 Nano). Mimics the bit of the Gemini chat API that Jarvis uses:
chat.send_message(text) -> object with .text, running the tool-calling loop itself."""
import inspect
import json
import logging
import re
import time
import typing

import requests

log = logging.getLogger("jarvis.local")
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


class _Reply:
    def __init__(self, text: str):
        self.text = text


def _schema(fn) -> dict:
    sig, hints = inspect.signature(fn), typing.get_type_hints(fn)
    props, required = {}, []
    for name, p in sig.parameters.items():
        props[name] = {"type": _JSON_TYPES.get(hints.get(name, str), "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "function", "function": {
        "name": fn.__name__, "description": (inspect.getdoc(fn) or "")[:700],
        "parameters": {"type": "object", "properties": props, "required": required}}}


def _coerce(fn, args: dict) -> dict:
    """Small models often send numbers as strings or objects as dicts; fix the types."""
    hints, sig, out = typing.get_type_hints(fn), inspect.signature(fn), {}
    for key, val in (args or {}).items():
        if key not in sig.parameters:
            continue
        t = hints.get(key, str)
        try:
            if t is int:
                val = int(float(val))
            elif t is float:
                val = float(val)
            elif t is bool:
                val = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
            elif t is str and not isinstance(val, str):
                val = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
        except (TypeError, ValueError):
            pass
        out[key] = val
    return out


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return re.sub(r"^.*?</think>", "", text, flags=re.S).strip()


def _text_tool_calls(content: str) -> list:
    """Some small models print tool calls as text instead of using the structured field."""
    calls = []
    try:
        m = re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", content or "", re.S)
        if m:
            calls += [{"function": {"name": c["name"], "arguments": c.get("arguments", {})}} for c in json.loads(m.group(1))]
        for m in re.finditer(r"<tool_call>(.*?)</tool_call>", content or "", re.S):
            c = json.loads(m.group(1))
            calls.append({"function": {"name": c["name"], "arguments": c.get("arguments", {})}})
    except (ValueError, KeyError, TypeError):
        return []
    return calls


class LocalChat:
    def __init__(self, cfg, system: str, tools: list, max_calls: int):
        self.url = cfg.llm.ollama_url.rstrip("/") + "/api/chat"
        self.model = cfg.llm.ollama_model
        self.num_ctx = int(cfg.llm.ollama_context)
        self.think = bool(cfg.llm.ollama_think)
        self.keep_alive = getattr(cfg.llm, "ollama_keep_alive", "5m")      # unload the model soon after use to free memory
        self.threads = int(getattr(cfg.llm, "ollama_threads", 4))
        self.tools = {t.__name__: t for t in tools}
        self.schemas = [_schema(t) for t in tools]
        self.max_calls = max_calls
        self.messages = [{"role": "system", "content": system}]

    def _post(self, use_tools: bool) -> dict:
        body = {"model": self.model, "messages": self.messages, "stream": False, "keep_alive": self.keep_alive,
                "options": {"num_ctx": self.num_ctx, "temperature": 0.3, "num_thread": self.threads}}
        if use_tools and self.schemas:
            body["tools"] = self.schemas
        if self.think is not None:
            body["think"] = self.think
        try:
            r = requests.post(self.url, json=body, timeout=900)
            if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
                self.think = None            # this model has no thinking switch
                body.pop("think")
                r = requests.post(self.url, json=body, timeout=900)
        except requests.ConnectionError:
            raise RuntimeError("Ollama is not running. Open the Ollama app (or run 'ollama serve').")
        if r.status_code == 404:
            raise RuntimeError(f"Model '{self.model}' is not installed. Run: ollama pull {self.model}")
        r.raise_for_status()
        return r.json()["message"]

    def _trim(self) -> None:
        """Keep the conversation inside the context window by shrinking old tool output."""
        if sum(len(str(m.get("content", ""))) for m in self.messages) < self.num_ctx * 2.5:
            return
        for m in self.messages[1:-6]:
            if m["role"] == "tool" and len(m["content"]) > 200:
                m["content"] = m["content"][:150] + " ...[trimmed]"

    def send_message(self, text: str) -> _Reply:
        self.messages.append({"role": "user", "content": text})
        for step in range(self.max_calls + 1):
            self._trim()
            msg = self._post(use_tools=step < self.max_calls)
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or _text_tool_calls(content)
            if not calls:
                reply = _strip_think(content)
                self.messages.append({"role": "assistant", "content": reply})
                return _Reply(reply)
            self.messages.append({"role": "assistant", "content": "" if not msg.get("tool_calls") else content,
                                  "tool_calls": calls})
            for call in calls:
                name, args = call["function"]["name"], call["function"].get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                tool = self.tools.get(name)
                result = tool(**_coerce(tool, args)) if tool else f"Error: there is no tool called {name}."
                self.messages.append({"role": "tool", "tool_name": name, "content": str(result)[:6000]})
        return _Reply("")


def self_test(cfg) -> None:
    """./jarvisctl.sh local  -> checks Ollama, the model, and that it can call a tool."""
    from .tools import REGISTRY

    tools = [t for t in REGISTRY if t.__name__ in ("get_current_time", "system_status")]
    chat = LocalChat(cfg, "You are a concise assistant. Use your tools when they help.", tools, 4)
    t0 = time.time()
    try:
        reply = chat.send_message("What time is it right now? Use your tool to find out.")
    except RuntimeError as exc:
        print(f"\nLocal model check failed: {exc}")
        return
    used = any(m["role"] == "tool" for m in chat.messages)
    print(f"Reply: {reply.text}\nTool calling worked: {used}\nTook {time.time() - t0:.1f}s on {cfg.llm.ollama_model}")
