"""Native text-selection integration QA; generated fixtures remain in qa/.

Endpoints are carets on the displayed page, not a rectangular text filter.
Run only after the parent authoring marker has completed successfully.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parent
if (ROOT / "vendor126").is_dir():
    sys.path.insert(0, str(ROOT / "vendor126"))
import pymupdf as fitz

QA = ROOT / "qa"
LEFT = [
    "ALPHA bravo charlie DELTA",
    "Second line has useful words.",
    "Third line stays fully selected.",
    "Fourth ending segment.",
]
RIGHT = [
    "RIGHT COLUMN MUST STAY",
    "Nothing from this side.",
    "RIGHT SIDE SECOND PARAGRAPH",
    "Do not include the right column.",
]


def norm(value):
    return re.sub(r"\s+", " ", value).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def endpoints(page, text):
    """Place start/end carets just inside the first/last glyphs, then rotate."""
    rect = page.search_for(text)[0]
    start = fitz.Point(rect.x0 + .2, (rect.y0 + rect.y1) / 2)
    end = fitz.Point(rect.x1 - .2, (rect.y0 + rect.y1) / 2)
    return tuple(start * page.rotation_matrix), tuple(end * page.rotation_matrix)


def create_selection_fixture():
    QA.mkdir(exist_ok=True)
    target = QA / "selection-source.pdf"
    doc = fitz.open()
    page = doc.new_page(width=650, height=390)
    page.draw_rect(fitz.Rect(35, 45, 325, 192), fill=(.88, .94, 1), color=(.4, .6, .8), width=2)
    # The selected word overlaps both a filled vector background and an image.
    patch = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 30, 12), False)
    patch.clear_with(230)
    page.insert_image(fitz.Rect(73, 54, 156, 86), pixmap=patch)
    page.insert_text((42, 78), "\n".join(LEFT), fontsize=14, lineheight=1.9)
    page.insert_text((350, 78), "\n".join(RIGHT), fontsize=14, lineheight=1.9)
    page.insert_text((42, 300), "FOOTER MUST STAY OUTSIDE THE SELECTION", fontsize=12)
    chinese = doc.new_page(width=650, height=390)
    chinese.draw_rect(fitz.Rect(30, 40, 550, 120), fill=(.9, .96, .85), color=None)
    chinese.insert_text((42, 78), "左侧保留 真实擦除 右侧保留", fontname="china-s", fontsize=18)
    chinese.insert_text((42, 150), "第二行保持原样", fontname="china-s", fontsize=18)
    doc.save(target, deflate=True, garbage=4)
    doc.close()
    return target


class NativeSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pdf_engine import PdfDocument
        from text_selection import TextSelector
        cls.PdfDocument = staticmethod(PdfDocument)
        cls.TextSelector = staticmethod(TextSelector)
        cls.path = create_selection_fixture()

    def engine(self, path=None):
        pdf = self.PdfDocument(path or self.path)
        self.addCleanup(pdf.close)
        return pdf

    def assert_selection(self, value, expected):
        self.assertIsNotNone(value)
        self.assertEqual(norm(value["text"]), norm(expected))
        self.assertTrue(value["quads"])
        self.assertTrue(value["rects"])
        self.assertEqual(len(value["bounds"]), 4)
        self.assertTrue(all(len(q) == 8 for q in value["quads"]))
        self.assertTrue(all(len(r) == 4 and r[2] > r[0] and r[3] > r[1] for r in value["rects"]))

    def test_01_partial_line_and_reverse_select_same_characters(self):
        pdf = self.engine()
        selector = self.TextSelector(pdf, 0)
        with fitz.open(self.path) as doc:
            start, end = endpoints(doc[0], "bravo")
        forward = selector.select(start, end)
        backward = selector.select(end, start)
        self.assert_selection(forward, "bravo")
        self.assert_selection(backward, "bravo")
        self.assertEqual(forward["quads"], backward["quads"])
        self.assertEqual(forward["rects"], backward["rects"])
        self.assertLess(fitx_width(forward["bounds"]), 50, "Selected only the word, not the whole line")

    def test_02_multiline_native_range_and_column_isolation(self):
        pdf = self.engine()
        selector = self.TextSelector(pdf, 0)
        with fitz.open(self.path) as doc:
            page = doc[0]
            start, _ = endpoints(page, "bravo")
            _, end = endpoints(page, "ending")
        chosen = selector.select(start, end)
        self.assert_selection(chosen,
                              "bravo charlie DELTA " + LEFT[1] + " " + LEFT[2] + " Fourth ending")
        self.assertGreaterEqual(len(chosen["quads"]), 4)
        self.assertLess(chosen["bounds"][2], 340, "Right column must not enter a left-column range")
        self.assertNotIn("RIGHT", chosen["text"])
        self.assertNotIn("FOOTER", chosen["text"])
        reverse = selector.select(end, start)
        self.assertEqual(norm(reverse["text"]), norm(chosen["text"]))
        self.assertEqual(reverse["quads"], chosen["quads"])

    def test_03_right_column_range_does_not_include_left(self):
        pdf = self.engine()
        selector = self.TextSelector(pdf, 0)
        with fitz.open(self.path) as doc:
            page = doc[0]
            start, _ = endpoints(page, "COLUMN")
            _, end = endpoints(page, "this side.")
        chosen = selector.select(start, end)
        self.assert_selection(chosen, "COLUMN MUST STAY Nothing from this side.")
        self.assertGreater(chosen["bounds"][0], 340)
        self.assertNotIn("ALPHA", chosen["text"])

    def test_04_rotations_and_nonzero_cropbox(self):
        source = QA / "crop-source.pdf"
        self.assertTrue(source.is_file(), "Run test_editor.py once to create the shared crop fixture")
        pdf = self.engine(source)
        with fitz.open(source) as doc:
            for index, page in enumerate(doc):
                with self.subTest(rotation=page.rotation):
                    selector = self.TextSelector(pdf, index)
                    start, end = endpoints(page, "CRET")
                    value = selector.select(start, end)
                    self.assert_selection(value, "CRET")
                    expected = page.search_for("CRET")[0] * page.rotation_matrix
                    actual = fitz.Rect(value["bounds"])
                    for got, want in zip(actual, expected):
                        self.assertAlmostEqual(got, want, delta=2)
                    reverse = selector.select(end, start)
                    self.assertEqual(value["quads"], reverse["quads"])

    def test_05_selected_real_erasure_preserves_neighbors_and_source(self):
        source_hash = sha(self.path)
        pdf = self.engine()
        unchanged_png, _, _ = pdf.render_page(0, 1)
        selector = self.TextSelector(pdf, 0)
        with fitz.open(self.path) as doc:
            start, end = endpoints(doc[0], "bravo")
        chosen = selector.select(start, end)
        self.assert_selection(chosen, "bravo")
        actions = [{"kind": "erase", "page": 0, "rect": r, "text_only": True}
                   for r in chosen["rects"]]
        edited_selector = self.TextSelector(pdf, 0, actions=actions)
        repeat = edited_selector.select(start, end)
        self.assertTrue(repeat is None or "bravo" not in repeat["text"], "Removed characters cannot be selected again")
        # Neighbor words remain usable for later selections in the edited view.
        with fitz.open(self.path) as doc:
            neighbor_start, neighbor_end = endpoints(doc[0], "charlie")
            neighbor_bounds = doc[0].search_for("charlie")[0]
        self.assert_selection(edited_selector.select(neighbor_start, neighbor_end), "charlie")
        out = QA / "selection-erased.pdf"
        pdf.export(out, actions)
        with fitz.open(out) as doc, fitz.open(self.path) as original:
            page = doc[0]
            source_page = original[0]
            output_text = page.get_text()
            self.assertNotIn("bravo", output_text)
            for retained in ("ALPHA", "charlie DELTA", LEFT[1], LEFT[2], LEFT[3], *RIGHT):
                self.assertIn(retained, output_text)
            data = b"".join(doc.xref_stream(x) for x in range(1, doc.xref_length()) if doc.xref_is_stream(x))
            self.assertNotIn(b"bravo", data)
            self.assertNotIn(b"627261766f", data.lower())
            actual_neighbor = page.search_for("charlie")[0]
            for got, want in zip(actual_neighbor, neighbor_bounds):
                self.assertAlmostEqual(got, want, delta=.01, msg="Erasure cannot move the neighboring word")
            self.assertEqual(page.get_drawings(), source_page.get_drawings(), "Text erasure preserves vector backgrounds")
            images_before = [original.extract_image(x[0])["image"] for x in source_page.get_images()]
            images_after = [doc.extract_image(x[0])["image"] for x in page.get_images()]
            self.assertEqual(images_after, images_before, "Text erasure preserves the overlapping image")
        after_png, _, _ = pdf.render_page(0, 1)
        self.assertEqual(after_png, unchanged_png, "Selection helpers cannot mutate the original open PDF")
        self.assertEqual(sha(self.path), source_hash)

    def test_06_chinese_partial_selection_and_real_erasure(self):
        pdf = self.engine()
        selector = self.TextSelector(pdf, 1)
        with fitz.open(self.path) as doc:
            page = doc[1]
            start, end = endpoints(page, "真实擦除")
            preserved = page.search_for("右侧保留")[0]
        value = selector.select(start, end)
        self.assert_selection(value, "真实擦除")
        self.assertEqual(selector.select(end, start)["quads"], value["quads"])
        actions = [{"kind": "erase", "page": 1, "rect": r, "text_only": True} for r in value["rects"]]
        out = QA / "selection-chinese-erased.pdf"
        pdf.export(out, actions)
        with fitz.open(out) as doc:
            page = doc[1]
            self.assertNotIn("真实擦除", page.get_text())
            for retained in ("左侧保留", "右侧保留", "第二行保持原样"):
                self.assertIn(retained, page.get_text())
            for got, want in zip(page.search_for("右侧保留")[0], preserved):
                self.assertAlmostEqual(got, want, delta=.01)
            page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False).save(QA / "selection-chinese-after.png")


def fitx_width(rect):
    return rect[2] - rect[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)
