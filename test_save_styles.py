"""v1.1 integration QA: atomic in-place saves and persistent highlight styles.

Every mutation targets a newly generated fixture in qa/. No user PDF is used.
Run with the pinned PyMuPDF 1.26.7 environment.
"""
from __future__ import annotations

import ctypes
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
# Source-tree execution must prefer the tested engine over the obsolete vendor.
if (ROOT / 'vendor126').is_dir():
    sys.path.insert(0, str(ROOT / 'vendor126'))
import pymupdf as fitz
from PIL import Image

QA = ROOT / 'qa'
SECRET = 'SECRET-123'
WORDS = {'highlight': 'YELLOW WORDS', 'confused': 'PURPLE WORDS', 'useful': 'GREEN WORDS'}
LABELS = {'highlight': '高亮', 'confused': '困惑', 'useful': '有用'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_pdf(path, external=False):
    doc = fitz.open()
    page = doc.new_page(width=620, height=470)
    page.draw_rect(fitz.Rect(30, 34, 585, 120), color=None, fill=(.88, .95, 1))
    page.insert_text((45, 79), 'ALPHA ' + SECRET + ' KEEP NEIGHBOR', fontsize=18)
    for y, text in zip((170, 225, 280), WORDS.values()):
        page.insert_text((45, y), text, fontsize=18)
    page.insert_text((45, 390), 'EXTERNAL OWNER' if external else 'PRESERVE THE ORIGINAL LAYOUT', fontsize=14)
    scan = doc.new_page(width=620, height=300)
    image = Image.new('RGB', (620, 300), (236, 242, 246))
    data = io.BytesIO()
    image.save(data, format='PNG')
    scan.insert_image(scan.rect, stream=data.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def annotation_records(path, page_index=0):
    with fitz.open(path) as doc:
        page = doc[page_index]  # Keep the owning page alive while reading annots.
        return [{'type': annot.type[1], 'subject': annot.info.get('subject', ''),
                 'content': annot.info.get('content', ''), 'colors': annot.colors,
                 'opacity': annot.opacity} for annot in page.annots() or []]


class InPlaceSaveAndStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pdf_engine import PdfDocument, PdfEngineError, HIGHLIGHT_STYLES
        cls.PdfDocument = staticmethod(PdfDocument)
        cls.PdfEngineError = PdfEngineError
        cls.styles = HIGHLIGHT_STYLES
        QA.mkdir(exist_ok=True)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='save-styles-', dir=QA)
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.path = self.directory / '原件 测试.pdf'
        make_pdf(self.path)
        self.original_hash = sha(self.path)
        self.pdf = self.PdfDocument(self.path)
        self.addCleanup(self.pdf.close)

    def highlight(self, style='highlight', include_style=True):
        with fitz.open(self.path) as doc:
            area = doc[0].search_for(WORDS[style])[0]
        action = {'kind': 'highlight', 'page': 0, 'rect': list(area),
                  'quads': self.pdf.text_quads(0, list(area))}
        if include_style:
            action['style'] = style
        return action

    def erasure(self):
        from text_selection import TextSelector
        with fitz.open(stream=self.pdf._source_data, filetype='pdf') as doc:
            area = doc[0].search_for(SECRET)[0]
        selection = TextSelector(self.pdf, 0).select((area.x0 + .2, area.y0 + area.height / 2),
                                                    (area.x1 - .2, area.y0 + area.height / 2))
        self.assertEqual(selection['text'], SECRET)
        return [{'kind': 'erase', 'page': 0, 'rect': rect, 'text_only': True}
                for rect in selection['rects']]

    def directory_state(self):
        return {p.name: (p.stat().st_size, sha(p)) for p in self.directory.iterdir() if p.is_file()}

    def assert_colors_and_labels(self, records, styles):
        self.assertEqual(len(records), len(styles))
        by_subject = {r['subject']: r for r in records}
        self.assertEqual(set(by_subject), {LABELS[s] for s in styles})
        for style in styles:
            record = by_subject[LABELS[style]]
            self.assertEqual(record['content'], LABELS[style])
            self.assertEqual(record['type'], 'Highlight')
            for actual, expected in zip(record['colors']['stroke'], self.styles[style]['color']):
                self.assertAlmostEqual(actual, expected, places=5)

    def assert_actual_secret_erased(self):
        with fitz.open(self.path) as doc:
            page = doc[0]
            self.assertNotIn(SECRET, page.get_text())
            self.assertIn('KEEP NEIGHBOR', page.get_text())
            data = b''.join(doc.xref_stream(x) for x in range(1, doc.xref_length()) if doc.xref_is_stream(x))
            self.assertNotIn(SECRET.encode(), data)
            self.assertNotIn(SECRET.encode().hex().encode(), data.lower())
        self.assertEqual(self.path.read_bytes().count(b'%%EOF'), 1, 'Save rewrites rather than retaining prior revisions')

    def test_01_save_updates_exact_original_and_repeated_actions_do_not_duplicate(self):
        action = self.highlight(include_style=False)  # Existing yellow actions remain compatible.
        result = self.pdf.save_in_place([action])
        self.assertEqual(Path(result['output_path']).resolve(), self.path.resolve())
        self.assertNotEqual(sha(self.path), self.original_hash)
        self.assertEqual(result['output_bytes'], self.path.stat().st_size)
        self.assert_colors_and_labels(annotation_records(self.path), ['highlight'])
        for _ in range(2):
            self.pdf.save_in_place([action])
            self.assert_colors_and_labels(annotation_records(self.path), ['highlight'])
        self.assertEqual(set(p.name for p in self.directory.iterdir()), {self.path.name})
        # Export retains its original safer copy-only contract.
        with self.assertRaises(self.PdfEngineError):
            self.pdf.export(self.path, [action])

    def test_02_save_then_continue_editing_replays_full_queue_once(self):
        yellow, purple, green = [self.highlight(s) for s in ('highlight', 'confused', 'useful')]
        self.pdf.save_in_place([yellow])
        actions = [yellow, purple] + self.erasure()
        self.pdf.save_in_place(actions)
        self.assert_colors_and_labels(annotation_records(self.path), ['highlight', 'confused'])
        self.assert_actual_secret_erased()
        actions.append(green)
        self.pdf.save_in_place(actions)
        self.assert_colors_and_labels(annotation_records(self.path), ['highlight', 'confused', 'useful'])
        self.assert_actual_secret_erased()
        with self.PdfDocument(self.path) as reopened:
            self.assertNotIn(SECRET, reopened.doc[0].get_text())
            copied = self.directory / 'reopened-copy.pdf'
            reopened.export(copied, [])
        self.assert_colors_and_labels(annotation_records(copied), ['highlight', 'confused', 'useful'])

    def test_03_save_then_undo_and_redo_resaves_against_initial_snapshot(self):
        highlight = self.highlight('confused')
        erased = self.erasure()
        original_neighbor = tuple(self.pdf.doc[0].search_for('KEEP NEIGHBOR')[0])
        full = [highlight] + erased
        self.pdf.save_in_place(full)
        self.assert_actual_secret_erased()
        # Undo the erasure after it was saved, and save the shorter history.
        self.pdf.save_in_place([highlight])
        with fitz.open(self.path) as doc:
            self.assertIn(SECRET, doc[0].get_text())
        self.assert_colors_and_labels(annotation_records(self.path), ['confused'])
        self.pdf.save_in_place([])  # Undo the saved highlight as well.
        self.assertEqual(annotation_records(self.path), [])
        with fitz.open(self.path) as doc:
            self.assertIn(SECRET, doc[0].get_text())
        self.pdf.save_in_place(full)  # Redo both, with no duplicate annotations.
        self.assert_actual_secret_erased()
        self.assert_colors_and_labels(annotation_records(self.path), ['confused'])
        with fitz.open(self.path) as doc:
            for actual, expected in zip(doc[0].search_for('KEEP NEIGHBOR')[0], original_neighbor):
                self.assertAlmostEqual(actual, expected, delta=.02)
            self.assertTrue(doc[0].get_drawings(), 'Text-only erasure preserves the background')

    def test_04_palette_semantics_and_all_categories_persist_on_text_and_scan(self):
        self.assertEqual(set(self.styles), {'highlight', 'confused', 'useful'})
        self.assertEqual(tuple(self.styles['highlight']['color']), (1, 1, 0))
        purple = self.styles['confused']['color']
        green = self.styles['useful']['color']
        self.assertGreater(purple[0], purple[1])
        self.assertGreater(purple[2], purple[1])
        self.assertGreater(green[1], green[0])
        self.assertGreater(green[1], green[2])
        actions = [self.highlight(s) for s in WORDS]
        for i, style in enumerate(WORDS):
            actions.append({'kind': 'highlight', 'style': style, 'page': 1,
                            'rect': [30 + 180 * i, 70, 175 + 180 * i, 125], 'quads': []})
        self.pdf.save_in_place(actions)
        self.assert_colors_and_labels(annotation_records(self.path), list(WORDS))
        scan = annotation_records(self.path, 1)
        self.assertEqual(len(scan), 3)
        scan_by_subject = {r['subject']: r for r in scan}
        for style in WORDS:
            record = scan_by_subject[LABELS[style]]
            self.assertEqual(record['content'], LABELS[style])
            self.assertEqual(record['type'], 'Square')
            for actual, expected in zip(record['colors']['fill'], self.styles[style]['color']):
                self.assertAlmostEqual(actual, expected, places=5)
        invalid = dict(actions[0], style='pink-unknown')
        before = self.directory_state()
        with self.assertRaises(self.PdfEngineError):
            self.pdf.save_in_place([invalid])
        self.assertEqual(self.directory_state(), before)

    def test_05_external_modification_is_rejected_without_overwriting_or_temp_leaks(self):
        action = self.highlight()
        # An external writer changes bytes after the editor loaded its snapshot.
        self.path.write_bytes(self.path.read_bytes() + b'\n% external editor change\n')
        before = self.directory_state()
        with self.assertRaises(self.PdfEngineError):
            self.pdf.save_in_place([action])
        self.assertEqual(self.directory_state(), before)

    def test_06_external_replacement_and_deletion_are_rejected(self):
        action = self.highlight()
        external = self.directory / 'external-replacement.pdf'
        make_pdf(external, external=True)
        os.replace(external, self.path)
        before = self.directory_state()
        with self.assertRaises(self.PdfEngineError):
            self.pdf.save_in_place([action])
        self.assertEqual(self.directory_state(), before)
        self.path.unlink()  # This is this test's generated fixture only.
        before = self.directory_state()
        with self.assertRaises(self.PdfEngineError):
            self.pdf.save_in_place([action])
        self.assertFalse(self.path.exists(), 'Refusing a missing target must not silently recreate it')
        self.assertEqual(self.directory_state(), before)

    def test_07_atomic_replace_failure_protects_original_cleans_temp_and_can_retry(self):
        action = self.highlight('useful')
        before = self.directory_state()
        with mock.patch('pdf_engine.os.replace', side_effect=PermissionError('QA simulated replace failure')):
            with self.assertRaises(self.PdfEngineError):
                self.pdf.save_in_place([action])
        self.assertEqual(self.directory_state(), before, 'Failed saves must not damage originals or leak temp files')
        self.assertEqual(sha(self.path), self.original_hash)
        self.pdf.save_in_place([action])
        self.assert_colors_and_labels(annotation_records(self.path), ['useful'])

    @unittest.skipUnless(sys.platform == 'win32', 'Actual Windows file-share lock test')
    def test_08_real_windows_lock_keeps_original_and_cleans_temp(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        # Share reads, but deny writes/deletion so atomic replacement must fail.
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        before = self.directory_state()
        try:
            with self.assertRaises(self.PdfEngineError):
                self.pdf.save_in_place([self.highlight()])
            self.assertEqual(self.directory_state(), before)
        finally:
            kernel.CloseHandle(handle)
        self.pdf.save_in_place([self.highlight()])
        self.assert_colors_and_labels(annotation_records(self.path), ['highlight'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
