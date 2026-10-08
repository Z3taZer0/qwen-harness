"""The agent loop. Frontend-agnostic: everything user-visible is reported through
`on_event(kind, data)` so the CLI and the GTK app render the same stream.

Events
  step       {step, think}                         a model call starts
  reasoning  {text}                                streamed thought chunk
  content    {text}                                streamed answer chunk
  tool_start {id, name, args}
  tool_end   {id, name, args, output, error, dt}
  usage      {prompt, cached, completion, dt, tps, think, ctx}
  notice     {text}                                compaction, limits, mode switches...
  cancelled  {}
"""
from __future__ import annotations

import itertools
import json
import re
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import knowledge, sessions
from .client import LLM, Cancelled, Completion
from .config import Config
from .context import ContextManager
from .modes import ReasoningMode, get_mode
from .tools import Toolbox
from .vision import drop_old_images, image_in_context, load_image_part

SYSTEM_PROMPT = """You are a hands-on assistant operating the user's Linux machine and projects via tools.

- Be efficient: act, don't narrate. Prefer one tool call that answers the question over several.
- Issue independent tool calls in the same turn (e.g. read several files at once).
- Use grep/find_files to locate code before reading; read only the line ranges you need.
- Use edit_file for changes; use write_file only for new files.
- After changing code, verify it with a command (tests, build, run) before declaring success.
- If a tool errors, read the message and fix the cause; don't repeat the same call.
- For web tasks: use web_search / fetch_url (prefer sites with JSON APIs), download to save files, and
  view_image on small thumbnail URLs to judge content BEFORE downloading large files. Never loop with
  sleep; if a source fails twice, switch source. Don't call bash curl for what these tools do.
- To change the wallpaper: download the chosen image, then call set_wallpaper on it (it files it into
  the wallpaper folder and applies it). Don't hunt for other ways unless set_wallpaper fails.
- Answer concisely when done. State what changed and anything unresolved. Markdown is rendered.
- Skills are defaults: paths/targets named in the user's request ('this folder', a file) always override paths written in a skill.
- Describe images only from what you actually see in them; don't repeat what you assumed beforehand.
- Viewed images stay visible in the conversation, each labeled [image: path]. Compare candidates by
  those labels; only view_image again if an image was removed to save context.
- To choose between several images, call contact_sheet once with all candidates (one numbered grid),
  pick from it, then act. Judge each image once; don't re-inspect images you already judged.
- If you discover a non-obvious, reusable fact (an API quirk, a user preference), call propose_note."""

EventFn = Callable[[str, dict], None]


def system_prompt() -> str:
    """~/.config/qh/system.md replaces the built-in prompt entirely when present."""
    custom = knowledge.CFG_DIR / "system.md"
    try:
        text = custom.read_text().strip()
    except OSError:
        text = ""
    return text or SYSTEM_PROMPT


class Agent:
    def __init__(self, cfg: Config, cwd: str, on_event: EventFn | None = None):
        self.cfg = cfg
        self.on_event: EventFn = on_event or (lambda kind, data: None)
        self.mode: ReasoningMode = get_mode(cfg.mode)
        self.llm = LLM(cfg)
        self.tb = Toolbox(cfg, cwd)
        self.ctx = ContextManager(cfg, self.llm)
        self.tool_schemas = self.tb.schemas()
        self.messages: list[dict] = [{"role": "system", "content": self._system()}]
        self._first = True
        self._pending: list[str] = []  # notes prepended to the next user message
        self._stop = threading.Event()
        self.busy = False
        self.stats = {"prompt": 0, "cached": 0, "completion": 0, "steps": 0}
        self.session_id = sessions.new_id()
        self.title = ""
        self.created = time.time()

    # ------------------------------------------------------------------ setup
    def _system(self) -> str:
        return system_prompt() + knowledge.skill_index(self.tb.skills) + f"\n\n---\n{self.mode.system_guidance}"

    def _env_note(self) -> str:
        return (f"[env] cwd={self.tb.cwd} os={platform.system()} shell=bash "
                f"date={time.strftime('%Y-%m-%d %H:%M')} mode={self.mode.id} "
                f"wallpapers={self.cfg.wallpaper_dir}")

    @property
    def started(self) -> bool:
        return any(m["role"] != "system" for m in self.messages)

    def set_mode(self, mode_id: str) -> None:
        """Switching mode mid-conversation must not rewrite the system prompt (that would
        throw away the whole prefix cache), so the new guidance rides on the next user turn."""
        new = get_mode(mode_id)
        if new.id == self.mode.id:
            return
        self.mode = new
        self.cfg.mode = new.id
        if not self.started:
            self.messages[0]["content"] = self._system()
        else:
            self._pending = [p for p in self._pending if not p.startswith("[mode")]
            self._pending.append(f"[mode switched to {new.id}]\n{new.system_guidance}")

    def set_cwd(self, path: str) -> None:
        p = Path(os.path.expanduser(path)).resolve()
        if not p.is_dir():
            raise ValueError(f"not a directory: {p}")
        self.tb.cwd = p
        if self.started:
            self._pending = [x for x in self._pending if not x.startswith("[cwd")]
            self._pending.append(f"[cwd changed to {p}]")

    def emit(self, kind: str, **data) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass  # a rendering bug must never break the agent loop

    def cancel(self) -> None:
        """Stop the current turn from any thread: aborts streaming and running commands."""
        self._stop.set()
        self.tb.stop.set()
        self.llm.cancel()

    # ------------------------------------------------------------------- turn
    def user_turn(self, text: str, images: list[str] | None = None) -> str:
        self._stop.clear()
        self.tb.stop.clear()
        self.llm.reset()
        self.busy = True
        try:
            return self._turn(text, images or [])
        finally:
            self.busy = False
            self.save()

    def _turn(self, text: str, images: list[str]) -> str:
        display = text
        pre = []
        if self._first:
            pre += [self._env_note(), knowledge.session_context(self.tb.cwd)]
        pre += self._pending
        if any(pre):
            text = "\n\n".join(x for x in (*pre, text) if x)
        parts: list[dict] = [{"type": "text", "text": text}]
        for p in images:
            part, note = load_image_part(p, self.cfg)
            parts += [{"type": "text", "text": f"[image: {self.tb.image_label(p)}]"}, part]
            self.emit("notice", text=f"image {note}")
        self.messages.append({
            "role": "user", "content": parts if images else text,
            "_display": display, "_images": images, "_ts": time.time(),
            "_first": self._first, "_notes": list(self._pending),
        })
        self._first, self._pending = False, []
        if not self.title:
            self.title = sessions.title_from(display)
        if images:
            drop_old_images(self.messages, self.cfg.max_images_in_context)

        steps = range(self.cfg.max_steps) if self.cfg.max_steps > 0 else itertools.count()
        last_text, failed, seen, stall = "", False, {}, 0
        for step in steps:
            if self._stop.is_set():
                return self._cancelled(last_text)
            try:
                self.ctx.maybe_compact(self.messages, log=lambda s: self.emit("notice", text=s))
            except Cancelled:
                return self._cancelled(last_text)
            think = self._thinking(step, failed)
            self.emit("step", step=step, think=think)
            t0 = time.time()
            try:
                res = self.llm.chat(
                    self.messages, self.tool_schemas, thinking=think,
                    temperature=self.mode.temperature, top_p=self.mode.top_p,
                    on_content=lambda c: self.emit("content", text=c),
                    on_reasoning=lambda c: self.emit("reasoning", text=c),
                )
            except Cancelled as c:
                p = c.partial
                msg = {"role": "assistant", "content": (p.content + "\n\n" if p.content else "") + "[interrupted by the user]"}
                self.messages.append(msg)
                return self._cancelled(p.content or last_text)
            self._account(res, time.time() - t0, think)
            self.messages.append(res.to_message())
            self.ctx.observe(res.prompt_tokens, len(self.messages) - 1)
            last_text = res.content or last_text
            if res.finish_reason == "length" and not res.tool_calls:
                self.emit("notice", text="output hit max_tokens, asking the model to continue")
                self.messages.append({"role": "user", "content": "You were cut off. Continue, but be brief."})
                continue
            if not res.tool_calls:
                return res.content
            failed, progress = self._run_tools(res, seen)
            if self._stop.is_set():
                return self._cancelled(last_text)
            # Loop breaker: steps that only repeat what the model already has don't count as
            # progress. Nudge after 2 such steps, force a decision after 4.
            stall = 0 if progress else stall + 1
            if stall == 2:
                self.emit("notice", text="the model is repeating itself; nudging it to decide")
                self.messages.append({"role": "user", "content": self._stall_note()})
            elif stall >= 4:
                return self._force_answer()
            failed = failed or not progress  # re-enable thinking to get out of the rut
        self.emit("notice", text=f"reached the step limit ({self.cfg.max_steps}); stopping")
        return last_text

    def _stall_note(self) -> str:
        seen_imgs = []
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, list):
                seen_imgs += [p["text"][8:-1] for p, q in zip(c, c[1:])
                              if p.get("text", "").startswith("[image: ") and q.get("type") == "image_url"]
        imgs = f" Images you can already see: {', '.join(seen_imgs)}." if seen_imgs else ""
        return ("[harness] Your last steps only repeated things you already have." + imgs +
                " Stop re-checking. Decide now with what you have and act on it (e.g. download / set_wallpaper),"
                " or answer the user.")

    def _force_answer(self) -> str:
        self.emit("notice", text="stopped a loop: asking the model for its decision without further tools")
        self.messages.append({"role": "user", "content": (
            "[harness] You are stuck in a loop, so tools are disabled for this reply. Using only what you have "
            "already seen, give your decision and answer now. If you were choosing between images, say which one "
            "and why; the user can tell you to go ahead.")})
        self.emit("step", step=-1, think=False)
        t0 = time.time()
        try:
            res = self.llm.chat(self.messages, self.tool_schemas, thinking=False, tool_choice="none",
                                temperature=self.mode.temperature, top_p=self.mode.top_p,
                                on_content=lambda c: self.emit("content", text=c))
        except Cancelled:
            return self._cancelled("")
        self._account(res, time.time() - t0, False)
        content = re.sub(r"(?s)<tool_call>.*?(</tool_call>|$)", "", res.content).strip()
        self.messages.append({"role": "assistant", "content": content or "(no answer)"})
        return content

    def _cancelled(self, last_text: str) -> str:
        self.emit("cancelled")
        return last_text

    def _thinking(self, step: int, failed: bool) -> bool:
        if self.cfg.thinking == "on":
            return True
        if self.cfg.thinking == "off":
            return False
        policy = self.mode.thinking_policy
        if policy == "always":
            return True
        if policy == "off":
            return False
        if policy == "initial_only":
            return step == 0
        return step == 0 or failed  # adaptive

    def _run_tools(self, res: Completion, seen: dict) -> tuple[bool, bool]:
        """Run the calls. Returns (any_failed, made_progress)."""
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
            t0 = time.time()
            self.emit("tool_start", id=tc["id"], name=name, args=args)
            redundant = False
            if err:
                out, is_err = err, True
            elif self._stop.is_set():
                out, is_err = "Error: cancelled by the user before it ran.", True
            elif name == "view_image" and image_in_context(self.messages, label := self.tb.image_label(str(args.get("path", "")))):
                out, is_err, redundant = (f"[image: {label}] is still visible above in this conversation; look at it "
                                          "there instead of viewing it again."), False, True
            else:
                sig = (name, json.dumps(args, sort_keys=True))
                seen[sig] = seen.get(sig, 0) + 1
                tool = self.tb.tools.get(name)
                redundant = seen[sig] >= 2 and bool(tool and tool.read_only) or name == "contact_sheet" and seen[sig] >= 2
                if seen[sig] >= 3 and name not in ("bash", "view_image"):
                    out, is_err = "Error: you've made this exact call 3 times. It won't change. Try something different.", True
                else:
                    out, is_err = self.tb.run(name, args)
            self.emit("tool_end", id=tc["id"], name=name, args=args, output=out, error=is_err, dt=time.time() - t0)
            return out, is_err, redundant

        read_only = all(self.tb.tools.get(n) and self.tb.tools[n].read_only for _, n, _, _ in parsed)
        if len(parsed) > 1 and read_only:
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(one, parsed))
        else:
            results = [one(p) for p in parsed]

        any_failed = False
        progress = not all(r[2] for r in results)
        for (tc, name, _, _), (out, is_err, _) in zip(parsed, results):
            any_failed |= is_err
            self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out, "_err": is_err})

        if self.tb.pending_images:
            self.messages.append({"role": "user", "content": [
                {"type": "text", "text": "(images from view_image)"}, *self.tb.pending_images]})
            self.tb.pending_images.clear()
            drop_old_images(self.messages, self.cfg.max_images_in_context)
        return any_failed, progress

    def _account(self, res: Completion, dt: float, think: bool) -> None:
        s = self.stats
        s["prompt"] += res.prompt_tokens
        s["cached"] += res.cached_tokens
        s["completion"] += res.completion_tokens
        s["steps"] += 1
        self.emit("usage", prompt=res.prompt_tokens, cached=res.cached_tokens, completion=res.completion_tokens,
                  dt=dt, tps=res.completion_tokens / dt if dt > 0 else 0, think=think,
                  ctx=res.prompt_tokens + res.completion_tokens)

    # ------------------------------------------------------------ inspection
    def context_tokens(self) -> int:
        return self.ctx.estimate(self.messages) if self.started else 0

    def compact(self) -> bool:
        return self.ctx.maybe_compact(self.messages, log=lambda s: self.emit("notice", text=s), force=True)

    def undo(self) -> tuple[str, list[str]] | None:
        """Remove the last user turn and everything after it. Returns its (text, images)."""
        idx = next((i for i in range(len(self.messages) - 1, 0, -1) if "_display" in self.messages[i]), None)
        if idx is None:
            return None
        m = self.messages[idx]
        del self.messages[idx:]
        if m.get("_first"):
            self._first = True
        # Re-queue the notes that rode on the removed turn; newer notes of the same kind win.
        notes = {}
        for n in [*(m.get("_notes") or []), *self._pending]:
            notes[n.split("]", 1)[0].split(" ")[0]] = n
        self._pending = list(notes.values())
        if not self.started:
            self.title = ""
        self.ctx.observe(0, 0)  # history changed: fall back to the char estimate
        self.save()
        return m["_display"], list(m.get("_images") or [])

    def transcript(self):
        """Yield display items to rebuild a conversation view:
        ("user", text, images) ("reasoning", text) ("assistant", text)
        ("tool", name, args, output, error) ("summary", text)"""
        results = {m["tool_call_id"]: m for m in self.messages if m["role"] == "tool"}
        for m in self.messages[1:]:
            role, c = m["role"], m.get("content")
            if role == "user":
                if "_display" in m:
                    yield ("user", m["_display"], m.get("_images") or [])
                elif isinstance(c, str) and c.startswith("[Summary of earlier work"):
                    yield ("summary", c)
            elif role == "assistant":
                r = m.get("reasoning_content") or m.get("reasoning")
                if r:
                    yield ("reasoning", r)
                if c and c != "Understood. Continuing from that summary.":
                    yield ("assistant", c)
                for tc in m.get("tool_calls") or []:
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except ValueError:
                        args = {"_raw": tc["function"]["arguments"]}
                    res = results.get(tc["id"], {})
                    yield ("tool", tc["function"]["name"], args, res.get("content", ""), bool(res.get("_err")))

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        if not self.cfg.save_sessions or not self.started:
            return
        try:
            sessions.save(self.session_id, {
                "title": self.title, "created": self.created, "updated": time.time(),
                "cwd": str(self.tb.cwd), "mode": self.mode.id, "model": self.cfg.model,
                "turns": sum(1 for m in self.messages if "_display" in m),
                "stats": self.stats, "messages": self.messages,
            })
        except OSError as e:
            self.emit("notice", text=f"could not save session: {e}")

    def resume(self, sid: str) -> None:
        d = sessions.load(sid)
        self.session_id, self.title = sid, d.get("title", "")
        self.created = d.get("created", time.time())
        self.messages = d["messages"]
        self.stats = {**self.stats, **d.get("stats", {})}
        self.mode = get_mode(d.get("mode", self.mode.id))
        self.cfg.mode = self.mode.id
        if d.get("cwd") and Path(d["cwd"]).is_dir():
            self.tb.cwd = Path(d["cwd"])
        self._first = not any("_display" in m for m in self.messages)
        self._pending = []
        self.ctx.observe(0, 0)
