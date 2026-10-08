"""Minimal streaming client for vLLM's OpenAI-compatible chat endpoint."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .config import Config

REASONING_KEYS = ("reasoning_content", "reasoning")


@dataclass
class Completion:
    content: str = ""
    reasoning: str = ""
    reasoning_key: str = "reasoning_content"
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    finish_reason: str = ""

    def to_message(self) -> dict:
        """Assistant message exactly as the server produced it."""
        m: dict = {"role": "assistant", "content": self.content}
        if self.reasoning:
            m[self.reasoning_key] = self.reasoning
        if self.tool_calls:
            m["tool_calls"] = self.tool_calls
        return m


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = httpx.Client(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10),
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        thinking: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        on_content: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> Completion:
        temp = temperature if temperature is not None else (0.6 if thinking else 0.7)
        tp = top_p if top_p is not None else (0.95 if thinking else 0.8)

        body = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking},
            "temperature": temp,
            "top_p": tp,
            "top_k": 20,
        }
        if not thinking:
            body["presence_penalty"] = 1.0

        if tools:
            body["tools"] = tools
            body["parallel_tool_calls"] = True

        out = Completion()
        calls: dict[int, dict] = {}
        with self.http.stream("POST", "/chat/completions", json=body) as r:
            if r.status_code != 200:
                raise RuntimeError(f"vLLM {r.status_code}: {r.read().decode()[:2000]}")
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if u := chunk.get("usage"):
                    out.prompt_tokens = u.get("prompt_tokens", 0)
                    out.completion_tokens = u.get("completion_tokens", 0)
                    out.cached_tokens = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                for ch in chunk.get("choices", []):
                    d = ch.get("delta", {})
                    for k in REASONING_KEYS:
                        if d.get(k):
                            out.reasoning_key = k
                            out.reasoning += d[k]
                            on_reasoning and on_reasoning(d[k])
                    if d.get("content"):
                        out.content += d["content"]
                        on_content and on_content(d["content"])
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(
                            tc.get("index", 0),
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["function"]["name"] += fn.get("name") or ""
                        c["function"]["arguments"] += fn.get("arguments") or ""
                    if ch.get("finish_reason"):
                        out.finish_reason = ch["finish_reason"]
        out.tool_calls = [calls[i] for i in sorted(calls)]
        for i, c in enumerate(out.tool_calls):
            c["id"] = c["id"] or f"call_{i}"
        return out
