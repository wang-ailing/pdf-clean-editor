"""Integration tests for real PDF removal, highlights, and rotated display coordinates.

Run with the bundled Python: test_editor.py [--fixtures-only].
All generated material stays in qa/ except the GUI demonstration document.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parent
if (ROOT / "vendor126").is_dir():
    sys.path.insert(0, str(ROOT / "vendor126"))
import pymupdf as fitz
from PIL import Image, ImageChops, ImageDraw, ImageFont

QA = ROOT / "qa"
SECRET = "SECRET-123"
KEEP = "KEEP THIS SENTENCE AND VECTOR ART."


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render(path, page, name, scale=1.35):
    with fitz.open(path) as doc:
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        pix.save(QA / name)


def quad_rect(quads):
    xs = [q[i] for q in quads for i in (0, 2, 4, 6)]
    ys = [q[i] for q in quads for i in (1, 3, 5, 7)]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def displayed_search(page, needle):
    r = page.search_for(needle)[0]
    return r * page.rotation_matrix


def streams(doc):
    result = []
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_is_stream(x):
                result.append(doc.xref_stream(x))
        except (RuntimeError, ValueError):
            pass
    return b"\n".join(result)


def create_fixtures():
    QA.mkdir(exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=600, height=480)
    page.insert_text((48, 52), "Real erasure and yellow highlight", fontsize=22)
    page.insert_text((48, 115), SECRET, fontsize=19, color=(0.75, 0.10, 0.12))
    page.insert_text((48, 170), "Highlight precisely this sentence.", fontsize=15)
    page.insert_text((48, 210), KEEP, fontsize=14)
    page.insert_text((48, 400), "UNAPPLIED REDACTION MUST REMAIN", fontsize=13)
    page.add_redact_annot(page.search_for("UNAPPLIED REDACTION")[0], fill=(1, 1, 1))
    page.draw_rect(fitz.Rect(430, 250, 540, 340), color=(0.1, 0.5, 0.8), width=3)
    page.draw_line((430, 250), (540, 340), color=(0.2, 0.6, 0.3), width=2)
    # Save an unreferenced, incompressible image as a deliberate garbage resource.
    junk = doc.new_page(width=600, height=600)
    rng = random.Random(123)
    raw = rng.randbytes(600 * 600 * 3)
    img = Image.frombytes("RGB", (600, 600), raw)
    image_bytes = io.BytesIO()
    img.save(image_bytes, format="PNG")
    junk.insert_image(junk.rect, stream=image_bytes.getvalue())
    doc.delete_page(1)
    text_path = QA / "text-source.pdf"
    doc.save(text_path, garbage=0, deflate=False)
    doc.close()

    scan = Image.new("RGB", (1000, 700), "white")
    dr = ImageDraw.Draw(scan)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 42)
    small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 30)
    dr.text((80, 80), SECRET, fill=(190, 20, 25), font=font)
    dr.text((80, 230), "Scanned page - real image pixel removal", fill=(15, 20, 25), font=small)
    dr.text((80, 300), "Yellow rectangle highlight works on scans.", fill=(15, 20, 25), font=small)
    dr.rectangle((600, 430, 860, 590), outline=(22, 100, 190), width=7)
    scan_bytes = io.BytesIO()
    scan.save(scan_bytes, "PNG")
    scan_doc = fitz.open()
    scan_page = scan_doc.new_page(width=600, height=420)
    scan_page.insert_image(scan_page.rect, stream=scan_bytes.getvalue())
    scan_path = QA / "scan-source.pdf"
    scan_doc.save(scan_path, deflate=False)
    scan_doc.close()

    rotation = fitz.open()
    for angle in (0, 90, 180, 270):
        pg = rotation.new_page(width=500, height=360)
        pg.insert_text((50, 80), SECRET, fontsize=20)
        pg.insert_text((50, 155), "ROTATED HIGHLIGHT", fontsize=17)
        pg.insert_text((50, 220), KEEP, fontsize=14)
        pg.draw_rect(fitz.Rect(360, 240, 450, 310), color=(0.15, 0.5, 0.75), width=2)
        pg.set_rotation(angle)
    rotation_path = QA / "rotation-source.pdf"
    rotation.save(rotation_path, deflate=True)
    rotation.close()

    cropped = fitz.open()
    for angle in (0, 90, 180, 270):
        pg = cropped.new_page(width=620, height=480)
        pg.insert_text((105, 120), SECRET, fontsize=20)
        pg.insert_text((105, 190), "CROPPED HIGHLIGHT", fontsize=17)
        pg.insert_text((105, 280), KEEP, fontsize=13)
        pg.set_cropbox(fitz.Rect(65, 45, 590, 425))
        pg.set_rotation(angle)
    crop_path = QA / "crop-source.pdf"
    cropped.save(crop_path, deflate=True)
    cropped.close()

    # A compact, pleasant GUI demonstration, distinct from garbage-heavy QA inputs.
    demo = fitz.open()
    p = demo.new_page(width=600, height=760)
    p.draw_rect(fitz.Rect(0, 0, 600, 10), fill=(0.16, 0.40, 0.70), color=None)
    p.insert_text((42, 65), "PDF 真实擦除与黄色高亮", fontname="china-s", fontsize=24,
                  color=(0.12, 0.21, 0.35))
    p.insert_text((42, 103), "拖框擦除敏感内容，或拖框为正文添加黄色高亮。", fontname="china-s", fontsize=13)
    p.draw_rect(fitz.Rect(42, 139, 558, 218), fill=(0.98, 0.94, 0.94), color=None)
    p.insert_text((58, 165), "待擦除的敏感信息", fontname="china-s", fontsize=12, color=(0.5, 0.15, 0.15))
    p.insert_text((58, 198), SECRET, fontsize=22, color=(0.75, 0.1, 0.12))
    p.insert_text((42, 266), "选中这一段：真正擦除会移除底层文字，不只是覆盖白色。", fontname="china-s", fontsize=14)
    p.insert_text((42, 302), "选中这一段：黄色高亮保留文字，便于阅读与复习。", fontname="china-s", fontsize=14)
    p.insert_text((42, 358), "矢量图形与未选中内容应保留原样", fontname="china-s", fontsize=13)
    for i, h in enumerate((70, 105, 85, 135)):
        r = fitz.Rect(65 + i * 110, 550 - h, 130 + i * 110, 550)
        p.draw_rect(r, fill=(0.18 + i * 0.03, 0.42 + i * 0.06, 0.70), color=None)
    p.draw_line((48, 550), (520, 550), color=(0.4, 0.47, 0.55), width=1)
    p.insert_text((42, 650), "第二页为扫描图片，可验证图片中的真实擦除与矩形高亮。", fontname="china-s", fontsize=12)
    p.insert_text((42, 720), "LOCAL PDF EDITOR / DEMO", fontsize=10, color=(0.45, 0.50, 0.58))
    p2 = demo.new_page(width=600, height=420)
    p2.insert_image(p2.rect, stream=scan_bytes.getvalue())
    demo_path = ROOT / "示例文档.pdf"
    demo.save(demo_path, garbage=4, deflate=True)
    demo.close()
    for source, index, name in ((text_path, 0, "text-before.png"),
                                (scan_path, 0, "scan-before.png"),
                                (demo_path, 0, "demo-before.png")):
        render(source, index, name)
    return {"text": text_path, "scan": scan_path, "rotation": rotation_path,
            "crop": crop_path, "demo": demo_path}


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pdf_engine import PdfDocument
        cls.PdfDocument = staticmethod(PdfDocument)
        cls.paths = create_fixtures()

    def open_engine(self, name):
        engine = self.PdfDocument(self.paths[name])
        self.addCleanup(engine.close)
        return engine

    def assert_no_secret(self, doc):
        self.assertNotIn(SECRET, "".join(pg.get_text() for pg in doc))
        data = streams(doc)
        self.assertNotIn(SECRET.encode(), data)
        self.assertNotIn(SECRET.encode().hex().encode().lower(), data.lower())

    def assert_yellow(self, annot):
        colors = annot.colors
        candidates = [colors.get("stroke"), colors.get("fill")]
        self.assertTrue(any(c and len(c) == 3 and c[0] > .9 and c[1] > .8 and c[2] < .2
                            for c in candidates), colors)

    def test_01_real_text_redaction_garbage_and_safe_copy(self):
        source = self.paths["text"]
        source_hash = digest(source)
        engine = self.open_engine("text")
        self.assertEqual(engine.page_count, 1)
        self.assertEqual(engine.source_size, source.stat().st_size)
        self.assertEqual(tuple(engine.page_size(0)), (600, 480))
        png, w, h = engine.render_page(0, 1)
        self.assertEqual((w, h), (600, 480))
        self.assertEqual(Image.open(io.BytesIO(png)).size, (600, 480))
        with fitz.open(source) as inp:
            rect = displayed_search(inp[0], SECRET)
            baseline = inp[0].get_pixmap(alpha=False).pil_image()
            initial_streams = streams(inp)
            self.assertTrue(SECRET.encode() in initial_streams or
                            SECRET.encode().hex().encode() in initial_streams.lower())
        out = QA / "text-erased.pdf"
        result = engine.export(out, [{"kind": "erase", "page": 0, "rect": list(rect)}])
        self.assertEqual(digest(source), source_hash, "The original must remain untouched")
        self.assertEqual(result["output_bytes"], out.stat().st_size)
        self.assertGreater(result["percent_saved"], 80)
        self.assertLess(out.stat().st_size, source.stat().st_size * .2)
        self.assertEqual(out.read_bytes().count(b"%%EOF"), 1, "No previous revision payload")
        with fitz.open(out) as doc:
            self.assert_no_secret(doc)
            self.assertIn(KEEP, doc[0].get_text())
            self.assertIn("UNAPPLIED REDACTION MUST REMAIN", doc[0].get_text())
            self.assertEqual(len(list(doc[0].annots())), 1)
            after = doc[0].get_pixmap(alpha=False).pil_image()
            diff = ImageChops.difference(baseline, after)
            ImageDraw.Draw(diff).rectangle(tuple(rect + (-3, -3, 3, 3)), fill=(0, 0, 0))
            self.assertIsNone(diff.getbbox(), "Outside pixels/layout must remain identical")
        render(out, 0, "text-after.png")
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            engine.export(source, [])
        self.assertEqual(digest(source), source_hash)

    def test_02_text_highlight_quads_persistence_and_no_text_loss(self):
        engine = self.open_engine("text")
        with fitz.open(self.paths["text"]) as doc:
            selected = displayed_search(doc[0], "precisely this sentence")
        quads = engine.text_quads(0, list(selected))
        self.assertGreater(len(quads), 0)
        self.assertTrue(all(len(q) == 8 for q in quads))
        self.assertLess(abs(quad_rect(quads).y0 - selected.y0), 2)
        out = QA / "text-highlight.pdf"
        engine.export(out, [{"kind": "highlight", "page": 0, "rect": list(selected), "quads": quads}])
        with fitz.open(out) as doc:
            page = doc[0]
            self.assertIn(SECRET, page.get_text())
            yellow = [a for a in page.annots() if a.type[1] == "Highlight"]
            self.assertEqual(len(yellow), 1)
            self.assert_yellow(yellow[0])
            self.assertLess(abs(quad_rect([yellow[0].vertices[0] + yellow[0].vertices[1] +
                                          yellow[0].vertices[2] + yellow[0].vertices[3]]).x0 - selected.x0), 5)
        second = self.PdfDocument(out)
        self.addCleanup(second.close)
        out2 = QA / "text-highlight-reopened.pdf"
        with fitz.open(out) as doc:
            secret_rect = displayed_search(doc[0], SECRET)
        second.export(out2, [{"kind": "erase", "page": 0, "rect": list(secret_rect)}])
        with fitz.open(out2) as doc:
            self.assert_no_secret(doc)
            self.assertEqual(sum(a.type[1] == "Highlight" for a in doc[0].annots()), 1)
        render(out, 0, "highlight-after.png")

    def test_03_scan_pixel_removal_and_scan_highlight(self):
        engine = self.open_engine("scan")
        erased = fitz.Rect(42, 42, 240, 93)
        highlighted = fitz.Rect(44, 173, 505, 213)
        self.assertEqual(engine.text_quads(0, list(highlighted)), [])
        out = QA / "scan-erased.pdf"
        with fitz.open(self.paths["scan"]) as src:
            original_image = src.extract_image(src[0].get_images()[0][0])["image"]
            baseline = src[0].get_pixmap(alpha=False).pil_image()
        engine.export(out, [{"kind": "erase", "page": 0, "rect": list(erased)},
                            {"kind": "highlight", "page": 0, "rect": list(highlighted), "quads": []}])
        with fitz.open(out) as doc:
            self.assertEqual(doc[0].get_text().strip(), "")
            after = doc[0].get_pixmap(alpha=False).pil_image()
            region = after.crop(tuple(erased + (2, 2, -2, -2)))
            self.assertEqual(region.getextrema(), ((255, 255), (255, 255), (255, 255)))
            diff = ImageChops.difference(baseline, after)
            painter = ImageDraw.Draw(diff)
            painter.rectangle(tuple(erased + (-3, -3, 3, 3)), fill=(0, 0, 0))
            painter.rectangle(tuple(highlighted + (-4, -4, 4, 4)), fill=(0, 0, 0))
            self.assertIsNone(diff.getbbox(), "Scan pixels outside edited areas must be preserved")
            images = [doc.extract_image(x[0])["image"] for x in doc[0].get_images()]
            self.assertNotIn(original_image, images, "Original secret image must not remain recoverable")
            self.assertTrue(images)
            for x in doc[0].get_images():
                image = Image.open(io.BytesIO(doc.extract_image(x[0])["image"])).convert("RGB")
                if image.size == (1000, 700):
                    r = image.crop((75, 75, 390, 145))
                    self.assertEqual(r.getextrema(), ((255, 255), (255, 255), (255, 255)))
            annots = list(doc[0].annots())
            self.assertEqual(len(annots), 1)
            self.assert_yellow(annots[0])
        render(out, 0, "scan-after.png")

    def test_04_rotation_display_coordinates(self):
        engine = self.open_engine("rotation")
        actions = []
        with fitz.open(self.paths["rotation"]) as doc:
            for index, page in enumerate(doc):
                self.assertEqual(tuple(engine.page_size(index)), (page.rect.width, page.rect.height))
                erase = displayed_search(page, SECRET)
                high = displayed_search(page, "ROTATED HIGHLIGHT")
                quads = engine.text_quads(index, list(high))
                self.assertTrue(quads, "Text quads at rotation %s" % page.rotation)
                self.assertLess(abs(quad_rect(quads).x0 - high.x0), 2)
                actions.extend([{"kind": "erase", "page": index, "rect": list(erase)},
                                {"kind": "highlight", "page": index, "rect": list(high), "quads": quads}])
        out = QA / "rotation-erased.pdf"
        engine.export(out, actions)
        with fitz.open(out) as doc:
            self.assert_no_secret(doc)
            for page in doc:
                self.assertIn(KEEP, page.get_text())
                yellow = [a for a in page.annots() if a.type[1] == "Highlight"]
                self.assertEqual(len(yellow), 1)
                self.assert_yellow(yellow[0])
                actual = fitz.Quad(yellow[0].vertices).rect * page.rotation_matrix
                expected = displayed_search(page, "ROTATED HIGHLIGHT")
                for got, want in zip(actual, expected):
                    self.assertAlmostEqual(got, want, delta=2)
        render(out, 1, "rotation-90-after.png")

    def test_05_nonzero_cropbox_with_all_rotations(self):
        engine = self.open_engine("crop")
        actions = []
        with fitz.open(self.paths["crop"]) as doc:
            for index, page in enumerate(doc):
                self.assertNotEqual(page.cropbox_position, fitz.Point(0, 0))
                self.assertEqual(tuple(engine.page_size(index)), (page.rect.width, page.rect.height))
                erase = displayed_search(page, SECRET)
                high = displayed_search(page, "CROPPED HIGHLIGHT")
                quads = engine.text_quads(index, list(high))
                self.assertTrue(quads)
                for got, want in zip(quad_rect(quads), high):
                    self.assertAlmostEqual(got, want, delta=2)
                actions.extend([{"kind": "erase", "page": index, "rect": list(erase)},
                                {"kind": "highlight", "page": index, "rect": list(high), "quads": quads}])
        out = QA / "crop-erased.pdf"
        engine.export(out, actions)
        with fitz.open(out) as doc:
            self.assert_no_secret(doc)
            for page in doc:
                self.assertIn(KEEP, page.get_text())
                yellow = [a for a in page.annots() if a.type[1] == "Highlight"]
                self.assertEqual(len(yellow), 1)
                actual = fitz.Quad(yellow[0].vertices).rect * page.rotation_matrix
                expected = displayed_search(page, "CROPPED HIGHLIGHT")
                for got, want in zip(actual, expected):
                    self.assertAlmostEqual(got, want, delta=2)
        render(out, 3, "crop-270-after.png")

    def test_06_true_preview_matches_export_for_annotation_and_vector_removal(self):
        engine = self.open_engine("text")
        source_hash = digest(self.paths["text"])
        baseline_png, _, _ = engine.render_page(0, 1.35)
        with fitz.open(self.paths["text"]) as doc:
            high = displayed_search(doc[0], "precisely this sentence")
        quads = engine.text_quads(0, list(high))
        # The erasure intersects a small part of the prior highlight. The real
        # exporter drops that whole annotation, so a white overlay is inaccurate.
        small_erase = fitz.Rect(high.x0 + 10, high.y0 + 3, high.x0 + 28, high.y1 - 3)
        cases = {
            "annotation": [
                {"kind": "highlight", "page": 0, "rect": list(high), "quads": quads},
                {"kind": "erase", "page": 0, "rect": list(small_erase)},
            ],
            # The rectangle is one PDF path; touching its top-left edge removes
            # the whole path, including the portion outside the small eraser.
            "vector": [{"kind": "erase", "page": 0, "rect": [425, 247, 445, 275]}],
        }
        for name, actions in cases.items():
            with self.subTest(case=name):
                png, width, height = engine.render_preview(0, 1.35, actions)
                preview = Image.open(io.BytesIO(png)).convert("RGB")
                self.assertEqual(preview.size, (width, height))
                out = QA / ("preview-" + name + "-export.pdf")
                engine.export(out, actions)
                with fitz.open(out) as doc:
                    page = doc[0]
                    exported = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False).pil_image()
                    if name == "annotation":
                        self.assertEqual(sum(a.type[1] == "Highlight" for a in page.annots()), 0)
                    else:
                        original_art_bounds = fitz.Rect(430, 250, 540, 340)
                        self.assertFalse(any(d["rect"] == original_art_bounds for d in page.get_drawings()),
                                         "The intersected complete original vector paths are removed")
                diff = ImageChops.difference(preview, exported)
                self.assertIsNone(diff.getbbox(), "Actual preview and exported rendering must match pixel for pixel")
                preview.save(QA / ("preview-" + name + ".png"))
                exported.save(QA / ("preview-" + name + "-export.png"))
        unchanged_png, _, _ = engine.render_page(0, 1.35)
        self.assertEqual(unchanged_png, baseline_png, "Preview must not mutate the open source")
        self.assertEqual(digest(self.paths["text"]), source_hash)


if __name__ == "__main__":
    if "--fixtures-only" in sys.argv:
        paths = create_fixtures()
        print(json.dumps({k: str(v) for k, v in paths.items()}, ensure_ascii=False, indent=2))
    else:
        unittest.main(verbosity=2)
