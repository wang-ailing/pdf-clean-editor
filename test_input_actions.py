"""Headless QA for opening dropped PDFs and copying selected text.

The Tk window is never created, and every clipboard call targets a mock.
Generated PDFs live in an automatically cleaned temporary directory.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
if (ROOT / "vendor126").is_dir():
    sys.path.insert(0, str(ROOT / "vendor126"))

import pymupdf as fitz

from app import Editor
from pdf_engine import PdfDocument
from text_selection import TextSelector


def make_pdf(path):
    with fitz.open() as doc:
        page = doc.new_page(width=620, height=360)
        page.insert_text((35, 75), "KEEP ALPHA SECRET-123 OMEGA END", fontsize=18)
        page.insert_text((35, 150), "中文内容可以复制", fontname="china-s", fontsize=18)
        page.insert_text((35, 190), "第二行保留换行", fontname="china-s", fontsize=18)
        doc.save(path, garbage=4, deflate=True)


def endpoints(page, text):
    bounds = page.search_for(text)[0]
    middle = (bounds.y0 + bounds.y1) / 2
    return (bounds.x0 + .2, middle), (bounds.x1 - .2, middle)


class InputActionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdf-input-qa-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.pdf_path = self.folder / "中文 文件名称.PDF"
        make_pdf(self.pdf_path)
        self.editor = Editor.__new__(Editor)
        self.editor.root = mock.Mock()
        self.editor.status = mock.Mock()
        self.editor.busy = False
        self.editor.selection = None
        self.editor.actions = [{"kind": "highlight", "page": 0, "rect": [1, 2, 3, 4]}]
        self.editor.undo_stack = [[{"kind": "erase", "page": 0, "rect": [4, 5, 6, 7]}]]
        self.editor.redo_stack = [[{"kind": "highlight", "page": 0, "rect": [7, 8, 9, 10]}]]
        self.editor.saved_token = "[]"

    def snapshot_edit_state(self):
        return copy.deepcopy((self.editor.actions, self.editor.undo_stack,
                              self.editor.redo_stack, self.editor.saved_token,
                              self.editor._token()))

    def test_01_copy_preserves_unicode_line_breaks_and_selection(self):
        chosen = {"text": "中文、é、α 与 emoji 🙂\n第二行\t保留空格  ",
                  "quads": [[1, 2, 3, 2, 1, 4, 3, 4]]}
        self.editor.selection = chosen
        edit_state = self.snapshot_edit_state()
        self.assertEqual(self.editor.copy_selection(), "break")
        self.assertEqual(self.editor.root.method_calls, [
            mock.call.clipboard_clear(), mock.call.clipboard_append(chosen["text"])])
        self.assertIs(self.editor.selection, chosen, "Copy must leave the selected range available for marking")
        self.assertEqual(self.snapshot_edit_state(), edit_state)
        self.assertIn("已复制", self.editor.status.set.call_args.args[0])

    def test_02_empty_selection_leaves_clipboard_and_edit_state_untouched(self):
        state = self.snapshot_edit_state()
        for selection in (None, {}, {"text": ""}):
            with self.subTest(selection=selection):
                self.editor.selection = selection
                self.assertEqual(self.editor.copy_selection(), "break")
        self.editor.root.clipboard_clear.assert_not_called()
        self.editor.root.clipboard_append.assert_not_called()
        self.editor.status.set.assert_not_called()
        self.assertEqual(self.snapshot_edit_state(), state)

    def test_03_copy_while_busy_is_a_noop(self):
        self.editor.busy = True
        self.editor.selection = {"text": "pending changes"}
        state = self.snapshot_edit_state()
        self.assertEqual(self.editor.copy_selection(), "break")
        self.editor.root.clipboard_clear.assert_not_called()
        self.editor.root.clipboard_append.assert_not_called()
        self.editor.status.set.assert_not_called()
        self.assertEqual(self.snapshot_edit_state(), state)

    def test_04_clipboard_errors_leave_selection_and_document_edits_intact(self):
        self.editor.selection = {"text": "保留我的选择"}
        state = self.snapshot_edit_state()
        chosen = self.editor.selection
        for failed_method in ("clipboard_clear", "clipboard_append"):
            with self.subTest(failed_method=failed_method):
                self.editor.root.reset_mock(side_effect=True)
                self.editor.status.reset_mock()
                getattr(self.editor.root, failed_method).side_effect = tk.TclError("clipboard unavailable")
                self.assertEqual(self.editor.copy_selection(), "break")
                self.assertIn("复制失败", self.editor.status.set.call_args.args[0])
                self.assertIs(self.editor.selection, chosen)
                self.assertEqual(self.snapshot_edit_state(), state)

    def test_05_copy_uses_real_remaining_text_after_unsaved_erasure(self):
        original_hash = hashlib.sha256(self.pdf_path.read_bytes()).hexdigest()
        with PdfDocument(self.pdf_path) as pdf, fitz.open(self.pdf_path) as doc:
            page = doc[0]
            erase_start, erase_end = endpoints(page, "SECRET-123")
            start, _ = endpoints(page, "KEEP")
            _, end = endpoints(page, "END")
            erased = TextSelector(pdf, 0).select(erase_start, erase_end)
            actions = [{"kind": "erase", "page": 0, "rect": rect, "text_only": True}
                       for rect in erased["rects"]]
            self.editor.selection = TextSelector(pdf, 0, actions).select(start, end)
            self.assertIsNotNone(self.editor.selection)
            self.assertNotIn("SECRET-123", self.editor.selection["text"])
            self.assertIn("KEEP", self.editor.selection["text"])
            self.assertIn("OMEGA END", self.editor.selection["text"])
            self.editor.actions = actions
            state = self.snapshot_edit_state()
            self.editor.copy_selection()
            copied = self.editor.root.clipboard_append.call_args.args[0]
            self.assertEqual(copied, self.editor.selection["text"])
            self.assertNotIn("SECRET", copied)
            self.assertEqual(self.snapshot_edit_state(), state)
        self.assertEqual(hashlib.sha256(self.pdf_path.read_bytes()).hexdigest(), original_hash)

    def test_06_drop_accepts_existing_pdf_with_chinese_spaces_and_uppercase_suffix(self):
        self.editor.open_path = mock.Mock()
        self.editor.open_dropped_files((str(self.pdf_path),))
        self.editor.open_path.assert_called_once_with(self.pdf_path)
        self.editor.status.set.assert_not_called()

    def test_07_drop_rejects_missing_folder_and_unsupported_file(self):
        self.editor.open_path = mock.Mock()
        disguised_folder = self.folder / "这是目录.pdf"
        disguised_folder.mkdir()
        text_path = self.folder / "notes.txt"
        text_path.write_text("PDF should not open this", encoding="utf-8")
        for path in (self.folder / "不存在.pdf", disguised_folder, text_path):
            with self.subTest(path=path):
                self.editor.status.reset_mock()
                self.editor.open_dropped_files((str(path),))
                self.editor.open_path.assert_not_called()
                self.assertIn("有效的 PDF", self.editor.status.set.call_args.args[0])

    def test_08_drop_rejects_empty_or_multiple_without_replacing_document(self):
        self.editor.open_path = mock.Mock()
        state = self.snapshot_edit_state()
        for paths in ((), (str(self.pdf_path), str(self.pdf_path))):
            with self.subTest(paths=paths):
                self.editor.status.reset_mock()
                self.editor.open_dropped_files(paths)
                self.editor.open_path.assert_not_called()
                self.assertIn("一次拖入一份", self.editor.status.set.call_args.args[0])
        self.assertEqual(self.snapshot_edit_state(), state)

    def test_09_drop_while_busy_is_a_noop(self):
        self.editor.busy = True
        self.editor.open_path = mock.Mock()
        self.editor.open_dropped_files((str(self.pdf_path),))
        self.editor.open_path.assert_not_called()
        self.editor.status.set.assert_not_called()

    def test_10_drop_respects_real_unsaved_changes_guard(self):
        self.editor.pdf = mock.Mock()
        original = self.editor.pdf
        chosen = {"text": "existing selection"}
        self.editor.selection = chosen
        self.editor._clear_selection = mock.Mock()
        state = self.snapshot_edit_state()
        with mock.patch("app.messagebox.askyesno", return_value=False) as prompt, \
                mock.patch("app.PdfDocument") as constructor:
            self.editor.open_dropped_files((str(self.pdf_path),))
        prompt.assert_called_once()
        constructor.assert_not_called()
        original.close.assert_not_called()
        self.editor._clear_selection.assert_not_called()
        self.assertIs(self.editor.pdf, original)
        self.assertIs(self.editor.selection, chosen)
        self.assertEqual(self.snapshot_edit_state(), state)

    def test_11_corrupt_dropped_pdf_uses_existing_open_error_handling(self):
        broken = self.folder / "invalid.pdf"
        broken.write_bytes(b"this is not a PDF")
        self.editor.pdf = mock.Mock()
        original = self.editor.pdf
        self.editor.saved_token = self.editor._token()
        self.editor._clear_selection = mock.Mock()
        self.editor._error = mock.Mock()
        state = self.snapshot_edit_state()
        self.editor.open_dropped_files((str(broken),))
        self.editor._error.assert_called_once()
        self.assertEqual(self.editor._error.call_args.args[0], "打开失败")
        self.assertIs(self.editor.pdf, original)
        original.close.assert_not_called()
        self.assertEqual(self.snapshot_edit_state(), state)

    def test_12_ctrl_c_and_context_menu_reach_copy_without_a_live_window(self):
        self.editor.selection = {"text": "Ctrl+C 与右键"}
        self.editor._shortcuts()
        bindings = {args[0]: args[1] for args, _ in self.editor.root.bind.call_args_list}
        self.assertEqual(bindings["<Control-c>"](SimpleNamespace()), "break")
        self.editor.root.clipboard_append.assert_called_once_with("Ctrl+C 与右键")
        self.editor.copy_menu = mock.Mock()
        event = SimpleNamespace(x_root=123, y_root=456)
        self.assertEqual(self.editor._show_copy_menu(event), "break")
        self.editor.copy_menu.tk_popup.assert_called_once_with(123, 456)
        self.editor.copy_menu.grab_release.assert_called_once_with()
        self.editor.copy_menu.reset_mock()
        self.editor.selection = None
        self.assertEqual(self.editor._show_copy_menu(event), "break")
        self.editor.copy_menu.tk_popup.assert_not_called()

    def encrypted_pdf(self):
        target = self.folder / "加密文件.pdf"
        with fitz.open(self.pdf_path) as doc:
            doc.save(target, encryption=fitz.PDF_ENCRYPT_AES_256,
                     owner_pw="owner-password", user_pw="reading-password")
        return target

    def test_13_cancel_password_for_dropped_pdf_keeps_original_document(self):
        encrypted = self.encrypted_pdf()
        self.editor.pdf = mock.Mock()
        original = self.editor.pdf
        self.editor.saved_token = self.editor._token()
        self.editor._clear_selection = mock.Mock()
        self.editor._error = mock.Mock()
        state = self.snapshot_edit_state()
        with mock.patch("app.simpledialog.askstring", return_value=None) as prompt:
            self.editor.open_dropped_files((str(encrypted),))
        prompt.assert_called_once()
        self.editor._error.assert_not_called()
        self.assertIs(self.editor.pdf, original)
        original.close.assert_not_called()
        self.assertEqual(self.snapshot_edit_state(), state)

    def test_14_dropped_encrypted_pdf_opens_with_existing_password_flow(self):
        encrypted = self.encrypted_pdf()
        self.editor.pdf = mock.Mock()
        original = self.editor.pdf
        self.editor.saved_token = self.editor._token()
        self.editor._clear_selection = mock.Mock()
        self.editor._error = mock.Mock()
        self.editor.filename = mock.Mock()
        self.editor.render = mock.Mock()
        self.editor.selector = None
        with mock.patch("app.simpledialog.askstring", return_value="reading-password") as prompt:
            self.editor.open_dropped_files((str(encrypted),))
        prompt.assert_called_once()
        self.editor._error.assert_not_called()
        original.close.assert_called_once_with()
        self.assertIsInstance(self.editor.pdf, PdfDocument)
        self.addCleanup(self.editor.pdf.close)
        self.assertEqual(self.editor.pdf.page_count, 1)
        self.editor.render.assert_called_once_with()
        self.editor.filename.configure.assert_called_once_with(text=encrypted.name)
        self.assertEqual(self.editor.actions, [])
        self.assertEqual(self.editor.saved_token, self.editor._token())


if __name__ == "__main__":
    unittest.main(verbosity=2)
