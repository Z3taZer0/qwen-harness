"""Terminal frontend: rich rendering + prompt_toolkit input.

Enter sends, Alt+Enter (or Esc then Enter) inserts a newline, Ctrl+C interrupts the
running turn (or clears the input line), Ctrl+D exits. Type /help for commands.
"""
from __future__ import annotations

import argparse
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import knowledge, md, sessions
from .agent import Agent
from .config import CONFIG_ENV, DATA_DIR, Config
from .modes import MODES

THEME = Theme({
    "dim": "grey50", "think": "italic grey58", "tool": "#e0af68", "ok": "#9ece6a", "err": "#f7768e",
    "accent": "#7aa2f7", "notice": "#bb9af7", "user": "bold #7aa2f7", "stat": "grey42",
})

COMMANDS = {
    "/help": "show this help",
    "/mode": "[id] show or switch reasoning mode",
    "/think": "[on|off|auto] override thinking for every mode",
    "/reasoning": "toggle streaming the model's thoughts",
    "/img": "<path> [text]  send an image (quote paths with spaces)",
    "/new": "start a new conversation (alias /clear)",
    "/sessions": "list saved conversations",
    "/resume": "<n|id> continue a saved conversation",
    "/undo": "drop the last exchange, put its text back in the prompt",
    "/retry": "drop the last answer and ask again",
    "/compact": "summarize history now to free context",
    "/context": "context usage and token stats (alias /stats)",
    "/cwd": "[path] show or change the working directory",
    "/copy": "copy the last answer to the clipboard",
    "/notes": "[approve|clear] show learned notes / approve pending ones",
    "/skills": "list skill playbooks",
    "/config": "show the effective configuration",
    "/exit": "quit (or Ctrl+D)",
}


def human(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"


class Renderer:
    """Turns agent events into terminal output. One rich Live area shows whatever is
    still in flight (spinner, partial thought line, unfinished markdown block, running
    tools); everything finished is printed above it once and never redrawn."""

    def __init__(self, console: Console, cfg: Config):
        self.c, self.cfg = console, cfg
        self.lock = threading.RLock()
        self.live = None
        self.reset()

    def reset(self) -> None:
        self.answer = ""          # full answer text of the current step
        self.printed_blocks = 0   # markdown blocks of `answer` already printed
        self.think_buf = ""       # partial reasoning line
        self.think_chars = 0
        self.in_think = False
        self.running: dict[str, tuple[str, dict, float]] = {}
        self.phase = "waiting for model"
        self.t0 = time.time()
        self.step_tokens = 0

    # -------------------------------------------------------------- live area
    def __rich__(self):
        parts = []
        if self.in_think and self.think_buf and self.cfg.show_reasoning:
            parts.append(Text(self.think_buf, style="think"))
        if self.answer:
            _, rest = md.split_blocks(self.answer)
            if rest.strip():
                h = max(5, self.c.size.height - 6)
                lines = rest.split("\n")
                if len(lines) > h:  # too tall to redraw cleanly: show the tail, render on completion
                    parts.append(Text("\n".join(lines[-h:]), style="dim"))
                else:
                    parts.append(Markdown(rest))
        for _, (name, args, t) in list(self.running.items()):
            parts.append(Spinner("dots", Text.assemble((f" {name} ", "tool"), (brief(name, args, 80), "dim"),
                                                       (f"  {time.time() - t:.0f}s", "stat"))))
        label = self.phase
        if self.in_think and not self.cfg.show_reasoning:
            label = f"thinking · {human(self.think_chars / 3.5)} tok"
        if not self.running:
            parts.append(Spinner("dots", Text(f" {label} · {time.time() - self.t0:.0f}s", style="dim"), style="accent"))
        return Group(*parts)

    def start(self) -> None:
        from rich.live import Live
        self.reset()
        if self.c.is_terminal:
            self.live = Live(self, console=self.c, refresh_per_second=12, transient=True)
            self.live.start()

    def stop(self) -> None:
        with self.lock:
            self._flush_think()
            self._flush_answer(final=True)
            if self.live:
                self.live.stop()
                self.live = None

    def pause(self):
        if self.live:
            self.live.stop()

    def resume(self):
        if self.live:
            self.live.start()

    def out(self, *r, **kw) -> None:
        (self.live.console if self.live else self.c).print(*r, **kw)

    # ------------------------------------------------------------- flushing
    def _flush_think(self) -> None:
        if self.in_think:
            if self.cfg.show_reasoning and self.think_buf.strip():
                self.out(Text(self.think_buf, style="think"))
            elif not self.cfg.show_reasoning:
                self.out(Text(f"💭 thought for ~{human(self.think_chars / 3.5)} tokens", style="dim"))
            self.think_buf, self.in_think = "", False

    def _flush_answer(self, final: bool = False) -> None:
        if not self.answer:
            return
        blocks = md.all_blocks(self.answer) if final else md.split_blocks(self.answer)[0]
        for b in blocks[self.printed_blocks:]:
            self.out(Markdown(b))
            if not md.FENCE.match(b) and not b.lstrip().startswith("|"):
                self.out()  # code blocks and tables bring their own spacing
        self.printed_blocks = len(blocks)
        if final:
            self.answer, self.printed_blocks = "", 0

    # --------------------------------------------------------------- events
    def __call__(self, kind: str, d: dict) -> None:
        with self.lock:
            getattr(self, f"on_{kind}", lambda d: None)(d)

    def on_step(self, d):
        self._flush_answer(final=True)
        self.phase = "thinking" if d["think"] else "generating"
        self.t0 = time.time()

    def on_reasoning(self, d):
        if not self.in_think:
            self.in_think = True
            if self.cfg.show_reasoning:
                self.out(Text("💭 thinking", style="notice"))
        self.think_chars += len(d["text"])
        self.think_buf += d["text"]
        if "\n" in self.think_buf and self.cfg.show_reasoning:
            done, self.think_buf = self.think_buf.rsplit("\n", 1)
            self.out(Text(done, style="think"))
        elif not self.cfg.show_reasoning:
            self.think_buf = self.think_buf[-200:]

    def on_content(self, d):
        if self.in_think:
            self._flush_think()
        self.phase = "writing"
        self.answer += d["text"]
        self._flush_answer()

    def on_tool_start(self, d):
        self._flush_think()
        self._flush_answer(final=True)
        self.running[d["id"]] = (d["name"], d["args"], time.time())
        self.phase = "running tools"

    def on_tool_end(self, d):
        self.running.pop(d["id"], None)
        mark = Text("✗ ", style="err") if d["error"] else Text("✓ ", style="ok")
        line = Text.assemble(mark, (d["name"], "tool"), " ", (brief(d["name"], d["args"], 110), "dim"),
                             (f"  {d['dt']:.1f}s" if d["dt"] >= 0.1 else "", "stat"))
        self.out(line)
        preview = tool_preview(d["name"], d["output"], d["error"])
        if preview:
            self.out(Text(preview, style="err" if d["error"] else "stat"), overflow="ellipsis", no_wrap=True)
        if not self.running:
            self.phase = "waiting for model"
            self.t0 = time.time()

    def on_usage(self, d):
        self._flush_think()
        self._flush_answer(final=True)
        hit = d["cached"] / d["prompt"] if d["prompt"] else 0
        ctx = d["ctx"] / self.cfg.context_window
        self.out(Text(
            f"  {human(d['prompt'])} in ({hit:.0%} cached) · {human(d['completion'])} out · "
            f"{d['tps']:.0f} tok/s · {d['dt']:.1f}s · ctx {ctx:.0%}", style="stat"), justify="right")

    def on_notice(self, d):
        self._flush_think()
        self.out(Text(f"• {d['text']}", style="notice"))

    def on_cancelled(self, d):
        self._flush_think()
        self._flush_answer(final=True)
        self.running.clear()
        self.out(Text("✗ interrupted", style="err"))


def brief(name: str, args: dict, n: int) -> str:
    if name == "bash":
        s = args.get("command", "")
    elif "path" in args and len(args) <= 3:
        s = args["path"] + "".join(f" {k}={v}" for k, v in args.items() if k not in ("path", "content", "old", "new"))
    elif "url" in args:
        s = args["url"]
    elif "query" in args:
        s = args["query"]
    else:
        s = " ".join(f"{k}={v}" for k, v in args.items())
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_preview(name: str, output: str, error: bool, lines: int = 3) -> str:
    if name in ("read_file", "load_skill") and not error:
        return ""
    out = [l for l in output.strip().splitlines() if l.strip() and l not in ("[exit 0]", "(no output)")]
    if not out:
        return ""
    shown = out[:lines]
    more = f"  … +{len(out) - lines} lines" if len(out) > lines else ""
    return "\n".join("    " + l[:200] for l in shown) + more


# ------------------------------------------------------------------- the app
class App:
    def __init__(self, cfg: Config, cwd: str):
        self.cfg = cfg
        self.c = Console(theme=THEME, highlight=False)
        self.r = Renderer(self.c, cfg)
        self.asks: queue.Queue = queue.Queue()
        self.agent = self._new_agent(cwd)
        self.last_answer = ""
        self.prefill = ""

    def _new_agent(self, cwd: str) -> Agent:
        a = Agent(self.cfg, cwd, on_event=self.r)
        a.tb.ask = self._ask_from_worker
        return a

    # Approval prompts come from the worker thread but must be asked on the main
    # thread (which owns the terminal and receives Ctrl+C).
    def _ask_from_worker(self, question: str) -> bool:
        ev, box = threading.Event(), []
        self.asks.put((question, ev, box))
        ev.wait()
        return bool(box and box[0])

    def _answer_asks(self) -> None:
        while not self.asks.empty():
            q, ev, box = self.asks.get()
            self.r.pause()
            try:
                self.c.print(Panel(Text(q), title="approval needed", border_style="tool", expand=False))
                ans = self.c.input("[tool]allow? \\[y/N][/] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            box.append(ans in ("y", "yes"))
            ev.set()
            self.r.resume()

    # ----------------------------------------------------------------- turns
    def run_turn(self, text: str, images: list[str] | None = None) -> None:
        if not self.c.is_terminal:
            return self._run_plain(text, images)
        result: list = []
        done = threading.Event()
        t = threading.Thread(target=self._worker, args=(text, images, result, done), daemon=True)
        self.r.start()
        t.start()
        interrupted = False
        # Wait on an Event, not Thread.join: a Ctrl+C landing inside join() can leave
        # is_alive() reporting False while the worker is still running.
        while not done.is_set():
            try:
                done.wait(0.05)
                self._answer_asks()
            except KeyboardInterrupt:
                if interrupted:  # second Ctrl+C: stop waiting
                    break
                interrupted = True
                self.agent.cancel()
        self.r.stop()
        if result and isinstance(result[0], Exception):
            self.c.print(Text(f"error: {result[0]}", style="err"))
        elif result and result[0]:
            self.last_answer = result[0]

    def _worker(self, text, images, result, done):
        try:
            result.append(self.agent.user_turn(text, images))
        except Exception as e:
            result.append(e)
        finally:
            done.set()

    def _run_plain(self, text, images):
        """Non-terminal stdout (piped): print the answer only."""
        self.agent.on_event = lambda k, d: (sys.stdout.write(d["text"]), sys.stdout.flush()) if k == "content" else None
        try:
            self.last_answer = self.agent.user_turn(text, images)
            sys.stdout.write("\n")
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)

    # ------------------------------------------------------------- interface
    def banner(self) -> None:
        status, color = self._server_status()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column()
        t.add_row("model", f"{self.cfg.model} [dim]@ {self.cfg.base_url}[/] [{color}]● {status}[/]")
        t.add_row("mode", f"{self.agent.mode.name} [dim]({self.agent.mode.id})[/]")
        t.add_row("cwd", escape(str(self.agent.tb.cwd)))
        if self.agent.tb.skills:
            t.add_row("skills", ", ".join(self.agent.tb.skills))
        self.c.print(Panel(t, title="[accent]qh[/] · qwen harness", title_align="left", border_style="grey35", expand=False))
        self.c.print("[dim]Enter send · Alt+Enter newline · Ctrl+C interrupt · /help commands · !cmd shell[/]")

    def _server_status(self) -> tuple[str, str]:
        try:
            served = self.agent.llm.models(timeout=2.5)
        except Exception:
            return "unreachable", "err"
        if served and self.cfg.model not in served:
            self.cfg.model = served[0]
            return f"online (using served model {served[0]})", "tool"
        return "online", "ok"

    def toolbar(self):
        a = self.agent
        ctx = a.context_tokens()
        pct = ctx / self.cfg.context_window
        think = "" if self.cfg.thinking == "auto" else f" · think {self.cfg.thinking}"
        cwd = str(a.tb.cwd).replace(str(Path.home()), "~")
        return [("class:tb", f" {a.mode.id}{think} · ctx {human(ctx)} ({pct:.0%}) · {cwd} ")]

    def loop(self) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion, PathCompleter
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style


        class Comp(Completer):
            paths = PathCompleter(expanduser=True)

            def get_completions(self, doc, ev):
                t = doc.text_before_cursor
                if t.startswith("/") and " " not in t:
                    for c, h in COMMANDS.items():
                        if c.startswith(t):
                            yield Completion(c, -len(t), display_meta=h)
                elif t.startswith("/mode "):
                    w = t[6:]
                    for m in MODES.values():
                        if m.id.startswith(w):
                            yield Completion(m.id, -len(w), display_meta=m.name)
                elif t.startswith(("/img ", "/cwd ")):
                    from prompt_toolkit.document import Document
                    sub = t.split(" ", 1)[1]
                    yield from self.paths.get_completions(Document(sub, len(sub)), ev)
                elif t.startswith("/resume "):
                    w = t[8:]
                    for i, s in enumerate(sessions.list_sessions()[:20], 1):
                        if s.id.startswith(w) or str(i).startswith(w):
                            yield Completion(str(i), -len(w), display=f"{i}. {s.title}", display_meta=s.when)

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _(e):
            e.current_buffer.insert_text("\n")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ps = PromptSession(
            history=FileHistory(str(DATA_DIR / "history")), completer=Comp(), complete_while_typing=True,
            key_bindings=kb, multiline=False, bottom_toolbar=self.toolbar,
            style=Style.from_dict({"tb": "bg:#1a1b26 #737aa2", "bottom-toolbar": "noreverse", "prompt": "#7aa2f7 bold"}),
        )
        self.banner()
        while True:
            try:
                line = ps.prompt([("class:prompt", "\n❯ ")], default=self.prefill,
                                 prompt_continuation=lambda w, n, s: "  ").strip()
                self.prefill = ""
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not line:
                continue
            try:
                if line.startswith("!"):
                    self.shell(line[1:])
                elif line.startswith("/"):
                    if self.command(line) == "exit":
                        break
                else:
                    self.run_turn(line)
            except Exception as e:
                self.c.print(Text(f"error: {e}", style="err"))

    def shell(self, cmd: str) -> None:
        """`!cmd` runs a command yourself, without the model."""
        r = subprocess.run(cmd, shell=True, cwd=self.agent.tb.cwd, text=True, capture_output=True)
        out = (r.stdout + r.stderr).rstrip()
        if out:
            self.c.print(Text(out))
        self.c.print(Text(f"[exit {r.returncode}]", style="stat"))

    # --------------------------------------------------------------- commands
    def command(self, line: str) -> str | None:
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        cmd, args = parts[0], parts[1:]
        a = self.agent
        if cmd in ("/exit", "/quit", "/q"):
            return "exit"
        if cmd in ("/help", "/?"):
            t = Table.grid(padding=(0, 2))
            for c, h in COMMANDS.items():
                t.add_row(f"[accent]{c}[/]", f"[dim]{escape(h)}[/]")
            t.add_row("[accent]!cmd[/]", "[dim]run a shell command yourself (not sent to the model)[/]")
            self.c.print(t)
        elif cmd in ("/new", "/clear"):
            self.agent = self._new_agent(str(a.tb.cwd))
            self.agent.set_mode(a.mode.id)
            self.c.clear()
            self.banner()
        elif cmd == "/mode":
            if not args:
                for m in MODES.values():
                    cur = "[ok]●[/]" if m.id == a.mode.id else " "
                    self.c.print(f"{cur} [accent]{m.id:<10}[/] {m.name} [dim]— {escape(m.description)}[/]")
            elif args[0] in MODES:
                a.set_mode(args[0])
                self.c.print(f"[notice]mode → {a.mode.name}[/]")
            else:
                self.c.print(f"[err]unknown mode. choose: {', '.join(MODES)}[/]")
        elif cmd == "/think":
            if args and args[0] in ("on", "off", "auto"):
                self.cfg.thinking = args[0]
            self.c.print(f"[notice]thinking: {self.cfg.thinking}[/] [dim](auto = follow the mode)[/]")
        elif cmd == "/reasoning":
            self.cfg.show_reasoning = not self.cfg.show_reasoning
            self.c.print(f"[notice]show reasoning: {'on' if self.cfg.show_reasoning else 'off'}[/]")
        elif cmd == "/img":
            if not args:
                self.c.print("[err]usage: /img <path> [text][/]")
            else:
                imgs = [os.path.expanduser(p) for p in args if Path(os.path.expanduser(p)).is_file()]
                text = " ".join(p for p in args if os.path.expanduser(p) not in imgs) or "Describe this image."
                if not imgs:
                    self.c.print(f"[err]no such file: {escape(args[0])}[/]")
                else:
                    self.run_turn(text, imgs)
        elif cmd == "/sessions":
            items = sessions.list_sessions()[:20]
            if not items:
                self.c.print("[dim]no saved sessions yet[/]")
            for i, s in enumerate(items, 1):
                cur = "[ok]●[/]" if s.id == a.session_id else " "
                self.c.print(f"{cur}[accent]{i:>3}[/] {escape(s.title)} [dim]· {s.turns} turns · {s.mode} · {s.when}[/]")
        elif cmd == "/resume":
            sid = sessions.resolve(args[0]) if args else (sessions.list_sessions() or [None])[0]
            sid = sid.id if isinstance(sid, sessions.SessionInfo) else sid
            if not sid:
                self.c.print("[err]session not found (see /sessions)[/]")
            else:
                self.resume(sid)
        elif cmd in ("/undo", "/retry"):
            got = a.undo()
            if not got:
                self.c.print("[err]nothing to undo[/]")
            elif cmd == "/retry":
                self.c.print(Text(f"↻ {got[0]}", style="dim"))
                self.run_turn(*got)
            else:
                self.prefill = got[0]
                self.c.print("[notice]last exchange removed[/]")
        elif cmd == "/compact":
            with self.c.status("compacting…"):
                done = a.compact()
            self.c.print(f"[notice]{'compacted' if done else 'nothing to compact'} · ctx now {human(a.context_tokens())}[/]")
        elif cmd in ("/context", "/stats"):
            s, ctx = a.stats, a.context_tokens()
            hit = s["cached"] / s["prompt"] if s["prompt"] else 0
            self.c.print(f"context  {human(ctx)} / {human(self.cfg.context_window)} ({ctx / self.cfg.context_window:.0%})"
                         f" · compacts at {self.cfg.compact_at:.0%}")
            self.c.print(f"session  {s['steps']} model calls · {human(s['prompt'])} prompt ({hit:.0%} cached) · "
                         f"{human(s['completion'])} generated · {a.ctx.compactions} compactions")
            self.c.print(f"[dim]id {a.session_id}[/]")
        elif cmd == "/cwd":
            if args:
                a.set_cwd(args[0])
            self.c.print(f"cwd: {escape(str(a.tb.cwd))}")
        elif cmd == "/copy":
            self.copy(self.last_answer)
        elif cmd == "/notes":
            self.notes(args[0] if args else "")
        elif cmd == "/skills":
            if not a.tb.skills:
                self.c.print(f"[dim]no skills. add SKILL.md files under {knowledge.SKILL_DIRS[0]}[/]")
            for s in a.tb.skills.values():
                self.c.print(f"[accent]{s.name}[/] [dim]— {escape(s.description[:150])}[/]")
        elif cmd == "/config":
            from dataclasses import fields
            for f in fields(self.cfg):
                v = getattr(self.cfg, f.name)
                self.c.print(f"[dim]{f.name:<26}[/] {escape(str(v)) if f.name != 'api_key' else '***'}")
            self.c.print(f"[dim]file: {CONFIG_ENV}[/]")
        else:
            self.c.print(f"[err]unknown command {escape(cmd)} — /help[/]")
        return None

    def resume(self, sid: str) -> None:
        self.agent = self._new_agent(str(self.agent.tb.cwd))
        self.agent.resume(sid)
        self.c.print(Panel(f"{escape(self.agent.title)}\n[dim]{self.agent.session_id} · {self.agent.mode.id} · "
                           f"{escape(str(self.agent.tb.cwd))}[/]", title="resumed", border_style="grey35", expand=False))
        items = list(self.agent.transcript())
        for item in items[-12:]:  # replay the tail so you know where you left off
            kind = item[0]
            if kind == "user":
                self.c.print(Text(f"❯ {item[1]}", style="user"))
            elif kind == "assistant":
                self.c.print(Markdown(item[1]))
                self.last_answer = item[1]
            elif kind == "tool":
                mark = Text("✗ ", style="err") if item[4] else Text("✓ ", style="ok")
                self.c.print(Text.assemble(mark, (item[1], "tool"), " ", (brief(item[1], item[2], 110), "dim")))
            elif kind == "summary":
                self.c.print(Text("• earlier history was compacted", style="notice"))

    def copy(self, text: str) -> None:
        if not text:
            self.c.print("[err]nothing to copy[/]")
            return
        for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
            if shutil.which(tool[0]):
                subprocess.run(tool, input=text, text=True)
                self.c.print(f"[notice]copied {len(text)} chars[/]")
                return
        self.c.print("[err]no clipboard tool found (install wl-clipboard)[/]")

    def notes(self, sub: str) -> None:
        pend = knowledge.NOTES_PENDING
        if sub == "approve" and pend.exists():
            with open(knowledge.NOTES, "a") as f:
                f.write(pend.read_text())
            pend.unlink()
            self.c.print("[notice]pending notes approved[/]")
            return
        if sub == "clear" and pend.exists():
            pend.unlink()
            self.c.print("[notice]pending notes discarded[/]")
            return
        for title, p in (("notes", knowledge.NOTES), ("pending (/notes approve | clear)", pend)):
            if p.exists() and p.read_text().strip():
                self.c.print(Panel(escape(p.read_text().strip()), title=title, border_style="grey35", expand=False))
        if not knowledge.NOTES.exists() and not pend.exists():
            self.c.print("[dim]no notes yet[/]")


def main() -> None:
    ap = argparse.ArgumentParser(prog="qh", description="Qwen harness: an agent for your local vLLM Qwen.")
    ap.add_argument("prompt", nargs="*", help="one-shot prompt (omit for interactive). stdin is appended if piped")
    ap.add_argument("-C", "--cwd", default=os.getcwd(), help="working directory")
    ap.add_argument("-m", "--mode", choices=list(MODES), help="reasoning mode")
    ap.add_argument("--thinking", choices=["on", "off", "auto"])
    ap.add_argument("--base-url")
    ap.add_argument("-i", "--image", action="append", default=[], help="attach an image (repeatable)")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the most recent session")
    ap.add_argument("-r", "--resume", metavar="ID", help="resume a session by id, prefix or /sessions number")
    ap.add_argument("-l", "--list", action="store_true", help="list saved sessions and exit")
    a = ap.parse_args()

    cfg = Config.load()
    if a.thinking:
        cfg.thinking = a.thinking
    if a.base_url:
        cfg.base_url = a.base_url
    if a.mode:
        cfg.mode = a.mode
    app = App(cfg, a.cwd)

    if a.list:
        app.command("/sessions")
        return
    if a.cont or a.resume:
        sid = sessions.resolve(a.resume) if a.resume else next(iter(s.id for s in sessions.list_sessions()), None)
        if not sid:
            app.c.print("[err]no session to resume[/]")
            sys.exit(1)
        app.resume(sid)

    prompt = " ".join(a.prompt)
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        prompt = f"{prompt}\n\n```\n{piped}\n```" if prompt else piped
        app.agent.tb.ask = None  # nobody can answer approval prompts
    if prompt or a.image:
        app.run_turn(prompt or "Describe this image.", a.image)
        return
    app.loop()


if __name__ == "__main__":
    main()
