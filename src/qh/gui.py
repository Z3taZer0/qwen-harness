"""GTK4 / Libadwaita Desktop App for Qwen 3.8 Harness (qh).
Clean, modern interface integrated with Wayland/Omarchy desktop.
Features:
- Prompt input with image attachment
- Reasoning Mode selector (Adaptive, Complex/Deep, Artistic/Aesthetic, Quick)
- Real-time token streaming with collapsible thought/reasoning log
- Tool execution monitor & token statistics
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from .agent import Agent
from .config import Config
from .modes import MODES, get_mode


class QHWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Qwen 3.8 Harness")
        self.set_default_size(900, 700)

        self.cfg = Config.load()
        self.agent = Agent(self.cfg, os.getcwd(), out=None)
        self.in_reasoning = False
        self.attached_images: list[str] = []
        self.in_reasoning = False
        self.is_busy = False

        self._build_ui()

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        # Header bar
        header = Adw.HeaderBar()
        main_box.append(header)

        # Title widget
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_label = Gtk.Label(label="Qwen 3.8")
        title_label.add_css_class("heading")
        badge = Gtk.Label(label="27B")
        badge.add_css_class("caption")
        badge.add_css_class("dim-label")
        title_box.append(title_label)
        title_box.append(badge)
        header.set_title_widget(title_box)

        # Clear button
        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear Conversation")
        clear_btn.connect("clicked", self._on_clear)
        header.pack_start(clear_btn)

        # Mode dropdown in header
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_lbl = Gtk.Label(label="Mode:")
        mode_lbl.add_css_class("dim-label")
        mode_box.append(mode_lbl)

        mode_strings = [f"{m.name} ({m.id})" for m in MODES.values()]
        self.mode_model = Gtk.StringList.new(mode_strings)
        self.mode_dropdown = Gtk.DropDown.new(self.mode_model, None)
        self.mode_dropdown.set_selected(0)
        self.mode_dropdown.connect("notify::selected-item", self._on_mode_changed)
        mode_box.append(self.mode_dropdown)
        header.pack_end(mode_box)

        # Chat / Output area
        self.scrolled = Gtk.ScrolledWindow()
        scrolled = self.scrolled
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        main_box.append(scrolled)

        self.chat_view = Gtk.TextView()
        self.chat_view.set_editable(False)
        self.chat_view.set_cursor_visible(False)
        self.chat_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_view.set_left_margin(16)
        self.chat_view.set_right_margin(16)
        self.chat_view.set_top_margin(16)
        self.chat_view.set_bottom_margin(16)
        self.chat_buffer = self.chat_view.get_buffer()

        # Styles
        self._setup_tags()
        scrolled.set_child(self.chat_view)

        # Attached images bar (hidden if empty)
        self.attach_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.attach_box.set_margin_start(16)
        self.attach_box.set_margin_end(16)
        self.attach_box.set_margin_top(6)
        self.attach_box.set_margin_bottom(6)
        self.attach_box.set_visible(False)
        main_box.append(self.attach_box)

        # Input box at bottom
        input_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        input_container.set_margin_start(16)
        input_container.set_margin_end(16)
        input_container.set_margin_top(10)
        input_container.set_margin_bottom(16)

        # Image attach button
        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Attach Image")
        attach_btn.connect("clicked", self._on_attach_image)
        input_container.append(attach_btn)

        # Text input entry
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask Qwen 3.8 anything (e.g. 'Download a 4k anime wallpaper similar to my collection')...")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_submit)
        input_container.append(self.entry)

        # Send button
        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.connect("clicked", self._on_submit)
        input_container.append(self.send_btn)

        # Spinner
        self.spinner = Gtk.Spinner()
        input_container.append(self.spinner)

        main_box.append(input_container)

        # Status footer bar
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_margin_start(16)
        footer.set_margin_end(16)
        footer.set_margin_bottom(8)
        self.status_label = Gtk.Label(label="Ready")
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        footer.append(self.status_label)
        main_box.append(footer)

        self._append_text("system", f"qh ready · Model: {self.cfg.model} @ {self.cfg.base_url}\nReasoning Mode: {self.agent.mode.name}\nType a message or attach an image to start.\n\n")

    def _setup_tags(self):
        tb = self.chat_buffer.get_tag_table()
        bold = Gtk.TextTag.new("user_tag")
        bold.set_property("weight", Pango.Weight.BOLD)
        bold.set_property("foreground", "#7aa2f7")
        tb.add(bold)

        sys_tag = Gtk.TextTag.new("system")
        sys_tag.set_property("foreground", "#7dcfff")
        tb.add(sys_tag)

        think_tag = Gtk.TextTag.new("reasoning")
        think_tag.set_property("foreground", "#b4befe")
        think_tag.set_property("style", Pango.Style.ITALIC)
        tb.add(think_tag)

        think_hdr = Gtk.TextTag.new("reasoning_hdr")
        think_hdr.set_property("foreground", "#89b4fa")
        think_hdr.set_property("weight", Pango.Weight.BOLD)
        tb.add(think_hdr)

        tool_tag = Gtk.TextTag.new("tool")
        tool_tag.set_property("foreground", "#e0af68")
        tb.add(tool_tag)

        asst_tag = Gtk.TextTag.new("assistant")
        asst_tag.set_property("foreground", "#c0caf5")
        tb.add(asst_tag)

    def _append_text(self, tag: str, text: str):
        end = self.chat_buffer.get_end_iter()
        self.chat_buffer.insert_with_tags_by_name(end, text, tag)
        mark = self.chat_buffer.create_mark(None, end, False)
        self.chat_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def _on_mode_changed(self, dropdown, _param):
        idx = dropdown.get_selected()
        mode_keys = list(MODES.keys())
        if 0 <= idx < len(mode_keys):
            m_id = mode_keys[idx]
            self.agent.set_mode(m_id)
            self.status_label.set_text(f"Mode switched to: {self.agent.mode.name}")
            self._append_text("system", f"\n[Mode changed: {self.agent.mode.name} — {self.agent.mode.description}]\n\n")

    def _on_attach_image(self, _btn):
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Image")
        filters = Gtk.FileFilter.new()
        filters.add_mime_type("image/png")
        filters.add_mime_type("image/jpeg")
        filters.add_mime_type("image/webp")
        filter_list = Gtk.FilterListModel.new()
        dialog.open(self, None, self._on_file_dialog_finish)

    def _on_file_dialog_finish(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                path = file.get_path()
                self.attached_images.append(path)
                self._update_attach_box()
        except Exception:
            pass

    def _update_attach_box(self):
        # Clear children
        child = self.attach_box.get_first_child()
        while child:
            next_ch = child.get_next_sibling()
            self.attach_box.remove(child)
            child = next_ch

        if not self.attached_images:
            self.attach_box.set_visible(False)
            return

        self.attach_box.set_visible(True)
        lbl = Gtk.Label(label="Attached:")
        lbl.add_css_class("dim-label")
        self.attach_box.append(lbl)

        for p in self.attached_images:
            pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            name_lbl = Gtk.Label(label=Path(p).name)
            name_lbl.add_css_class("badge")
            pill.append(name_lbl)
            self.attach_box.append(pill)

    def _on_clear(self, _btn):
        if self.is_busy:
            return
        self.agent = Agent(self.cfg, os.getcwd(), out=None)
        self.in_reasoning = False
        # Restore current mode selection
        idx = self.mode_dropdown.get_selected()
        mode_keys = list(MODES.keys())
        if 0 <= idx < len(mode_keys):
            self.agent.set_mode(mode_keys[idx])
        self.chat_buffer.set_text("")
        self._append_text("system", f"qh cleared · Mode: {self.agent.mode.name}\n\n")
        self.status_label.set_text("Conversation cleared")

    def _on_submit(self, _widget):
        text = self.entry.get_text().strip()
        if not text or self.is_busy:
            return

        self.is_busy = True
        self.in_reasoning = False
        self.send_btn.set_sensitive(False)
        self.entry.set_text("")
        self.spinner.start()
        self.status_label.set_text("Working...")

        images = list(self.attached_images)
        self.attached_images.clear()
        self._update_attach_box()

        # Display user input
        img_note = f" (+{len(images)} images)" if images else ""
        self._append_text("user_tag", f"You{img_note}: ")
        self._append_text("user_tag", f"{text}\n\n")

        self._append_text("assistant", f"Qwen [{self.agent.mode.id}]: ")

        # Run background thread for model turn
        threading.Thread(target=self._run_turn_worker, args=(text, images), daemon=True).start()

    def _run_turn_worker(self, text: str, images: list[str]):
        def token_cb(kind: str, chunk: str):
            GLib.idle_add(self._stream_token, kind, chunk)

        try:
            res = self.agent.user_turn(text, images, on_token=token_cb)
        except Exception as e:
            GLib.idle_add(self._on_turn_error, str(e))
            return

        GLib.idle_add(self._on_turn_finished, res)

    def _stream_token(self, kind: str, chunk: str):
        if kind == "reasoning":
            if not self.in_reasoning:
                self.in_reasoning = True
                self._append_text("reasoning_hdr", "\n💭 Thought Process:\n")
            self._append_text("reasoning", chunk)
        elif kind == "tool":
            if self.in_reasoning:
                self.in_reasoning = False
                self._append_text("system", "\n───\n")
            self._append_text("tool", f"\n{chunk}")
        else:
            if self.in_reasoning:
                self.in_reasoning = False
                self._append_text("system", "\n───\n")
            self._append_text("assistant", chunk)

    def _on_turn_finished(self, res: str):
        self._append_text("assistant", "\n\n")
        self.spinner.stop()
        self.send_btn.set_sensitive(True)
        self.is_busy = False
        s = self.agent.stats
        hit = (s['cached'] / s['prompt'] * 100) if s['prompt'] else 0
        self.status_label.set_text(f"Done · Cached: {hit:.0f}% · Total prompt: {s['prompt']}p")

    def _on_turn_error(self, err_msg: str):
        self._append_text("system", f"\n[Error: {err_msg}]\n\n")
        self.spinner.stop()
        self.send_btn.set_sensitive(True)
        self.is_busy = False
        self.status_label.set_text(f"Error: {err_msg}")


class QHApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.antigravity.qh")

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = QHWindow(self)
        win.present()


def main():
    app = QHApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    main()
