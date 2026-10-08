"""GTK4 / libadwaita desktop app for qh.

- session sidebar (search, resume, delete), auto-saved conversations
- streamed markdown answers with code blocks + copy buttons
- collapsible thoughts, live tool cards (args, output, duration, status)
- stop button (Esc) that aborts generation and kills running commands
- multi-line composer: Enter sends, Shift+Enter newline, paste / drag & drop images
- working-directory picker, mode picker, live context gauge, server status
- approval dialogs for dangerous commands and memory notes, preferences dialog
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import knowledge, md, sessions  # noqa: E402
from .agent import Agent  # noqa: E402
from .client import LLM  # noqa: E402
from .config import CONFIG_DIR, DATA_DIR, Config  # noqa: E402
from .modes import MODES, MODES_DIR  # noqa: E402

APP_ID = "org.antigravity.qh"  # keep stable: desktop files / window rules match on it
STATE_FILE = DATA_DIR / "gui.json"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".avif"}

CSS = """
.qh-user { background: alpha(@accent_bg_color, 0.14); border-radius: 16px; padding: 10px 14px; }
.qh-code { background: alpha(@view_fg_color, 0.05); border-radius: 10px; border: 1px solid alpha(@view_fg_color, 0.08); }
.qh-code-header { padding: 2px 4px 0 12px; }
.qh-code-body { padding: 4px 12px 10px 12px; font-family: monospace; }
.qh-table { font-family: monospace; padding: 8px 12px; background: alpha(@view_fg_color, 0.03); border-radius: 8px; }
.qh-tool { background: alpha(@view_fg_color, 0.04); border-radius: 10px; padding: 6px 10px; }
.qh-tool-out { font-family: monospace; font-size: 0.9em; }
.qh-thought { color: alpha(@view_fg_color, 0.6); font-style: italic; }
.qh-thought-box { border-left: 2px solid alpha(@accent_color, 0.4); padding-left: 10px; margin-left: 6px; }
.qh-notice { color: alpha(@view_fg_color, 0.55); font-size: 0.9em; }
.qh-error { background: alpha(@error_bg_color, 0.15); border-radius: 10px; padding: 8px 12px; }
.qh-composer { background: @card_bg_color; border-radius: 18px; border: 1px solid alpha(@view_fg_color, 0.12); padding: 6px; }
.qh-composer textview, .qh-composer textview text { background: transparent; }
.qh-thumb { border-radius: 8px; }
.qh-stat { color: alpha(@view_fg_color, 0.5); font-size: 0.85em; }
.qh-dot-ok { color: @success_color; }
.qh-dot-bad { color: @error_color; }
.qh-dot-wait { color: @warning_color; }
.qh-fab { border-radius: 999px; min-width: 36px; min-height: 36px; }
"""


# ----------------------------------------------------------------- utilities
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(d: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d))
    except OSError:
        pass


def spinner() -> Gtk.Widget:
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner()
    s = Gtk.Spinner()
    s.start()
    return s


def label(text: str = "", css: tuple[str, ...] = (), wrap: bool = True, select: bool = False,
          markup: bool = False, xalign: float = 0.0) -> Gtk.Label:
    lb = Gtk.Label(xalign=xalign)
    lb.set_wrap(wrap)
    if wrap:
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lb.set_selectable(select)
    for c in css:
        lb.add_css_class(c)
    if markup:
        set_markup_safe(lb, text)
    else:
        lb.set_text(text)
    return lb


def set_markup_safe(lb: Gtk.Label, markup: str) -> None:
    """Pango rejects the whole label on one bad tag, so validate first and fall back to
    plain text. (<a> is GtkLabel-only markup, so it is stripped for the check.)"""
    try:
        Pango.parse_markup(re.sub(r"</?a\b[^>]*>", "", markup), -1, "\0")
        lb.set_markup(markup)
    except GLib.Error:
        lb.set_text(html.unescape(re.sub(r"<[^>]+>", "", markup)))


def copy_text(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set(text)


def human(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"


def brief(name: str, args: dict, n: int = 90) -> str:
    if name == "bash":
        s = args.get("command", "")
    elif "path" in args:
        s = args["path"]
    elif "url" in args:
        s = args["url"]
    elif "query" in args:
        s = args["query"]
    else:
        s = " ".join(f"{k}={v}" for k, v in args.items())
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


TOOL_ICONS = {
    "bash": "utilities-terminal-symbolic", "read_file": "document-open-symbolic",
    "write_file": "document-save-symbolic", "edit_file": "document-edit-symbolic",
    "grep": "edit-find-symbolic", "find_files": "system-search-symbolic",
    "web_search": "web-browser-symbolic", "fetch_url": "network-transmit-receive-symbolic",
    "download": "folder-download-symbolic", "view_image": "image-x-generic-symbolic",
    "inspect_image": "image-x-generic-symbolic", "identify": "image-x-generic-symbolic",
    "load_skill": "accessories-dictionary-symbolic", "propose_note": "starred-symbolic",
}


# -------------------------------------------------------------- message parts
class MarkdownView(Gtk.Box):
    """Renders markdown as a column of widgets. Re-rendering only rebuilds blocks whose
    source changed, so streaming stays cheap even for long answers."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.text = ""
        self._src: list[str] = []
        self._w: list[Gtk.Widget] = []
        self._pending = False

    def append(self, chunk: str) -> None:
        self.text += chunk
        if not self._pending:  # throttle: at most ~20 renders/s
            self._pending = True
            GLib.timeout_add(50, self._flush)

    def set_text(self, text: str) -> None:
        self.text = text
        self.render()

    def _flush(self) -> bool:
        self._pending = False
        self.render()
        return False

    def render(self) -> None:
        blocks = md.all_blocks(self.text)
        for i, b in enumerate(blocks):
            if i < len(self._src) and self._src[i] == b:
                continue
            blk = md.to_pango(b, code_attrs='bgalpha="12%" bgcolor="#888888"')
            old = self._w[i] if i < len(self._w) else None
            if old is not None and getattr(old, "_kind", None) == blk.kind and blk.kind in ("text", "code", "table"):
                old._update(blk)
            else:
                w = self._make(blk)
                if old is not None:
                    self.insert_child_after(w, old)
                    self.remove(old)
                    self._w[i] = w
                else:
                    self.append_widget(w)
            if i < len(self._src):
                self._src[i] = b
            else:
                self._src.append(b)
        for w in self._w[len(blocks):]:
            self.remove(w)
        del self._w[len(blocks):]
        del self._src[len(blocks):]

    def append_widget(self, w: Gtk.Widget) -> None:
        Gtk.Box.append(self, w)
        self._w.append(w)

    def _make(self, blk: md.Block) -> Gtk.Widget:
        if blk.kind == "hr":
            w = Gtk.Separator()
        elif blk.kind == "code":
            w = CodeBlock(blk)
        elif blk.kind == "table":
            lb = label(blk.body, ("qh-table",), wrap=False, select=True)
            sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
            sw.set_child(lb)
            w = sw
            w._update = lambda b: lb.set_text(b.body)
        else:
            lb = label(blk.body, select=True, markup=True)
            lb.connect("activate-link", lambda _l, uri: (Gtk.UriLauncher.new(uri).launch(None, None, None, None), True)[1])
            w = lb
            w._update = lambda b: set_markup_safe(lb, b.body)
        w._kind = blk.kind
        return w


class CodeBlock(Gtk.Box):
    def __init__(self, blk: md.Block):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("qh-code")
        hdr = Gtk.Box(spacing=6)
        hdr.add_css_class("qh-code-header")
        self.lang = label(blk.lang or "text", ("dim-label", "caption"), wrap=False)
        self.lang.set_hexpand(True)
        hdr.append(self.lang)
        btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy code")
        btn.add_css_class("flat")
        btn.connect("clicked", lambda b: (copy_text(b, self.code), b.set_icon_name("object-select-symbolic"),
                                          GLib.timeout_add(1200, lambda: b.set_icon_name("edit-copy-symbolic"))))
        hdr.append(btn)
        self.append(hdr)
        self.body = label("", ("qh-code-body",), wrap=False, select=True)
        sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        sw.set_child(self.body)
        self.append(sw)
        self._kind = "code"
        self._update(blk)

    def _update(self, blk: md.Block) -> None:
        self.code = blk.body
        self.body.set_text(blk.body)
        self.lang.set_text(blk.lang or "text")


class ThoughtView(Gtk.Expander):
    def __init__(self, expanded: bool):
        super().__init__()
        self.t0 = time.time()
        self.text = ""
        hdr = Gtk.Box(spacing=8)
        self.spin = spinner()
        hdr.append(self.spin)
        self.title = label("Thinking…", ("dim-label",), wrap=False)
        hdr.append(self.title)
        self.set_label_widget(hdr)
        box = Gtk.Box()
        box.add_css_class("qh-thought-box")
        self.body = label("", ("qh-thought",), select=True)
        self.body.set_hexpand(True)
        box.append(self.body)
        self.set_child(box)
        self.set_expanded(expanded)
        self._pending = False
        self.done = False

    def append(self, chunk: str) -> None:
        self.text += chunk
        if not self._pending:
            self._pending = True
            GLib.timeout_add(80, self._flush)

    def _flush(self) -> bool:
        self._pending = False
        if not self.done:
            self.body.set_text(self.text.strip())
            self.title.set_text(f"Thinking… {human(len(self.text) / 3.5)} tokens")
        return False

    def finish(self, collapse: bool = True) -> None:
        self.done = True
        if self.spin.get_parent():
            self.spin.get_parent().remove(self.spin)
        self.body.set_text(self.text.strip())
        dt = time.time() - self.t0
        self.title.set_text(f"Thought for {dt:.0f}s" if dt >= 1 else "Thoughts")
        if collapse:
            self.set_expanded(False)


class ToolCard(Gtk.Box):
    def __init__(self, name: str, args: dict):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("qh-tool")
        self.name, self.args, self.t0 = name, args, time.time()
        exp = Gtk.Expander()
        hdr = Gtk.Box(spacing=8)
        self.status = Gtk.Stack()
        self.status.add_named(spinner(), "run")
        ok = Gtk.Image(icon_name="object-select-symbolic")
        ok.add_css_class("success")
        bad = Gtk.Image(icon_name="dialog-error-symbolic")
        bad.add_css_class("error")
        self.status.add_named(ok, "ok")
        self.status.add_named(bad, "err")
        hdr.append(self.status)
        hdr.append(Gtk.Image(icon_name=TOOL_ICONS.get(name, "applications-system-symbolic")))
        nm = label(name, ("heading",), wrap=False)
        hdr.append(nm)
        arg = label(brief(name, args), ("dim-label",), wrap=False)
        arg.set_ellipsize(Pango.EllipsizeMode.END)
        arg.set_hexpand(True)
        hdr.append(arg)
        self.dt = label("", ("qh-stat",), wrap=False)
        hdr.append(self.dt)
        exp.set_label_widget(hdr)
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6)
        self.detail.append(self._args_widget())
        exp.set_child(self.detail)
        self.append(exp)

    def _args_widget(self) -> Gtk.Widget:
        a = self.args
        if self.name == "bash":
            text = "$ " + a.get("command", "")
        elif self.name == "edit_file":
            text = f"{a.get('path')}\n--- old\n{a.get('old', '')}\n+++ new\n{a.get('new', '')}"
        elif self.name == "write_file":
            c = a.get("content", "")
            text = f"{a.get('path')} ({len(c)} chars)\n{c[:3000]}{'…' if len(c) > 3000 else ''}"
        else:
            text = json.dumps(a, indent=2, ensure_ascii=False)
        return label(text, ("qh-tool-out", "dim-label"), select=True)

    def finish(self, output: str, error: bool, dt: float) -> None:
        self.status.set_visible_child_name("err" if error else "ok")
        self.dt.set_text(f"{dt:.1f}s" if dt >= 0.1 else "")
        out = output if len(output) <= 8000 else output[:8000] + f"\n… [{len(output) - 8000} more chars]"
        self.detail.append(Gtk.Separator())
        self.detail.append(label(out.strip() or "(no output)", ("qh-tool-out",) + (("error",) if error else ()), select=True))


class UserBubble(Gtk.Box):
    def __init__(self, text: str, images: list[str]):
        super().__init__(halign=Gtk.Align.END, margin_start=60)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.add_css_class("qh-user")
        if images:
            row = Gtk.Box(spacing=6)
            for p in images:
                row.append(thumb(p, 96))
            inner.append(row)
        if text:
            inner.append(label(text, select=True))
        self.append(inner)


def thumb(path: str, size: int) -> Gtk.Widget:
    pic = Gtk.Picture.new_for_filename(path)
    pic.set_content_fit(Gtk.ContentFit.COVER)
    pic.set_size_request(size, size)
    pic.set_can_shrink(True)
    pic.add_css_class("qh-thumb")
    pic.set_overflow(Gtk.Overflow.HIDDEN)
    pic.set_tooltip_text(path)
    return pic


class AssistantMessage(Gtk.Box):
    """One assistant turn: thoughts, tool cards and answer text in the order they happened."""

    def __init__(self, win: "QHWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.win = win
        self.cur_md: MarkdownView | None = None
        self.cur_thought: ThoughtView | None = None
        self.tools: dict[str, ToolCard] = {}
        self.answer = ""
        self.stats = {"steps": 0, "completion": 0, "dt": 0.0}
        self.footer: Gtk.Box | None = None

    # live events
    def on_step(self, d):
        self._end_thought()
        self.cur_md = None
        self.answer = ""

    def on_reasoning(self, d):
        if self.cur_thought is None:
            self.cur_thought = ThoughtView(self.win.cfg.show_reasoning)
            self.append(self.cur_thought)
        self.cur_thought.append(d["text"])

    def on_content(self, d):
        self._end_thought()
        if self.cur_md is None:
            self.cur_md = MarkdownView()
            self.append(self.cur_md)
        self.answer += d["text"]
        self.cur_md.append(d["text"])

    def on_tool_start(self, d):
        self._end_thought()
        self.cur_md = None
        card = ToolCard(d["name"], d["args"])
        self.tools[d["id"]] = card
        self.append(card)

    def on_tool_end(self, d):
        card = self.tools.get(d["id"])
        if card:
            card.finish(d["output"], d["error"], d["dt"])

    def on_usage(self, d):
        self.stats["steps"] += 1
        self.stats["completion"] += d["completion"]
        self.stats["dt"] += d["dt"]

    def on_notice(self, d):
        self.append(label(d["text"], ("qh-notice",)))

    def on_cancelled(self, d):
        self._end_thought()
        for c in self.tools.values():
            if c.status.get_visible_child_name() == "run":
                c.finish("interrupted", True, time.time() - c.t0)
        self.append(label("Interrupted", ("qh-notice",)))

    def _end_thought(self):
        if self.cur_thought is not None:
            self.cur_thought.finish()
            self.cur_thought = None

    # replay
    def add_static(self, item) -> None:
        kind = item[0]
        if kind == "reasoning":
            t = ThoughtView(False)
            t.text = item[1]
            t.finish()
            self.append(t)
        elif kind == "assistant":
            v = MarkdownView()
            v.set_text(item[1])
            self.append(v)
            self.answer = item[1]
        elif kind == "tool":
            c = ToolCard(item[1], item[2])
            c.finish(item[3], item[4], -1)
            self.append(c)

    def finish(self, error: str | None = None) -> None:
        self._end_thought()
        if self.cur_md:
            self.cur_md.render()
        if error:
            box = Gtk.Box(spacing=8)
            box.add_css_class("qh-error")
            box.append(Gtk.Image(icon_name="dialog-error-symbolic"))
            box.append(label(error, select=True))
            self.append(box)
        self.add_footer()

    def add_footer(self) -> None:
        f = Gtk.Box(spacing=4)
        s = self.stats
        if s["steps"]:
            tps = s["completion"] / s["dt"] if s["dt"] else 0
            f.append(label(f"{s['steps']} step{'s' if s['steps'] > 1 else ''} · {human(s['completion'])} tokens · "
                           f"{tps:.0f} tok/s · {s['dt']:.1f}s", ("qh-stat",), wrap=False))
        spacer = Gtk.Box(hexpand=True)
        f.append(spacer)
        if self.answer:
            b = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy answer")
            b.add_css_class("flat")
            b.connect("clicked", lambda btn: (copy_text(btn, self.answer), self.win.toast("Answer copied")))
            f.append(b)
        r = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Retry (Ctrl+R)")
        r.add_css_class("flat")
        r.set_action_name("win.retry")
        f.append(r)
        self.footer = f
        self.append(f)


# ------------------------------------------------------------------- window
class QHWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Qwen Harness")
        self.state = load_state()
        self.set_default_size(self.state.get("width", 1100), self.state.get("height", 780))
        self.set_size_request(360, 400)  # required by Adw breakpoints
        self.cfg = Config.load()
        cwd = self.state.get("cwd") or os.environ.get("QH_CWD") or str(Path.home())
        if not Path(cwd).is_dir():
            cwd = str(Path.home())
        self.agent = self._new_agent(cwd)
        self.attached: list[str] = []
        self.busy = False
        self.msg: AssistantMessage | None = None
        self.turn_t0 = 0.0
        self.live = {"phase": "", "tokens": 0, "t": 0.0}
        self.history_idx = -1
        self._build()
        self._actions()
        self.refresh_sessions()
        self.check_server()
        GLib.timeout_add_seconds(60, lambda: (self.check_server() if not self.busy else None, True)[1])
        self.connect("close-request", self._on_close)

    def _new_agent(self, cwd: str) -> Agent:
        a = Agent(self.cfg, cwd, on_event=lambda k, d: GLib.idle_add(self._event, k, d))
        a.tb.ask = self._ask_from_worker
        return a

    # ------------------------------------------------------------------ build
    def _build(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.split = Adw.OverlaySplitView()
        self.split.set_show_sidebar(self.state.get("sidebar", True))
        self.split.set_sidebar_width_fraction(0.24)
        self.split.set_max_sidebar_width(320)
        self.toasts.set_child(self.split)
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self._build_content())

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        # drag & drop files onto the window
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

    def _build_sidebar(self) -> Gtk.Widget:
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_show_end_title_buttons(False)
        hb.set_title_widget(Adw.WindowTitle(title="Chats"))
        new = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New chat (Ctrl+N)")
        new.set_action_name("win.new-chat")
        hb.pack_start(new)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search = Gtk.SearchEntry(placeholder_text="Search chats", margin_start=8, margin_end=8, margin_bottom=6)
        self.search.connect("search-changed", lambda *_: self.sess_list.invalidate_filter())
        box.append(self.search)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.sess_list = Gtk.ListBox()
        self.sess_list.add_css_class("navigation-sidebar")
        self.sess_list.set_filter_func(self._filter_session)
        self.sess_list.connect("row-activated", self._on_session_activated)
        sw.set_child(self.sess_list)
        box.append(sw)
        tv.set_content(box)
        return tv

    def _build_content(self) -> Gtk.Widget:
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        side = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Toggle chats (F9)")
        side.bind_property("active", self.split, "show-sidebar", GObject.BindingFlags.BIDIRECTIONAL)
        side.set_active(self.split.get_show_sidebar())
        hb.pack_start(side)
        self.wtitle = Adw.WindowTitle(title="New chat", subtitle="")
        hb.set_title_widget(self.wtitle)

        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("New Chat", "win.new-chat")
        sec.append("Retry Last Message", "win.retry")
        sec.append("Undo Last Message", "win.undo")
        sec.append("Compact Context", "win.compact")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Open Config Folder", "win.open-config")
        sec.append("Edit Profile", "win.edit-profile")
        sec.append("Preferences", "win.preferences")
        sec.append("Keyboard Shortcuts", "win.shortcuts")
        sec.append("About", "win.about")
        menu.append_section(None, sec)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu")
        hb.pack_end(mb)
        self.status_dot = label("●", ("qh-dot-wait",), wrap=False)
        self.status_dot.set_tooltip_text("checking server…")
        hb.pack_end(self.status_dot)
        tv.add_top_bar(hb)

        overlay = Gtk.Overlay()
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=600)
        self.chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                            margin_top=18, margin_bottom=24, margin_start=16, margin_end=16)
        clamp.set_child(self.chat)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.empty = Adw.StatusPage(icon_name="user-available-symbolic", title="Qwen Harness")
        self.empty.set_vexpand(True)
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(clamp, "chat")
        self.scroller.set_child(self.stack)
        overlay.set_child(self.scroller)
        self.sticky = True
        adj = self.scroller.get_vadjustment()
        adj.connect("value-changed", self._on_scroll)
        adj.connect("notify::upper", self._on_grow)
        self.fab = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Scroll to bottom",
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=12, visible=False)
        self.fab.add_css_class("osd")
        self.fab.add_css_class("qh-fab")
        self.fab.connect("clicked", lambda *_: self.scroll_bottom(force=True))
        overlay.add_overlay(self.fab)
        tv.set_content(overlay)
        tv.add_bottom_bar(self._build_composer())
        self._update_empty()
        return tv

    def _build_composer(self) -> Gtk.Widget:
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=600)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12, margin_end=12,
                        margin_bottom=10, margin_top=4)
        clamp.set_child(outer)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("qh-composer")
        outer.append(card)

        self.attach_row = Gtk.Box(spacing=6, margin_start=6, margin_top=4, visible=False)
        card.append(self.attach_row)

        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                max_content_height=220)
        self.input = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                  top_margin=8, bottom_margin=4, left_margin=10, right_margin=10)
        self.input.set_size_request(-1, 36)
        self.buf = self.input.get_buffer()
        self.buf.connect("changed", lambda *_: self._sync_send())
        self.placeholder = label("Message Qwen…  (Enter to send, Shift+Enter for a new line)", ("dim-label",), wrap=False)
        self.placeholder.set_can_target(False)
        ov = Gtk.Overlay()
        ov.set_child(self.input)
        self.placeholder.set_halign(Gtk.Align.START)
        self.placeholder.set_valign(Gtk.Align.START)
        self.placeholder.set_margin_start(12)
        self.placeholder.set_margin_top(8)
        ov.add_overlay(self.placeholder)
        sw.set_child(ov)
        card.append(sw)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.input.add_controller(keys)

        row = Gtk.Box(spacing=4)
        attach = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Attach images (or paste / drop them)")
        attach.add_css_class("flat")
        attach.connect("clicked", self._on_attach)
        row.append(attach)

        self.cwd_btn = Gtk.Button(tooltip_text="Working directory")
        self.cwd_btn.add_css_class("flat")
        cb = Gtk.Box(spacing=6)
        cb.append(Gtk.Image(icon_name="folder-symbolic"))
        self.cwd_lbl = label("", wrap=False)
        self.cwd_lbl.set_ellipsize(Pango.EllipsizeMode.START)
        self.cwd_lbl.set_max_width_chars(28)
        cb.append(self.cwd_lbl)
        self.cwd_btn.set_child(cb)
        self.cwd_btn.connect("clicked", self._on_pick_cwd)
        row.append(self.cwd_btn)

        self.mode_ids = list(MODES)
        self.mode_dd = Gtk.DropDown.new_from_strings([MODES[m].name for m in self.mode_ids])
        self.mode_dd.set_tooltip_text("Reasoning mode")
        self.mode_dd.add_css_class("flat")
        self.mode_dd.set_selected(self.mode_ids.index(self.agent.mode.id) if self.agent.mode.id in self.mode_ids else 0)
        self.mode_dd.connect("notify::selected", self._on_mode)
        row.append(self.mode_dd)

        row.append(Gtk.Box(hexpand=True))
        self.ctx_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
        self.ctx_bar.set_size_request(70, -1)
        self.ctx_bar.add_offset_value("low", self.cfg.compact_at * 0.7)
        self.ctx_bar.add_offset_value("high", self.cfg.compact_at)
        self.ctx_bar.add_offset_value("full", 1.0)
        row.append(self.ctx_bar)
        self.ctx_lbl = label("", ("qh-stat",), wrap=False)
        row.append(self.ctx_lbl)

        self.send = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Send (Enter)", valign=Gtk.Align.CENTER)
        self.send.add_css_class("suggested-action")
        self.send.add_css_class("circular")
        self.send.connect("clicked", lambda *_: self.stop() if self.busy else self.submit())
        row.append(self.send)
        card.append(row)

        self.status = label("Ready", ("qh-stat",), wrap=False)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_margin_start(14)
        outer.append(self.status)
        self._update_cwd()
        self._update_ctx()
        self._sync_send()
        return clamp

    def _actions(self):
        acts = {
            "new-chat": lambda *_: self.new_chat(), "stop": lambda *_: self.stop(),
            "retry": lambda *_: self.retry(), "undo": lambda *_: self.undo(),
            "compact": lambda *_: self.compact(), "preferences": lambda *_: self.preferences(),
            "open-config": lambda *_: self.open_path(CONFIG_DIR),
            "edit-profile": lambda *_: self.open_path(knowledge.PROFILE_FILES[0], touch=True),
            "toggle-sidebar": lambda *_: self.split.set_show_sidebar(not self.split.get_show_sidebar()),
            "focus-input": lambda *_: self.input.grab_focus(), "shortcuts": lambda *_: self.shortcuts(),
            "about": lambda *_: self.about(), "attach": lambda *_: self._on_attach(None),
        }
        for name, fn in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", fn)
            self.add_action(a)
        app = self.get_application()
        for name, accels in {
            "win.new-chat": ["<Control>n"], "win.stop": ["Escape"], "win.retry": ["<Control>r"],
"win.preferences": ["<Control>comma"], "win.toggle-sidebar": ["F9"],
            "win.focus-input": ["<Control>l"], "win.attach": ["<Control>o"], "win.shortcuts": ["<Control>question"],
            "win.compact": ["<Control><Shift>k"],
        }.items():
            app.set_accels_for_action(name, accels)

    # ---------------------------------------------------------------- helpers
    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def _update_empty(self):
        has = self.chat.get_first_child() is not None
        self.stack.set_visible_child_name("chat" if has else "empty")
        self.empty.set_description(
            f"{GLib.markup_escape_text(self.cfg.model)} · {GLib.markup_escape_text(self.agent.mode.name)}\n"
            f"<small>Enter to send · Shift+Enter new line · Esc stops · paste or drop images</small>")

    def _update_cwd(self):
        p = str(self.agent.tb.cwd).replace(str(Path.home()), "~")
        self.cwd_lbl.set_text(p)
        self.cwd_btn.set_tooltip_text(f"Working directory: {self.agent.tb.cwd}")

    def _update_ctx(self):
        n = self.agent.context_tokens()
        frac = n / self.cfg.context_window
        self.ctx_bar.set_value(min(1.0, frac))
        self.ctx_lbl.set_text(f"{human(n)}")
        self.ctx_bar.set_tooltip_text(f"Context: {n:,} / {self.cfg.context_window:,} tokens ({frac:.0%}). "
                                      f"Auto-compacts at {self.cfg.compact_at:.0%}.")

    def _update_title(self):
        self.wtitle.set_title(self.agent.title or "New chat")

    def _sync_send(self):
        text = self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)
        self.placeholder.set_visible(not text)
        if self.busy:
            self.send.set_icon_name("media-playback-stop-symbolic")
            self.send.set_tooltip_text("Stop (Esc)")
            self.send.remove_css_class("suggested-action")
            self.send.add_css_class("destructive-action")
            self.send.set_sensitive(True)
        else:
            self.send.set_icon_name("go-up-symbolic")
            self.send.set_tooltip_text("Send (Enter)")
            self.send.remove_css_class("destructive-action")
            self.send.add_css_class("suggested-action")
            self.send.set_sensitive(bool(text.strip() or self.attached))

    def _on_scroll(self, adj):
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        self.sticky = at_bottom
        self.fab.set_visible(not at_bottom)

    def _on_grow(self, adj, _p):
        if self.sticky:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def scroll_bottom(self, force: bool = False):
        if force:
            self.sticky = True
        adj = self.scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper() - adj.get_page_size()))

    def add_row(self, w: Gtk.Widget) -> None:
        self.chat.append(w)
        self._update_empty()

    def clear_chat(self) -> None:
        while (c := self.chat.get_first_child()) is not None:
            self.chat.remove(c)
        self._update_empty()

    # ---------------------------------------------------------------- server
    def check_server(self):
        def work():
            try:
                served = LLM(self.cfg).models(timeout=3)
                GLib.idle_add(self._server_ok, served)
            except Exception as e:
                GLib.idle_add(self._server_bad, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _server_ok(self, served: list[str]):
        if served and self.cfg.model not in served:
            self.cfg.model = served[0]
            self.toast(f"Using served model {served[0]}")
        for c in ("qh-dot-bad", "qh-dot-wait"):
            self.status_dot.remove_css_class(c)
        self.status_dot.add_css_class("qh-dot-ok")
        self.status_dot.set_tooltip_text(f"Online · {self.cfg.model} @ {self.cfg.base_url}")
        self.wtitle.set_subtitle(f"{self.cfg.model}")

    def _server_bad(self, err: str):
        for c in ("qh-dot-ok", "qh-dot-wait"):
            self.status_dot.remove_css_class(c)
        self.status_dot.add_css_class("qh-dot-bad")
        self.status_dot.set_tooltip_text(f"Server unreachable: {self.cfg.base_url}\n{err[:200]}")
        self.wtitle.set_subtitle("server offline")

    # -------------------------------------------------------------- sessions
    def refresh_sessions(self):
        while (r := self.sess_list.get_row_at_index(0)) is not None:
            self.sess_list.remove(r)
        for s in sessions.list_sessions():
            row = Gtk.ListBoxRow()
            row.sid, row.title = s.id, s.title
            box = Gtk.Box(spacing=6, margin_top=4, margin_bottom=4)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            t = label(s.title, wrap=False)
            t.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(t)
            sub = f"{s.when} · {s.turns} msg · {Path(s.cwd).name or '~'}" if s.cwd else s.when
            col.append(label(sub, ("qh-stat",), wrap=False))
            box.append(col)
            rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete chat", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.set_opacity(0.55)
            rm.connect("clicked", lambda _b, sid=s.id: self.delete_session(sid))
            box.append(rm)
            row.set_child(box)
            self.sess_list.append(row)
            if s.id == self.agent.session_id:
                self.sess_list.select_row(row)

    def _filter_session(self, row) -> bool:
        q = self.search.get_text().strip().lower()
        return not q or q in row.title.lower()

    def _on_session_activated(self, _lb, row):
        if self.busy:
            self.toast("Stop the current turn first")
            return
        if row.sid == self.agent.session_id:
            return
        self.load_session(row.sid)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def load_session(self, sid: str):
        a = self._new_agent(str(self.agent.tb.cwd))
        try:
            a.resume(sid)
        except (OSError, ValueError, KeyError) as e:
            self.toast(f"Could not open chat: {e}")
            return
        self.agent = a
        self.clear_chat()
        msg = None
        for item in a.transcript():
            if item[0] == "user":
                if msg:
                    msg.add_footer()
                self.add_row(UserBubble(item[1], [p for p in item[2] if Path(p).exists()]))
                msg = None
            elif item[0] == "summary":
                self.add_row(label("Earlier messages were compacted into a summary.", ("qh-notice",), xalign=0.5))
            else:
                if msg is None:
                    msg = AssistantMessage(self)
                    self.add_row(msg)
                msg.add_static(item)
        if msg:
            msg.add_footer()
        self.mode_dd.set_selected(self.mode_ids.index(a.mode.id) if a.mode.id in self.mode_ids else 0)
        self._update_cwd()
        self._update_ctx()
        self._update_title()
        self._update_empty()
        self.scroll_bottom(force=True)

    def delete_session(self, sid: str):
        def done(dlg, res):
            if dlg.choose_finish(res) != "delete":
                return
            sessions.delete(sid)
            if sid == self.agent.session_id:
                self.new_chat()
            self.refresh_sessions()
        d = Adw.AlertDialog(heading="Delete chat?", body="This conversation will be permanently removed.")
        d.add_response("cancel", "Cancel")
        d.add_response("delete", "Delete")
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        d.choose(self, None, done)

    def new_chat(self):
        if self.busy:
            self.toast("Stop the current turn first")
            return
        mode = self.agent.mode.id
        self.agent = self._new_agent(str(self.agent.tb.cwd))
        self.agent.set_mode(mode)
        self.clear_chat()
        self._update_title()
        self._update_ctx()
        self.sess_list.unselect_all()
        self.input.grab_focus()

    # ------------------------------------------------------------- composer
    def _on_key(self, _ctl, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK):
                return False
            if not self.busy:
                self.submit()
            return True
        if keyval == Gdk.KEY_v and state & Gdk.ModifierType.CONTROL_MASK:
            clip = self.input.get_clipboard()
            if clip.get_formats().contain_gtype(Gdk.Texture):
                clip.read_texture_async(None, self._on_paste_texture)
                return True
        if keyval == Gdk.KEY_Up and not self._input_text():
            hist = [m["_display"] for m in self.agent.messages if "_display" in m]
            if hist:
                self.buf.set_text(hist[-1])
                return True
        return False

    def _input_text(self) -> str:
        return self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)

    def _on_paste_texture(self, clip, res):
        try:
            tex = clip.read_texture_finish(res)
        except GLib.Error:
            return
        p = Path(tempfile.mkdtemp(prefix="qh_paste_")) / "pasted.png"
        tex.save_to_png(str(p))
        self.attach(str(p))

    def _on_drop(self, _t, value, _x, _y):
        for f in value.get_files():
            p = f.get_path()
            if p and Path(p).suffix.lower() in IMAGE_EXT:
                self.attach(p)
            elif p:
                self.buf.insert_at_cursor(p)
        return True

    def _on_attach(self, _b):
        dlg = Gtk.FileDialog(title="Attach images")
        f = Gtk.FileFilter()
        f.set_name("Images")
        f.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg.set_filters(filters)
        dlg.open_multiple(self, None, self._on_attach_done)

    def _on_attach_done(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        for i in range(files.get_n_items()):
            self.attach(files.get_item(i).get_path())

    def attach(self, path: str):
        if path and path not in self.attached:
            self.attached.append(path)
        self._render_attachments()

    def _render_attachments(self):
        while (c := self.attach_row.get_first_child()) is not None:
            self.attach_row.remove(c)
        for p in self.attached:
            ov = Gtk.Overlay()
            ov.set_child(thumb(p, 56))
            x = Gtk.Button(icon_name="window-close-symbolic", halign=Gtk.Align.END, valign=Gtk.Align.START)
            x.add_css_class("circular")
            x.add_css_class("osd")
            x.connect("clicked", lambda _b, p=p: (self.attached.remove(p), self._render_attachments()))
            ov.add_overlay(x)
            self.attach_row.append(ov)
        self.attach_row.set_visible(bool(self.attached))
        self._sync_send()

    def _on_pick_cwd(self, _b):
        dlg = Gtk.FileDialog(title="Working directory", initial_folder=Gio.File.new_for_path(str(self.agent.tb.cwd)))
        dlg.select_folder(self, None, self._on_cwd_done)

    def _on_cwd_done(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        self.agent.set_cwd(f.get_path())
        self.state["cwd"] = f.get_path()
        self._update_cwd()
        self.toast(f"Working directory: {f.get_path()}")

    def _on_mode(self, dd, _p):
        mid = self.mode_ids[dd.get_selected()]
        if mid != self.agent.mode.id:
            self.agent.set_mode(mid)
            self.mode_dd.set_tooltip_text(MODES[mid].description)
            self.status.set_text(f"Mode: {MODES[mid].name} — {MODES[mid].description}")
            self._update_empty()

    # ------------------------------------------------------------------ turns
    def submit(self, text: str | None = None, images: list[str] | None = None):
        if self.busy:
            return
        if text is None:
            text = self._input_text().strip()
            images = list(self.attached)
            if not text and not images:
                return
            self.buf.set_text("")
            self.attached.clear()
            self._render_attachments()
        text = text or "Describe this image."
        self.add_row(UserBubble(text, images or []))
        self.msg = AssistantMessage(self)
        self.add_row(self.msg)
        self.scroll_bottom(force=True)
        self.busy = True
        self.turn_t0 = time.time()
        self.live = {"phase": "Waiting for model", "tokens": 0, "t": time.time()}
        self._sync_send()
        GLib.timeout_add(250, self._tick)
        threading.Thread(target=self._worker, args=(self.agent, self.msg, text, images or []), daemon=True).start()

    def _worker(self, agent: Agent, msg: AssistantMessage, text: str, images: list[str]):
        err = None
        try:
            agent.user_turn(text, images)
        except Exception as e:
            err = str(e) or type(e).__name__
        GLib.idle_add(self._finished, agent, msg, err)

    def _event(self, kind: str, d: dict):
        if self.msg is None:
            return False
        getattr(self.msg, f"on_{kind}", lambda d: None)(d)
        lv = self.live
        if kind == "step":
            lv.update(phase="Thinking" if d["think"] else "Generating", tokens=0, t=time.time())
        elif kind == "reasoning":
            lv["tokens"] += 1
        elif kind == "content":
            if lv["phase"] != "Writing":
                lv.update(phase="Writing", tokens=0, t=time.time())
            lv["tokens"] += 1
        elif kind == "tool_start":
            lv.update(phase=f"Running {d['name']}", t=time.time())
        elif kind == "tool_end":
            lv.update(phase="Waiting for model", t=time.time())
        elif kind == "usage":
            self._update_ctx()
        return False

    def _tick(self) -> bool:
        if not self.busy:
            return False
        lv = self.live
        dt = time.time() - lv["t"]
        extra = ""
        if lv["phase"] in ("Thinking", "Writing", "Generating") and dt > 0.5 and lv["tokens"]:
            extra = f" · {lv['tokens'] / dt:.0f} tok/s"
        self.status.set_text(f"{lv['phase']}… {dt:.0f}s{extra}  ·  turn {time.time() - self.turn_t0:.0f}s")
        return True

    def _finished(self, agent: Agent, msg: AssistantMessage, err: str | None):
        self.busy = False
        msg.finish(err)
        if self.msg is msg:
            self.msg = None
        if agent is self.agent:
            self._update_ctx()
            self._update_title()
            self.refresh_sessions()
        s = agent.stats
        hit = s["cached"] / s["prompt"] if s["prompt"] else 0
        self.status.set_text(f"{'Error' if err else 'Done'} in {time.time() - self.turn_t0:.1f}s · "
                             f"session: {s['steps']} calls, {hit:.0%} prompt cache hits")
        if err:
            self.check_server()
        self._sync_send()
        if not self.is_active():
            self._notify("Qwen finished" if not err else "Qwen hit an error", (msg.answer or err or "")[:200])
        return False

    def _notify(self, title: str, body: str):
        n = Gio.Notification.new(title)
        n.set_body(body)
        self.get_application().send_notification("turn-done", n)

    def stop(self):
        if self.busy:
            self.agent.cancel()
            self.status.set_text("Stopping…")

    def _replay_last(self):
        """Remove the widgets of the last exchange."""
        kids = []
        c = self.chat.get_first_child()
        while c:
            kids.append(c)
            c = c.get_next_sibling()
        last_user = max((i for i, k in enumerate(kids) if isinstance(k, UserBubble)), default=None)
        if last_user is not None:
            for k in kids[last_user:]:
                self.chat.remove(k)
        self._update_empty()

    def retry(self):
        if self.busy:
            return
        got = self.agent.undo()
        if not got:
            self.toast("Nothing to retry")
            return
        self._replay_last()
        self.submit(got[0], got[1])

    def undo(self):
        if self.busy:
            return
        got = self.agent.undo()
        if not got:
            self.toast("Nothing to undo")
            return
        self._replay_last()
        self.buf.set_text(got[0])
        for p in got[1]:
            self.attach(p)
        self._update_ctx()
        self.refresh_sessions()

    def compact(self):
        if self.busy or not self.agent.started:
            return
        self.busy = True
        self._sync_send()
        self.status.set_text("Compacting context…")

        def work():
            try:
                self.agent.compact()
                err = None
            except Exception as e:
                err = str(e)
            GLib.idle_add(self._compacted, err)
        threading.Thread(target=work, daemon=True).start()

    def _compacted(self, err):
        self.busy = False
        self._sync_send()
        self._update_ctx()
        self.agent.save()
        self.status.set_text(f"Compaction failed: {err}" if err else "Context compacted")
        if not err:
            self.add_row(label("Earlier messages were compacted into a summary.", ("qh-notice",), xalign=0.5))

    # -------------------------------------------------------------- approval
    def _ask_from_worker(self, question: str) -> bool:
        ev, box = threading.Event(), []
        GLib.idle_add(self._ask, question, ev, box)
        ev.wait()
        return bool(box and box[0])

    def _ask(self, question: str, ev: threading.Event, box: list):
        head, _, body = question.partition("\n\n")
        d = Adw.AlertDialog(heading=head, body=body)
        if body:
            d.set_body_use_markup(False)
        d.add_response("deny", "Deny")
        d.add_response("allow", "Allow")
        d.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("deny")
        d.set_close_response("deny")

        def done(dlg, res):
            box.append(dlg.choose_finish(res) == "allow")
            ev.set()
        d.choose(self, None, done)
        if not self.is_active():
            self._notify("Qwen needs approval", head)
        return False

    # ------------------------------------------------------------ dialogs etc
    def open_path(self, p: Path, touch: bool = False):
        self._open_custom(Path(p), touch)

    def about(self):
        d = Adw.AboutDialog(application_name="Qwen Harness", application_icon="user-available-symbolic",
                            developer_name="zeta", version="0.2.0",
                            comments=f"Agentic harness for {self.cfg.model} on vLLM\n{self.cfg.base_url}")
        d.present(self)

    def shortcuts(self):
        rows = [("Enter", "Send"), ("Shift+Enter", "New line"), ("Esc", "Stop generation"),
                ("Ctrl+N", "New chat"), ("Ctrl+R", "Retry last message"),
                ("Ctrl+O", "Attach image"), ("Ctrl+V", "Paste image"), ("Ctrl+L", "Focus input"),
                ("↑", "Edit last message (empty input)"), ("F9", "Toggle chat list"),
                ("Ctrl+Shift+K", "Compact context"), ("Ctrl+,", "Preferences")]
        d = Adw.Dialog(title="Keyboard Shortcuts", content_width=380)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        grp = Adw.PreferencesGroup(margin_start=12, margin_end=12, margin_bottom=12)
        for k, v in rows:
            r = Adw.ActionRow(title=v)
            r.add_suffix(label(k, ("dim-label",), wrap=False))
            grp.add(r)
        tv.set_content(grp)
        d.set_child(tv)
        d.present(self)

    def preferences(self):
        c = self.cfg
        d = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        d.add(page)

        g = Adw.PreferencesGroup(title="Server", description="vLLM OpenAI-compatible endpoint")
        url = Adw.EntryRow(title="Base URL", text=c.base_url)
        model = Adw.EntryRow(title="Model", text=c.model)
        key = Adw.PasswordEntryRow(title="API key", text=c.api_key)
        for r in (url, model, key):
            g.add(r)
        test = Adw.ActionRow(title="Test connection", activatable=True)
        test.add_suffix(Gtk.Image(icon_name="network-transmit-receive-symbolic"))

        def run_test(_r):
            tmp = Config(**{**c.__dict__, "base_url": url.get_text(), "api_key": key.get_text()})
            def work():
                try:
                    m = LLM(tmp).models(timeout=4)
                    GLib.idle_add(test.set_subtitle, f"OK · serving {', '.join(m) or '?'}")
                except Exception as e:
                    GLib.idle_add(test.set_subtitle, f"Failed: {str(e)[:120]}")
            test.set_subtitle("Testing…")
            threading.Thread(target=work, daemon=True).start()
        test.connect("activated", run_test)
        g.add(test)
        page.add(g)

        g = Adw.PreferencesGroup(title="Generation")
        ctxw = Adw.SpinRow.new_with_range(4096, 1_048_576, 1024)
        ctxw.set_title("Context window")
        ctxw.set_subtitle("Must match vLLM --max-model-len")
        ctxw.set_value(c.context_window)
        maxt = Adw.SpinRow.new_with_range(256, 131072, 256)
        maxt.set_title("Max output tokens per call")
        maxt.set_value(c.max_tokens)
        steps = Adw.SpinRow.new_with_range(0, 500, 1)
        steps.set_title("Max steps per message")
        steps.set_subtitle("0 = unlimited")
        steps.set_value(c.max_steps)
        think_opts = ["auto", "on", "off"]
        think = Adw.ComboRow(title="Thinking", subtitle="auto = follow the reasoning mode",
                             model=Gtk.StringList.new(["Follow mode", "Always on", "Always off"]))
        think.set_selected(think_opts.index(c.thinking) if c.thinking in think_opts else 0)
        for r in (ctxw, maxt, steps, think):
            g.add(r)
        page.add(g)

        g = Adw.PreferencesGroup(title="Behaviour")
        reason = Adw.SwitchRow(title="Expand thoughts while thinking", active=c.show_reasoning)
        danger = Adw.SwitchRow(title="Confirm dangerous commands", subtitle="rm -rf, sudo, dd, mkfs, git push --force…",
                               active=c.confirm_dangerous)
        save = Adw.SwitchRow(title="Save conversations", active=c.save_sessions)
        for r in (reason, danger, save):
            g.add(r)
        page.add(g)

        g = Adw.PreferencesGroup(title="Customization", description="Plain files, picked up on the next new chat")
        for title, sub, path, touch in (
            ("System prompt", "system.md replaces the built-in prompt", CONFIG_DIR / "system.md", True),
            ("Profile", "facts about you and your machines", knowledge.PROFILE_FILES[0], True),
            ("Skills", "SKILL.md playbooks loaded on demand", knowledge.SKILL_DIRS[0], False),
            ("Custom modes", "one .md file per reasoning mode", MODES_DIR, False),
            ("Learned notes", "lessons the agent saved", knowledge.NOTES, True),
        ):
            r = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            r.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
            r.connect("activated", lambda _r, p=path, t=touch: self._open_custom(p, t))
            g.add(r)
        page.add(g)

        def closed(_d):
            c.base_url, c.model, c.api_key = url.get_text().strip(), model.get_text().strip(), key.get_text()
            c.context_window, c.max_tokens = int(ctxw.get_value()), int(maxt.get_value())
            c.max_steps, c.thinking = int(steps.get_value()), think_opts[think.get_selected()]
            c.show_reasoning, c.confirm_dangerous = reason.get_active(), danger.get_active()
            c.save_sessions = save.get_active()
            try:
                c.save(["base_url", "model", "api_key", "context_window", "max_tokens", "max_steps", "thinking",
                        "show_reasoning", "confirm_dangerous", "save_sessions"])
            except OSError as e:
                self.toast(f"Could not save config: {e}")
            if not self.busy:
                self.agent.llm = LLM(c)
                self.agent.ctx.llm = self.agent.llm
            self._update_ctx()
            self.check_server()
        d.connect("closed", closed)
        d.present(self)

    def _open_custom(self, p: Path, touch: bool):
        p = Path(p)
        if touch and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
        elif not touch:
            p.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(p))).launch(self, None, None, None)

    def _on_close(self, _w):
        if self.busy:
            self.agent.cancel()
        w, h = self.get_default_size()
        self.state.update(width=w, height=h, sidebar=self.split.get_show_sidebar(), cwd=str(self.agent.tb.cwd))
        save_state(self.state)
        return False


class QHApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window or QHWindow(self)
        win.present()
        win.input.grab_focus()


def main():
    return QHApp().run(sys.argv)


if __name__ == "__main__":
    main()
