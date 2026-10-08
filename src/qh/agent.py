from __future__ import annotations

import json
import os
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .client import LLM, Completion
from . import knowledge
from .config import Config
from .context import ContextManager
from .modes import get_mode, ReasoningMode
from .tools import Toolbox
from .vision import count_images, drop_old_images, load_image_part

SYSTEM_PROMPT = """You are a coding agent working directly in the user's project via tools.

- Be efficient: act, don't narrate. Prefer one tool call that answers the question over several.
- Issue independent tool calls in the same turn (e.g. read several files at once).
- Use grep/find_files to locate code before reading; read only the line ranges you need.
- Use edit_file for changes; use write_file only for new files.
- After changing code, verify it with a command (tests, build, run) before declaring success.
- If a tool errors, read the message and fix the cause; don't repeat the same call.
- For web tasks: use web_search / fetch_url (prefer sites with JSON APIs), download to save files, and
  view_image on small thumbnail URLs to judge content BEFORE downloading large files. Never loop with
  sleep; if a source fails twice, switch source. Don't call bash curl for what these tools do.
- Answer concisely when done. State what changed and anything unresolved.
- Skills are defaults: paths/targets named in the user's request ('this folder', a file) always override paths written in a skill.
- Describe images only from what you actually see in them; don't repeat what you assumed beforehand.
- If you discover a non-obvious, reusable fact (an API quirk, a user preference), call propose_note."""

DIM, RESET = "\033[2m", "\033[0m"


class Agent:
    def __init__(self, cfg: Config, cwd: str, out=sys.stdout):
        self.cfg, self.out = cfg, out
        self.mode: ReasoningMode = get_mode(cfg.mode)
        self.llm = LLM(cfg)
        self.tb = Toolbox(cfg, cwd)
        self.ctx = ContextManager(cfg, self.llm)
        self.tool_schemas = self.tb.schemas()

        mode_sys = f"\n\n---\n{self.mode.system_guidance}"
        full_system = SYSTEM_PROMPT + knowledge.skill_index(self.tb.skills) + mode_sys
        self.messages: list[dict] = [{"role": "system", "content": full_system}]
        self.ctx_info = knowledge.session_context(self.tb.cwd)
        self.env_note = (
            f"[env] cwd={self.tb.cwd} os={platform.system()} shell=bash "
            f"date={time.strftime('%Y-%m-%d')} mode={self.mode.id}"
        )
        self._first = True
        self.stats = {"prompt": 0, "cached": 0, "completion": 0, "steps": 0}

    def set_mode(self, mode_id: str) -> None:
        self.mode = get_mode(mode_id)
        self.cfg.mode = self.mode.id
        mode_sys = f"\n\n---\n{self.mode.system_guidance}"
        full_system = SYSTEM_PROMPT + knowledge.skill_index(self.tb.skills) + mode_sys
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = full_system

    def say(self, s: str) -> None:
        if self.out:
            self.out.write(s)
            self.out.flush()

    def user_turn(self, text: str, images: list[str] | None = None, on_token=None) -> str:
        parts: list[dict] = []
        if self._first:
            pre = "\n\n".join(x for x in (self.env_note, self.ctx_info) if x)
            text, self._first = f"{pre}\n\n{text}", False
        parts.append({"type": "text", "text": text})
        for p in images or []:
            part, note = load_image_part(p, self.cfg)
            parts.append(part)
            self.say(f"{DIM}[image {note}]{RESET}\n")
        self.messages.append({"role": "user", "content": parts if images else text})
        if images:
            drop_old_images(self.messages, self.cfg.max_images_in_context)

        import itertools
        step_iter = range(self.cfg.max_steps) if self.cfg.max_steps > 0 else itertools.count()

        last_text, failed, seen = "", False, {}
        for step in step_iter:
            if self.ctx.maybe_compact(self.messages, log=lambda s: self.say(f"{DIM}{s}{RESET}\n")):
                pass

            think = self._thinking(step, failed)
            t0 = time.time()

            def _handle_content(chunk: str):
                self.say(chunk)
                if on_token:
                    on_token("content", chunk)

            def _handle_reasoning(chunk: str):
                if on_token:
                    on_token("reasoning", chunk)

            res = self.llm.chat(
                self.messages,
                self.tool_schemas,
                thinking=think,
                temperature=self.mode.temperature,
                top_p=self.mode.top_p,
                on_content=_handle_content,
                on_reasoning=_handle_reasoning,
            )
            self._account(res, time.time() - t0, think)
            self.messages.append(res.to_message())
            self.ctx.observe(res.prompt_tokens, len(self.messages) - 1)
            last_text = res.content or last_text
            if res.finish_reason == "length" and not res.tool_calls:
                self.messages.append({"role": "user", "content": "You were cut off. Continue, but be brief."})
                continue
            if not res.tool_calls:
                self.say("\n")
                return res.content
            failed = self._run_tools(res, seen, on_token=on_token)
        if self.cfg.max_steps > 0:
            msg = f"\n[Reached maximum step limit ({self.cfg.max_steps} turns). Stopping loop.]\n"
            self.say(f"{DIM}{msg}{RESET}")
            if on_token:
                on_token("system", msg)
        return last_text

    def _thinking(self, step: int, failed: bool) -> bool:
        if self.cfg.thinking == "on":
            return True
        if self.cfg.thinking == "off":
            return False

        policy = self.mode.thinking_policy
        if policy == "always":
            return True
        elif policy == "off":
            return False
        elif policy == "initial_only":
            return step == 0
        else:  # adaptive
            return step == 0 or failed

    def _run_tools(self, res: Completion, seen: dict, on_token=None) -> bool:
        parsed = []
        for tc in res.tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                err = None
            except Exception as e:
                args, err = {}, f"Error: invalid JSON arguments ({e}). Resend the call with valid JSON."
            parsed.append((tc, name, args, err))

        def one(item):
            tc, name, args, err = item
            if err:
                return err, True
            sig = (name, json.dumps(args, sort_keys=True))
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] >= 3 and name not in ("bash",):
                return "Error: you've made this exact call 3 times. It won't change. Try something different.", True
            return self.tb.run(name, args)

        read_only = all(self.tb.tools.get(n) and self.tb.tools[n].read_only for _, n, _, _ in parsed)
        if len(parsed) > 1 and read_only:
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(one, parsed))
        else:
            results = [one(p) for p in parsed]

        any_failed = False
        for (tc, name, args, _), (out, is_err) in zip(parsed, results):
            any_failed |= is_err
            brief = json.dumps(args, ensure_ascii=False)
            info = f"▸ {name} {brief[:140]}{'…' if len(brief) > 140 else ''}{' ✗' if is_err else ''}"
            self.say(f"{DIM}{info}{RESET}\n")
            if on_token:
                on_token("tool", info + "\n")
            self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})

        if self.tb.pending_images:
            self.messages.append({"role": "user", "content": [
                {"type": "text", "text": "(image from view_image)"}, *self.tb.pending_images]})
            self.tb.pending_images.clear()
            drop_old_images(self.messages, self.cfg.max_images_in_context)
        return any_failed

    def _account(self, res: Completion, dt: float, think: bool) -> None:
        s = self.stats
        s["prompt"] += res.prompt_tokens
        s["cached"] += res.cached_tokens
        s["completion"] += res.completion_tokens
        s["steps"] += 1
        tps = res.completion_tokens / dt if dt > 0 else 0
        self.say(
            f"\n{DIM}[{res.prompt_tokens}p ({res.cached_tokens} cached) + {res.completion_tokens}c "
            f"in {dt:.1f}s = {tps:.0f} tok/s, mode={self.mode.id}, think={'on' if think else 'off'}]{RESET}\n"
        )
