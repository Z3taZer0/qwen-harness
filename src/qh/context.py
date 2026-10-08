"""Cache-aware context management.

Why this exists: vLLM's prefix cache only helps while the *start* of the prompt is
byte-identical between requests. Any edit to history invalidates everything after the
edit point, so we edit rarely and in big hops instead of trimming a little every step:

  1. Nothing is touched until the prompt reaches `compact_at` (default 70% of window).
  2. Stage 1 (free, no LLM call): elide old tool outputs / images.
  3. Stage 2 (only if stage 1 wasn't enough): summarize the old part with the model.
  4. We aim for `compact_target` (40%), so there is a long runway before the next
     compaction -> no more "compacts 3 times in a row".
"""
from __future__ import annotations

import json

from .client import LLM
from .config import Config
from .vision import drop_old_images

SUMMARY_PROMPT = """You are compacting the history of a coding agent session so work can continue.
Write a dense, factual summary with these sections:
## Goal  (what the user asked for, verbatim where it matters)
## Done  (concrete actions and results, files created/edited with paths)
## State (current file contents/structure facts, commands that work, errors seen)
## Next  (the immediate next steps)
Keep exact paths, identifiers, commands and error messages. No filler. Under 1200 words."""


def _msg_chars(m: dict) -> int:
    c = m.get("content")
    n = len(c) if isinstance(c, str) else sum(len(p.get("text", "")) + (1200 * 3 if p.get("type") == "image_url" else 0) for p in c or [])
    for tc in m.get("tool_calls") or []:
        n += len(tc["function"]["arguments"]) + 20
    n += len(m.get("reasoning_content") or m.get("reasoning") or "")
    return n


class ContextManager:
    def __init__(self, cfg: Config, llm: LLM):
        self.cfg, self.llm = cfg, llm
        self.real_tokens = 0     # prompt_tokens reported by the server at last call
        self.real_len = 0        # len(messages) at that call
        self.compactions = 0

    def observe(self, prompt_tokens: int, n_messages: int) -> None:
        self.real_tokens, self.real_len = prompt_tokens, n_messages

    def estimate(self, messages: list[dict]) -> int:
        """Real server count for the known prefix + char heuristic for newer messages."""
        extra = sum(_msg_chars(m) for m in messages[self.real_len:])
        return self.real_tokens + int(extra / self.cfg.chars_per_token) if self.real_len else int(
            sum(_msg_chars(m) for m in messages) / self.cfg.chars_per_token)

    # ------------------------------------------------------------------ public
    def maybe_compact(self, messages: list[dict], tools_tokens: int = 0, log=print, force: bool = False) -> bool:
        limit = int(self.cfg.context_window * self.cfg.compact_at)
        if not force and self.estimate(messages) < limit:
            return False
        before = self.estimate(messages)
        target = int(self.cfg.context_window * self.cfg.compact_target)

        self._elide(messages)
        est = self.estimate_after_edit(messages, before)
        if est > target or force:
            self._summarize(messages)
            est = self.estimate_after_edit(messages, before)
        self.real_len = 0  # force heuristic until next server usage report
        self.real_tokens = 0
        self.compactions += 1
        log(f"[compacted: ~{before} -> ~{est} tokens]")
        return True

    # ----------------------------------------------------------------- helpers
    def estimate_after_edit(self, messages: list[dict], _before: int) -> int:
        return int(sum(_msg_chars(m) for m in messages) / self.cfg.chars_per_token)

    def _elide(self, messages: list[dict]) -> None:
        drop_old_images(messages, keep=1)
        tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
        for i in tool_idx[: max(0, len(tool_idx) - self.cfg.keep_recent_tool_results)]:
            c = messages[i]["content"]
            if isinstance(c, str) and len(c) > 300:
                messages[i]["content"] = f"[old tool output elided ({len(c)} chars); re-run the tool if you need it]"
        # Old reasoning is the biggest hidden cost; it is never needed once the step is done.
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=0)
        for m in messages[:last_user]:
            m.pop("reasoning_content", None)
            m.pop("reasoning", None)

    def _split_point(self, messages: list[dict]) -> int:
        """Index where the verbatim tail starts. Must be a clean boundary (never between
        an assistant tool_call and its tool results)."""
        budget = int(self.cfg.context_window * self.cfg.tail_fraction * self.cfg.chars_per_token)
        acc, idx = 0, len(messages)
        for i in range(len(messages) - 1, 0, -1):
            acc += _msg_chars(messages[i])
            if acc > budget:
                break
            idx = i
        while idx < len(messages) and messages[idx]["role"] == "tool":
            idx -= 1  # pull the owning assistant message into the tail
        return max(idx, 2)

    def _summarize(self, messages: list[dict]) -> None:
        split = self._split_point(messages)
        head = messages[1:split]  # messages[0] is the static system prompt
        if not head:
            return
        transcript = []
        for m in head:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "[image]") for p in c)
            line = f"{m['role'].upper()}: {c or ''}"
            for tc in m.get("tool_calls") or []:
                line += f"\n  -> {tc['function']['name']}({tc['function']['arguments'][:400]})"
            transcript.append(line)
        res = self.llm.chat(
            [{"role": "system", "content": SUMMARY_PROMPT},
             {"role": "user", "content": "\n\n".join(transcript)}],
            thinking=False, max_tokens=2500,
        )
        summary = {"role": "user", "content": f"[Summary of earlier work in this session]\n{res.content.strip()}"}
        ack = {"role": "assistant", "content": "Understood. Continuing from that summary."}
        messages[1:split] = [summary, ack]
