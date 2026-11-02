"""ChatGPT / any OpenAI-compatible API (OpenAI, OpenRouter, Groq, ...). Same interface as the Gemini chat:
chat.send_message(text) -> object with .text, with the tool-calling loop handled here."""
import json
import logging

import requests

from .llm import QuotaExhausted
from .local_llm import _Reply, _coerce, _schema

log = logging.getLogger("jarvis.openai")


class OpenAIChat:
    def __init__(self, cfg, system: str, tools: list, max_calls: int):
        self.url = cfg.llm.openai_base_url.rstrip("/") + "/chat/completions"
        self.model = cfg.llm.openai_model
        self.key = cfg.openai_key
        self.tools = {t.__name__: t for t in tools}
        self.schemas = [_schema(t) for t in tools]
        self.max_calls = max_calls
        self.messages = [{"role": "system", "content": system}]

    def _post(self, use_tools: bool) -> dict:
        body = {"model": self.model, "messages": self.messages}
        if use_tools and self.schemas:
            body["tools"] = self.schemas
        r = requests.post(self.url, json=body, timeout=120,
                          headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        if r.status_code >= 400:
            if "insufficient_quota" in r.text:
                raise QuotaExhausted("OpenAI credits are used up. Add billing credit at platform.openai.com.")
            raise RuntimeError(f"OpenAI API error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]

    def send_message(self, text: str) -> _Reply:
        self.messages.append({"role": "user", "content": text})
        for step in range(self.max_calls + 1):
            msg = self._post(use_tools=step < self.max_calls)
            calls = msg.get("tool_calls")
            if not calls:
                reply = (msg.get("content") or "").strip()
                self.messages.append({"role": "assistant", "content": reply})
                return _Reply(reply)
            self.messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                tool = self.tools.get(name)
                result = tool(**_coerce(tool, args)) if tool else f"Error: there is no tool called {name}."
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)[:6000]})
        return _Reply("")
