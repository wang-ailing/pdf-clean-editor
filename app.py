from __future__ import annotations

import ctypes
import io
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import traceback

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if not getattr(sys, "frozen", False) and (ROOT / "vendor126").is_dir():
    sys.path.insert(0, str(ROOT / "vendor126"))
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "PDFCleanEditor"
from PIL import Image, ImageChops, ImageDraw, ImageTk
from pdf_engine import PdfDocument, HIGHLIGHT_STYLES
from text_selection import TextSelector
from native_drop import WindowsFileDrop

APP_NAME = "PDF擦除与高亮"
APP_VERSION = "1.2.0"
BG, INK, MUTED = "#eef1f5", "#263447", "#708092"


def size_text(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.2f} MB"


class Editor:
    def __init__(self, root, initial_path=None):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1120x820")
        root.minsize(640, 480)
        root.configure(bg=BG)
        try:
            root.iconbitmap(str(ROOT / "editor.ico"))
        except tk.TclError:
            pass
        self.pdf = self.selector = self.base_image = self.photo = None
        self.page, self.scale, self.fit = 0, 1.0, True
        self.actions, self.undo_stack, self.redo_stack = [], [], []
        self.saved_token = "[]"
        self.drag_start = self.drag_pixel = self.selection = None
        self.selection_photos = []
        self.origin = (20, 20)
        self.busy = False
        self.result_queue = queue.Queue()
        self.resize_after = self.selection_after = None
        self.pending_drag_end = None
        self.status = tk.StringVar(value="拖入 PDF 打开；划选文字后可复制、高亮或擦除。")
        self._build()
        self._shortcuts()
        self.file_drop = None
        root.after_idle(self._enable_file_drop)
        root.report_callback_exception = lambda typ, value, tb: self._error(
            "操作失败", value, "".join(traceback.format_exception(typ, value, tb)))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(120, self._poll)
        if initial_path:
            root.after(160, lambda: self.open_path(initial_path))

    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", font=("Microsoft YaHei UI", 9), borderwidth=0, padding=(9, 5))
        header = tk.Frame(self.root, bg="white", height=46)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.menu_button = tk.Menubutton(header, text="☰", bg="white", fg=INK, relief="flat",
                                         font=("Segoe UI", 15), cursor="hand2", padx=15)
        self.file_menu = tk.Menu(self.menu_button, tearoff=False, font=("Microsoft YaHei UI", 10))
        self.file_menu.add_command(label="打开 PDF…", accelerator="Ctrl+O", command=self.open_file)
        self.file_menu.add_command(label="保存", accelerator="Ctrl+S", command=self.save)
        self.file_menu.add_command(label="另存副本…", accelerator="Ctrl+Shift+S", command=self.save_as)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="退出", command=self.close)
        self.menu_button.configure(menu=self.file_menu)
        self.menu_button.pack(side="left", fill="y")
        self.filename = tk.Label(header, text=APP_NAME, bg="white", fg=INK, font=("Microsoft YaHei UI", 10), anchor="w")
        self.filename.pack(side="left", fill="x", expand=True, padx=6)
        tk.Label(header, text="Ctrl+S 保存", bg="white", fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="right", padx=18)
        document = tk.Frame(self.root, bg=BG)
        document.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(document, bg=BG, highlightthickness=0, cursor="xterm")
        vs = ttk.Scrollbar(document, orient="vertical", command=self._vscroll)
        hs = ttk.Scrollbar(document, orient="horizontal", command=self._hscroll)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<ButtonPress-1>", self._start_selection)
        self.canvas.bind("<B1-Motion>", self._drag_selection)
        self.canvas.bind("<ButtonRelease-1>", self._end_selection)
        self.canvas.bind("<Button-3>", self._show_copy_menu)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<ButtonPress-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        footer = tk.Frame(self.root, bg="white", height=35)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        tk.Label(footer, textvariable=self.status, bg="white", fg=MUTED, font=("Microsoft YaHei UI", 9),
                 anchor="w").pack(side="left", fill="x", expand=True, padx=14)
        self.prev_button = tk.Button(footer, text="‹", command=lambda: self.change_page(self.page - 1), bg="white",
                                     fg=INK, bd=0, font=("Segoe UI", 15), padx=9, cursor="hand2")
        self.prev_button.pack(side="left")
        self.page_label = tk.Label(footer, text="—", bg="white", fg=MUTED, font=("Segoe UI", 9), width=9)
        self.page_label.pack(side="left")
        self.next_button = tk.Button(footer, text="›", command=lambda: self.change_page(self.page + 1), bg="white",
                                     fg=INK, bd=0, font=("Segoe UI", 15), padx=9, cursor="hand2")
        self.next_button.pack(side="left", padx=(0, 10))
        self.floating_bar = tk.Frame(self.canvas, bg="#d6dce4", padx=1, pady=1)
        bar = tk.Frame(self.floating_bar, bg="white", padx=3, pady=3)
        bar.pack()
        self.copy_button = tk.Button(bar, text="复制", command=self.copy_selection, bg="white",
                                     activebackground="#edf4ff", fg=INK, bd=0,
                                     font=("Microsoft YaHei UI", 11), cursor="hand2", padx=12, pady=8)
        self.copy_button.pack(side="left")
        tk.Frame(bar, bg="#e3e7ed", width=1, height=22).pack(side="left", padx=3)
        self.style_icons, self.highlight_buttons = {}, {}
        for key in ("highlight", "confused", "useful"):
            style = HIGHLIGHT_STYLES[key]
            icon = Image.new("RGBA", (16, 16))
            ImageDraw.Draw(icon).rectangle((1, 4, 15, 13), fill=style["swatch"])
            self.style_icons[key] = ImageTk.PhotoImage(icon)
            button = tk.Button(bar, text=" " + style["label"], image=self.style_icons[key], compound="left",
                               command=lambda s=key: self.apply_selection(s), bg="white", activebackground=style["hover"],
                               fg=INK, bd=0, font=("Microsoft YaHei UI", 11), cursor="hand2", padx=12, pady=8)
            button.pack(side="left")
            self.highlight_buttons[key] = button
        self.highlight_button = self.highlight_buttons["highlight"]
        tk.Frame(bar, bg="#e3e7ed", width=1, height=22).pack(side="left", padx=3)
        self.erase_button = tk.Button(bar, text="◇  擦除", command=lambda: self.apply_selection("erase"),
                                     bg="white", activebackground="#f1f4f8", fg=INK, bd=0,
                                     font=("Microsoft YaHei UI", 11), cursor="hand2", padx=14, pady=8)
        self.erase_button.pack(side="left")
        self.copy_menu = tk.Menu(self.canvas, tearoff=False, font=("Microsoft YaHei UI", 10))
        self.copy_menu.add_command(label="复制", accelerator="Ctrl+C", command=self.copy_selection)
        self.floating_id = None
        self._update_controls()
        self._empty_canvas()

    def _shortcuts(self):
        for key, callback in [("<Control-o>", self.open_file), ("<Control-s>", self.save),
                              ("<Control-Shift-S>", self.save_as),
                              ("<Control-c>", self.copy_selection),
                              ("<Control-z>", self.undo), ("<Control-y>", self.redo)]:
            self.root.bind(key, lambda event, fn=callback: (fn(), "break")[-1])
        self.root.bind("<Escape>", lambda e: self._clear_selection())
        self.root.bind("<Prior>", lambda e: self.change_page(self.page - 1))
        self.root.bind("<Next>", lambda e: self.change_page(self.page + 1))

    def _empty_canvas(self):
        self.canvas.delete("all")
        self.canvas.create_text(380, 200, text="拖入 PDF，划选文字", fill=INK, font=("Microsoft YaHei UI", 21))
        self.canvas.create_text(380, 250, text="划选后可复制、高亮或擦除\n拖入文件或 Ctrl+O 打开 · Ctrl+C 复制", fill=MUTED,
                                font=("Microsoft YaHei UI", 11), justify="center")

    def _enable_file_drop(self):
        try:
            self.file_drop = WindowsFileDrop(self.root, self.open_dropped_files)
        except (OSError, RuntimeError, tk.TclError):
            self.status.set("拖拽打开暂不可用，请按 Ctrl+O 打开 PDF。")

    def open_dropped_files(self, paths):
        if self.busy:
            return
        if len(paths) != 1:
            self.status.set("请一次拖入一份 PDF 文件。")
            return
        path = Path(paths[0])
        if path.suffix.lower() != ".pdf" or not path.is_file():
            self.status.set("请拖入有效的 PDF 文件。")
            return
        self.open_path(path)

    def copy_selection(self, event=None):
        if self.busy:
            return "break"
        text = (self.selection or {}).get("text", "")
        if not text:
            return "break"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set(f"已复制 {len(text)} 个字符。")
        except tk.TclError as exc:
            self.status.set(f"复制失败：{exc}")
        return "break"

    def _show_copy_menu(self, event):
        if self.busy or not self.selection or not self.selection.get("text"):
            return "break"
        try:
            self.copy_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.copy_menu.grab_release()
        return "break"

    def open_file(self):
        if self.busy:
            return
        self._clear_selection()
        path = filedialog.askopenfilename(title="打开 PDF", filetypes=[("PDF 文档", "*.pdf")])
        if path:
            self.open_path(path)

    def _token(self):
        return json.dumps(self.actions, sort_keys=True, separators=(",", ":"))

    def _discard_allowed(self):
        if self._token() != self.saved_token:
            return messagebox.askyesno("还有未保存的修改", "这些修改还没有保存。要放弃修改吗？", parent=self.root)
        return True

    def open_path(self, path):
        if self.busy or not self._discard_allowed():
            return
        self._clear_selection()
        new_pdf = None
        try:
            try:
                new_pdf = PdfDocument(str(path))
            except Exception as exc:
                if any(s in str(exc).lower() for s in ["password", "encrypted", "密码", "加密"]):
                    password = simpledialog.askstring("文档密码", "请输入此 PDF 的打开密码：", show="*", parent=self.root)
                    if password is None:
                        return
                    new_pdf = PdfDocument(str(path), password=password)
                else:
                    raise
            if self.pdf:
                self.pdf.close()
            self.pdf = new_pdf
            self.page, self.actions, self.undo_stack, self.redo_stack = 0, [], [], []
            self.saved_token = self._token()
            self.filename.configure(text=Path(path).name)
            self.root.title(f"{Path(path).name} · {APP_NAME}")
            self.fit = True
            self.render()
            if self.selector is not None and self.selector.has_text:
                if self.pdf.needs_highlight_upgrade:
                    self.status.set("旧版高亮已显示为平直边缘；Ctrl+S 保存此样式到原文件。")
                else:
                    self.status.set("划选文字后可复制或标记；Ctrl+C 复制，Ctrl+S 保存。")
        except Exception as exc:
            if new_pdf and new_pdf is not self.pdf:
                new_pdf.close()
            self._error("打开失败", exc)

    def change_page(self, index):
        if not self.pdf or self.busy or not 0 <= index < self.pdf.page_count or index == self.page:
            return
        self.page = index
        self.render()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def render(self):
        if not self.pdf:
            return
        self._clear_selection()
        self.base_image = self.selector = None
        try:
            w, h = self.pdf.page_size(self.page)
            if self.fit:
                self.scale = max(.15, min(3, (self.canvas.winfo_width() - 52) / w))
            self.scale = min(self.scale, 5000 / max(w, h))
            if any(a["page"] == self.page for a in self.actions):
                png, _, _ = self.pdf.render_preview(self.page, self.scale, self.actions)
            else:
                png, _, _ = self.pdf.render_page(self.page, self.scale)
            self.base_image = Image.open(io.BytesIO(png)).convert("RGB")
            self.selector = TextSelector(self.pdf, self.page, self.actions)
            self.photo = ImageTk.PhotoImage(self.base_image)
            self.canvas.delete("all")
            iw, ih = self.base_image.size
            self.origin = (max(20, (self.canvas.winfo_width() - iw) / 2), 20)
            ox, oy = self.origin
            self.canvas.create_rectangle(ox + 3, oy + 4, ox + iw + 3, oy + ih + 4, fill="#d5dce4", outline="")
            self.canvas.create_image(ox, oy, image=self.photo, anchor="nw")
            self.canvas.configure(scrollregion=(0, 0, max(iw + 40, self.canvas.winfo_width()), ih + 40))
            self.canvas.configure(cursor="xterm" if self.selector.has_text else "arrow")
            if not self.selector.has_text:
                self.status.set("这一页没有可选择的文字层。")
            self._update_controls()
        except Exception as exc:
            self.base_image = self.photo = self.selector = None
            self.canvas.delete("all")
            self.canvas.create_text(max(200, self.canvas.winfo_width() // 2), 150, text="这一页暂时无法显示，请尝试其他页面。",
                                    fill=MUTED, font=("Microsoft YaHei UI", 12))
            self._error("页面显示失败", exc)

    def _point(self, event):
        x = (self.canvas.canvasx(event.x) - self.origin[0]) / self.scale
        y = (self.canvas.canvasy(event.y) - self.origin[1]) / self.scale
        w, h = self.pdf.page_size(self.page)
        return max(0, min(w, x)), max(0, min(h, y))

    def _start_selection(self, event):
        if self.busy or not self.pdf or self.selector is None:
            return
        self._clear_selection()
        self.canvas.focus_set()
        if not self.selector.has_text:
            self.status.set("这一页没有可选择的文字层。")
            return
        point = self._point(event)
        if self.selector.closest_character(point, max_distance=12) is None:
            return
        self.drag_start = point
        self.drag_pixel = (event.x, event.y)

    def _drag_selection(self, event):
        if self.busy or self.drag_start is None:
            return
        self.pending_drag_end = self._point(event)
        if self.selection_after is None:
            self.selection_after = self.root.after(20, self._flush_drag)

    def _flush_drag(self):
        self.selection_after = None
        if self.drag_start is None or self.pending_drag_end is None:
            return
        self.selection = self.selector.select(self.drag_start, self.pending_drag_end)
        self._draw_selection()

    def _end_selection(self, event):
        if self.drag_start is None:
            return
        if self.selection_after is not None:
            self.root.after_cancel(self.selection_after)
            self.selection_after = None
        dx, dy = event.x - self.drag_pixel[0], event.y - self.drag_pixel[1]
        if dx * dx + dy * dy < 9:
            self._clear_selection()
            return
        self.selection = self.selector.select(self.drag_start, self._point(event))
        self.drag_start = self.drag_pixel = self.pending_drag_end = None
        self._draw_selection()
        if self.selection and self.selection.get("quads"):
            self._show_floating_bar()

    def _draw_selection(self):
        self.canvas.delete("text_selection")
        self.selection_photos.clear()
        if not self.selection or self.base_image is None:
            return
        iw, ih = self.base_image.size
        for quad in self.selection["quads"]:
            points = [(quad[i] * self.scale, quad[i + 1] * self.scale) for i in (0, 2, 6, 4)]
            left = max(0, int(min(p[0] for p in points)))
            top = max(0, int(min(p[1] for p in points)))
            right = min(iw, int(max(p[0] for p in points)) + 2)
            bottom = min(ih, int(max(p[1] for p in points)) + 2)
            if right <= left or bottom <= top:
                continue
            crop = self.base_image.crop((left, top, right, bottom)).convert("RGBA")
            tint = Image.new("RGBA", crop.size)
            ImageDraw.Draw(tint).polygon([(x - left, y - top) for x, y in points], fill=(51, 132, 232, 75))
            photo = ImageTk.PhotoImage(Image.alpha_composite(crop, tint))
            self.selection_photos.append(photo)
            self.canvas.create_image(self.origin[0] + left, self.origin[1] + top, image=photo, anchor="nw", tags="text_selection")

    def _show_floating_bar(self):
        self._hide_floating_bar()
        self.floating_bar.update_idletasks()
        bw, bh = self.floating_bar.winfo_reqwidth(), self.floating_bar.winfo_reqheight()
        left, top, right, bottom = self.selection["bounds"]
        x = self.origin[0] + left * self.scale
        y = self.origin[1] + top * self.scale - bh - 9
        visible_left, visible_top = self.canvas.canvasx(0), self.canvas.canvasy(0)
        x = max(visible_left + 8, min(x, visible_left + self.canvas.winfo_width() - bw - 8))
        if y < visible_top + 6:
            y = self.origin[1] + bottom * self.scale + 9
        y = max(visible_top + 6, min(y, visible_top + self.canvas.winfo_height() - bh - 6))
        self.floating_id = self.canvas.create_window(x, y, window=self.floating_bar, anchor="nw", tags="floating_bar")

    def _hide_floating_bar(self):
        self.canvas.delete("floating_bar")
        self.floating_id = None

    def _clear_selection(self):
        if self.selection_after is not None:
            self.root.after_cancel(self.selection_after)
            self.selection_after = None
        self.drag_start = self.drag_pixel = self.pending_drag_end = self.selection = None
        self._hide_floating_bar()
        self.canvas.delete("text_selection")
        self.selection_photos.clear()

    def apply_selection(self, kind):
        if self.busy or not self.selection:
            return
        chosen = self.selection
        if kind in HIGHLIGHT_STYLES:
            batch = [{"kind": "highlight", "style": kind, "page": self.page, "rect": chosen["bounds"], "quads": chosen["quads"]}]
        elif kind == "erase":
            batch = [{"kind": "erase", "page": self.page, "rect": rect, "text_only": True} for rect in chosen["rects"]]
        else:
            return
        if not batch:
            return
        self.actions.extend(batch)
        self.undo_stack.append(batch)
        self.redo_stack.clear()
        self.render()
        label = HIGHLIGHT_STYLES[kind]["label"] if kind in HIGHLIGHT_STYLES else "擦除"
        self.status.set(f"已{label} · Ctrl+Z 撤销，Ctrl+S 保存。")

    def undo(self):
        if self.busy or not self.undo_stack:
            return
        batch = self.undo_stack.pop()
        del self.actions[-len(batch):]
        self.redo_stack.append(batch)
        self.page = batch[0]["page"]
        self.render()
        self.status.set("已撤销 · Ctrl+S 保存。")

    def redo(self):
        if self.busy or not self.redo_stack:
            return
        batch = self.redo_stack.pop()
        self.actions.extend(batch)
        self.undo_stack.append(batch)
        self.page = batch[0]["page"]
        self.render()
        self.status.set("已重做 · Ctrl+S 保存。")

    def _update_controls(self):
        self.file_menu.entryconfigure(0, state="disabled" if self.busy else "normal")
        self.file_menu.entryconfigure(1, state="normal" if self.pdf and not self.busy else "disabled")
        self.file_menu.entryconfigure(2, state="normal" if self.pdf and not self.busy else "disabled")
        self.prev_button.configure(state="normal" if self.pdf and self.page > 0 and not self.busy else "disabled")
        self.next_button.configure(state="normal" if self.pdf and self.page < self.pdf.page_count - 1 and not self.busy else "disabled")
        self.page_label.configure(text=f"{self.page + 1} / {self.pdf.page_count}" if self.pdf else "—")

    def _vscroll(self, *args):
        self._clear_selection()
        self.canvas.yview(*args)

    def _hscroll(self, *args):
        self._clear_selection()
        self.canvas.xview(*args)

    def _wheel(self, event):
        self._clear_selection()
        if event.state & 0x4 and self.pdf and not self.busy:
            self.fit = False
            self.scale = max(.15, min(4, self.scale * (1.12 if event.delta > 0 else 1 / 1.12)))
            self.render()
        else:
            self.canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
        return "break"

    def _pan_start(self, event):
        self._clear_selection()
        self.canvas.scan_mark(event.x, event.y)

    def _resize(self, event):
        if not self.pdf:
            return
        if self.resize_after is not None:
            self.root.after_cancel(self.resize_after)
        self.resize_after = self.root.after(180, lambda: self.render() if not self.busy else None)

    def save(self):
        if not self.pdf or self.busy:
            return
        self._clear_selection()
        if self._token() == self.saved_token and not self.pdf.needs_highlight_upgrade:
            self.status.set("所有修改均已保存。")
            return
        self._start_save(self.pdf.path, in_place=True)

    def save_as(self):
        if not self.pdf or self.busy:
            return
        self._clear_selection()
        source = Path(self.pdf.path)
        output = filedialog.asksaveasfilename(title="另存为", initialdir=str(source.parent), initialfile=source.stem + "-已编辑.pdf",
                                             defaultextension=".pdf", filetypes=[("PDF 文档", "*.pdf")])
        if not output:
            return
        if os.path.normcase(os.path.realpath(output)) == os.path.normcase(os.path.realpath(source)):
            self.save()
            return
        self._start_save(output, in_place=False)

    def _start_save(self, output, in_place):
        self.busy = True
        self._update_controls()
        self.status.set("正在保存……")
        actions = json.loads(json.dumps(self.actions))
        token, pdf = self._token(), self.pdf

        def work():
            try:
                report = pdf.save_in_place(actions, optimize_images=False) if in_place else pdf.export(output, actions, optimize_images=False)
                self.result_queue.put(("saved", output, report, token, in_place))
            except Exception as exc:
                self.result_queue.put(("error", "保存失败", str(exc), traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            result = self.result_queue.get_nowait()
        except queue.Empty:
            result = None
        if result:
            self.busy = False
            self._update_controls()
            if result[0] == "error":
                self._error(result[1], RuntimeError(result[2]), result[3])
            else:
                _, path, report, token, in_place = result
                if in_place:
                    self.saved_token = token
                before, after = report["source_bytes"], report["output_bytes"]
                label = "已保存" if in_place else "已另存副本"
                self.status.set(f"{label}：{Path(path).name} · {size_text(before)} → {size_text(after)}")
        self.root.after(120, self._poll)

    def _error(self, title, error, detail=None):
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            with (DATA_ROOT / "error.log").open("a", encoding="utf-8") as log:
                log.write(f"{title}: {error}\n{detail or traceback.format_exc()}\n")
        except OSError:
            pass
        self.status.set(f"{title}：{error}")
        messagebox.showerror(title, str(error), parent=self.root)

    def close(self):
        if self.busy:
            return
        if self._discard_allowed():
            if self.file_drop:
                self.file_drop.close()
            if self.pdf:
                self.pdf.close()
            self.root.destroy()


def run_self_test(report_path):
    """Exercise the bundled editor without relying on a system Python install."""
    import hashlib
    import tempfile
    import pymupdf as fitz
    import PIL

    report = {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
              "pymupdf": fitz.VersionBind, "pillow": PIL.__version__, "checks": {}, "ok": False}
    check = report["checks"]
    root = drop_probe = None
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-clean-selftest-") as folder:
            source, output = Path(folder) / "input.pdf", Path(folder) / "output.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=330)
            page.draw_rect(fitz.Rect(30, 35, 550, 105), color=None, fill=(.9, .95, 1))
            page.insert_text((45, 75), "ALPHA secret KEEP", fontsize=18)
            page.insert_text((45, 150), "Highlight this sentence.", fontsize=15)
            keep_before = tuple(page.search_for("KEEP")[0])
            secret = page.search_for("secret")[0]
            high = page.search_for("Highlight")[0]
            doc.save(source)
            doc.close()
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            with PdfDocument(source) as pdf:
                selector = TextSelector(pdf, 0)
                chosen = selector.select((secret.x0 + 1, (secret.y0 + secret.y1) / 2),
                                         (secret.x1 - 1, (secret.y0 + secret.y1) / 2))
                check["text_selection"] = bool(chosen and chosen["text"] == "secret")
                highlighted = selector.select((high.x0 + 1, (high.y0 + high.y1) / 2),
                                              (high.x1 - 1, (high.y0 + high.y1) / 2))
                actions = [{"kind": "erase", "page": 0, "rect": r, "text_only": True} for r in chosen["rects"]]
                actions.append({"kind": "highlight", "page": 0, "rect": highlighted["bounds"], "quads": highlighted["quads"]})
                preview, _, _ = pdf.render_preview(0, 1.25, actions)
                pdf.export(output, actions)
            with fitz.open(output) as reopened:
                page = reopened[0]
                check["real_erasure"] = "secret" not in page.get_text()
                check["adjacent_position_preserved"] = max(abs(a - b) for a, b in zip(keep_before, page.search_for("KEEP")[0])) < .02
                check["yellow_highlight"] = any(a.type[0] == 8 and a.colors["stroke"] == [1., 1., 0.] for a in page.annots() or [])
                check["background_preserved"] = bool(page.get_drawings())
                rendered = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), colorspace=fitz.csRGB, alpha=False)
                before_image = Image.open(io.BytesIO(preview)).convert("RGB")
                after_image = Image.frombytes("RGB", (rendered.width, rendered.height), rendered.samples)
                difference = ImageChops.difference(before_image, after_image)
                max_delta = max(upper for _, upper in difference.getextrema())
                histogram = difference.histogram()
                mean_delta = sum((i % 256) * count for i, count in enumerate(histogram)) / (rendered.width * rendered.height * 3)
                report["preview_pixel_metrics"] = {"max_channel_delta": max_delta, "mean_absolute_channel_delta": mean_delta}
                # PDF serialization can change antialiasing/color rounding by
                # 1-2 intensity levels; text positions are checked separately.
                check["preview_matches_output"] = max_delta <= 2 and mean_delta <= .01
            check["export_preserves_original"] = hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
            with PdfDocument(source) as pdf:
                styled = list(actions)
                for needle, style in (("this", "confused"), ("sentence.", "useful")):
                    region = pdf.doc[0].search_for(needle)[0]
                    styled.append({"kind": "highlight", "style": style, "page": 0,
                                   "rect": list(region), "quads": pdf.text_quads(0, region)})
                pdf.save_in_place(styled)
                with fitz.open(source) as reopened:
                    page = reopened[0]
                    check["in_place_save"] = "secret" not in page.get_text() and hashlib.sha256(source.read_bytes()).hexdigest() != original_hash
                    annotations = list(page.annots() or [])
                    check["reading_highlight_styles"] = all(
                        any(a.info.get("subject") == HIGHLIGHT_STYLES[key]["label"]
                            and max(abs(c - e) for c, e in zip(a.colors["stroke"], HIGHLIGHT_STYLES[key]["color"])) < .001
                            for a in annotations)
                        for key in ("confused", "useful"))
                    scale = 3
                    with_marks = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=True)
                    without_marks = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=False)

                    def changed_at(x, y):
                        px, py = int(x * scale), int(y * scale)
                        return with_marks.pixel(px, py) != without_marks.pixel(px, py)

                    flat_checks = []
                    for annot in annotations:
                        if annot.type[0] != fitz.PDF_ANNOT_HIGHLIGHT:
                            continue
                        region = fitz.Quad(annot.vertices[:4]).rect
                        # Flat fill reaches all four inset corners, with no
                        # curved cap bleeding beyond either text endpoint.
                        flat_checks.extend(changed_at(x, y) for x in (region.x0 + 1, region.x1 - 1)
                                           for y in (region.y0 + 1, region.y1 - 1))
                        flat_checks.extend(not changed_at(x, region.y0 + region.height * fraction)
                                           for x in (region.x0 - 1, region.x1 + 1)
                                           for fraction in (.15, .5, .85))
                    check["flat_highlight_ends"] = bool(flat_checks) and all(flat_checks)
                pdf.save_in_place(styled)
                with fitz.open(source) as reopened:
                    check["repeat_save_no_duplicates"] = sum(a.type[0] == 8 for a in reopened[0].annots() or []) == 3
                pdf.save_in_place(styled[:-1])
                with fitz.open(source) as reopened:
                    undone = sum(a.type[0] == 8 for a in reopened[0].annots() or []) == 2
                pdf.save_in_place(styled)
                with fitz.open(source) as reopened:
                    check["undo_redo_after_save"] = undone and sum(a.type[0] == 8 for a in reopened[0].annots() or []) == 3
            check["pinned_pdf_engine"] = fitz.VersionBind == "1.26.7"
            root = tk.Tk()
            root.withdraw()
            root.iconbitmap(str(ROOT / "editor.ico"))
            check["tk_runtime"] = bool(root.tk.call("info", "patchlevel"))
            check["image_runtime"] = bool(ImageTk.PhotoImage(Image.open(io.BytesIO(preview))))
            root.update_idletasks()
            drop_probe = WindowsFileDrop(root, lambda paths: None)
            check["file_drop_runtime"] = drop_probe.enabled
            drop_probe.close()

            class MemoryClipboard:
                """Check the copy action without touching the user's clipboard."""
                text = ""

                def clipboard_clear(self):
                    self.text = ""

                def clipboard_append(self, text):
                    self.text += text

            copy_probe = Editor.__new__(Editor)
            copy_probe.root = MemoryClipboard()
            copy_probe.busy = False
            copy_probe.selection = {"text": "中文复制\nSelected PDF text"}
            copy_probe.status = tk.StringVar(master=root)
            copy_probe.copy_selection()
            check["copy_selected_text"] = copy_probe.root.text == copy_probe.selection["text"]
            if report["frozen"]:
                check["bundled_pdf_engine"] = Path(fitz.__file__).resolve().is_relative_to(ROOT.resolve())
                check["bundled_pillow"] = Path(PIL.__file__).resolve().is_relative_to(ROOT.resolve())
                check["bundled_licenses"] = (ROOT / "licenses").is_dir()
            report["ok"] = all(check.values())
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if drop_probe:
            drop_probe.close()
        if root:
            root.destroy()
        report_file = Path(report_path).resolve()
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def main():
    import argparse
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("file", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report", default="pdf-clean-editor-self-test.json")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test(args.report)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Editor(root, args.file)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
