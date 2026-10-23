"""Rendered regressions for flat-ended native PDF highlights.

All PDFs are generated locally under qa/. The tests use PDF renderers directly;
they never open an application or use the mouse/keyboard on the user's desktop.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
if (ROOT / "vendor126").is_dir():
    sys.path.insert(0, str(ROOT / "vendor126"))
import pymupdf as fitz
from PIL import Image, ImageChops

from pdf_engine import HIGHLIGHT_STYLES, PdfDocument

QA = ROOT / "qa"
SCALE = 4


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def flatten(quad):
    return [value for point in quad for value in point]


def point_at(quad, across, down):
    """Interpolate in the quadrilateral, including beyond the left/right ends."""
    top = quad.ul + (quad.ur - quad.ul) * across
    bottom = quad.ll + (quad.lr - quad.ll) * across
    return top + (bottom - top) * down


def make_blank(path, rotation=0):
    with fitz.open() as doc:
        page = doc.new_page(width=460, height=380)
        page.set_rotation(rotation)
        doc.save(path)


def highlight_action(quad, style="highlight", page=0, *, explicit_quads=True):
    result = {"kind": "highlight", "page": page, "rect": list(quad.rect), "style": style}
    if explicit_quads:
        result["quads"] = [flatten(quad)]
    return result


def image_from_render(result):
    data, width, height = result
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert image.size == (width, height)
    return image


def render_file(path, page=0):
    with fitz.open(path) as doc:
        pixmap = doc[page].get_pixmap(matrix=fitz.Matrix(SCALE, SCALE), alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


class FlatHighlightTests(unittest.TestCase):
    def setUp(self):
        QA.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="flat-highlights-", dir=QA)
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.source = self.directory / "原文件.pdf"

    def assert_pixels_flat(self, image, quad, style):
        def pixel(across, down):
            position = point_at(quad, across, down)
            return image.getpixel((int(position.x * SCALE), int(position.y * SCALE)))

        # Every end remains filled near both corners. Native MuPDF's curved
        # caps fail these checks despite coloring the middle of the text run.
        color = HIGHLIGHT_STYLES[style]["color"]
        opacity = HIGHLIGHT_STYLES[style]["opacity"]
        expected = tuple(round(255 * (1 - opacity + component * opacity)) for component in color)
        for across in (.01, .05, .5, .95, .99):
            for down in (.07, .25, .5, .75, .93):
                with self.subTest(location="inside", across=across, down=down):
                    for actual, wanted in zip(pixel(across, down), expected):
                        self.assertAlmostEqual(actual, wanted, delta=3)
        # A native round cap protrudes well beyond these selection boundaries.
        for across in (-.01, 1.01):
            for down in (.15, .5, .85):
                with self.subTest(location="outside-end", across=across, down=down):
                    self.assertEqual(pixel(across, down), (255, 255, 255))
        for across in (.1, .5, .9):
            for down in (-.08, 1.08):
                with self.subTest(location="outside-side", across=across, down=down):
                    self.assertEqual(pixel(across, down), (255, 255, 255))

    def assert_annotation(self, annotation, style, expected_quads):
        self.assertEqual(annotation.type, (fitz.PDF_ANNOT_HIGHLIGHT, "Highlight"))
        self.assertEqual(annotation.info["subject"], HIGHLIGHT_STYLES[style]["label"])
        self.assertEqual(annotation.info["content"], HIGHLIGHT_STYLES[style]["label"])
        self.assertAlmostEqual(annotation.opacity, HIGHLIGHT_STYLES[style]["opacity"], places=5)
        for actual, expected in zip(annotation.colors["stroke"], HIGHLIGHT_STYLES[style]["color"]):
            self.assertAlmostEqual(actual, expected, places=5)
        expected_points = [point for quad in expected_quads for point in quad]
        self.assertEqual(len(annotation.vertices), len(expected_points))
        for actual, expected in zip(annotation.vertices, expected_points):
            self.assertAlmostEqual(actual[0], expected.x, delta=.002)
            self.assertAlmostEqual(actual[1], expected.y, delta=.002)

    def assert_preview_matches(self, preview, output, page=0):
        difference = ImageChops.difference(preview, render_file(output, page))
        self.assertLessEqual(max(channel[1] for channel in difference.getextrema()), 2)
        average_error = sum(sum(value * count for value, count in enumerate(histogram))
                            for histogram in (difference.getchannel(c).histogram() for c in range(3)))
        self.assertLessEqual(average_error / (preview.width * preview.height * 3), .01)

    def test_01_three_styles_are_flat_and_native_after_save_reopen(self):
        make_blank(self.source)
        with PdfDocument(self.source) as pdf:
            actions = []
            quads = []
            for index, style in enumerate(HIGHLIGHT_STYLES):
                quad = fitz.Rect(85, 50 + index * 85, 290, 78 + index * 85).quad
                quads.append(quad)
                actions.append(highlight_action(quad, style))
            preview = image_from_render(pdf.render_preview(0, SCALE, actions))
            for quad, style in zip(quads, HIGHLIGHT_STYLES):
                self.assert_pixels_flat(preview, quad, style)
            pdf.save_in_place(actions)
        with fitz.open(self.source) as doc:
            page = doc[0]
            annotations = list(page.annots())
            self.assertEqual(len(annotations), 3)
            for annotation, quad, style in zip(annotations, quads, HIGHLIGHT_STYLES):
                self.assert_annotation(annotation, style, [quad])
        self.assert_preview_matches(preview, self.source)
        with PdfDocument(self.source) as reopened:
            self.assertFalse(reopened.needs_highlight_upgrade)
            self.assert_preview_matches(image_from_render(reopened.render_page(0, SCALE)), self.source)

    def test_02_tilted_edges_follow_quad_on_all_page_rotations(self):
        for rotation in (0, 90, 180, 270):
            make_blank(self.source, rotation)
            raw = fitz.Quad(((90, 135), (290, 85), (97, 163), (297, 113)))
            with PdfDocument(self.source) as pdf:
                visible = raw * pdf.doc[0].rotation_matrix
                for style in HIGHLIGHT_STYLES:
                    with self.subTest(rotation=rotation, style=style):
                        action = highlight_action(visible, style)
                        preview = image_from_render(pdf.render_preview(0, SCALE, [action]))
                        self.assert_pixels_flat(preview, visible, style)
                        output = self.directory / f"tilt-{rotation}-{style}.pdf"
                        pdf.export(output, [action])
                        self.assert_preview_matches(preview, output)
                        with fitz.open(output) as doc:
                            page = doc[0]
                            annotation = page.first_annot
                            self.assert_annotation(annotation, style, [raw])

    def test_03_multiline_quad_gaps_are_not_filled(self):
        make_blank(self.source)
        quads = [fitz.Rect(90, 65, 290, 91).quad, fitz.Rect(90, 125, 240, 151).quad]
        action = highlight_action(quads[0], "confused")
        action["rect"] = list(quads[0].rect | quads[1].rect)
        action["quads"] = [flatten(quad) for quad in quads]
        with PdfDocument(self.source) as pdf:
            pdf.save_in_place([action])
        image = render_file(self.source)
        for quad in quads:
            self.assert_pixels_flat(image, quad, "confused")
        self.assertEqual(image.getpixel((150 * SCALE, 108 * SCALE)), (255, 255, 255))
        with fitz.open(self.source) as doc:
            page = doc[0]
            self.assertEqual(len(list(page.annots())), 1)
            self.assert_annotation(page.first_annot, "confused", quads)

    def test_04_image_selection_fallback_stays_a_flat_square(self):
        make_blank(self.source)
        quad = fitz.Rect(95, 110, 290, 142).quad
        with PdfDocument(self.source) as pdf:
            action = highlight_action(quad, "useful", explicit_quads=False)
            pdf.save_in_place([action])
        self.assert_pixels_flat(render_file(self.source), quad, "useful")
        with fitz.open(self.source) as doc:
            page = doc[0]
            annotation = page.first_annot
            self.assertEqual(annotation.type, (fitz.PDF_ANNOT_SQUARE, "Square"))
            self.assertEqual(annotation.info["subject"], HIGHLIGHT_STYLES["useful"]["label"])
            self.assertEqual(annotation.info["content"], HIGHLIGHT_STYLES["useful"]["label"])
            for actual, expected in zip(annotation.colors["fill"], HIGHLIGHT_STYLES["useful"]["color"]):
                self.assertAlmostEqual(actual, expected, places=5)

    def test_05_erasure_removes_text_and_touched_flat_annotation_only(self):
        with fitz.open() as doc:
            page = doc.new_page(width=460, height=380)
            page.insert_text((70, 100), "SECRET-123 KEEP NEIGHBOR", fontsize=18)
            secret = page.search_for("SECRET-123")[0]
            keep = page.search_for("KEEP NEIGHBOR")[0]
            doc.save(self.source)
        untouched = fitz.Rect(80, 170, 290, 198).quad
        actions = [highlight_action(secret.quad, "confused"),
                   highlight_action(untouched, "useful"),
                   {"kind": "erase", "page": 0, "rect": list(secret), "text_only": True}]
        with PdfDocument(self.source) as pdf:
            preview = image_from_render(pdf.render_preview(0, SCALE, actions))
            pdf.save_in_place(actions)
        self.assert_preview_matches(preview, self.source)
        with fitz.open(self.source) as doc:
            page = doc[0]
            self.assertNotIn("SECRET-123", page.get_text())
            self.assertIn("KEEP NEIGHBOR", page.get_text())
            for actual, expected in zip(page.search_for("KEEP NEIGHBOR")[0], keep):
                self.assertAlmostEqual(actual, expected, delta=.02)
            self.assertEqual(len(list(page.annots())), 1)
            self.assert_annotation(page.first_annot, "useful", [untouched])
        self.assert_pixels_flat(render_file(self.source), untouched, "useful")

    def test_06_old_owned_marks_upgrade_without_touching_other_authors(self):
        owned = []
        other_quad = fitz.Rect(85, 300, 290, 328).quad
        with fitz.open() as doc:
            page = doc.new_page(width=460, height=380)
            for index, style in enumerate(HIGHLIGHT_STYLES):
                quad = fitz.Rect(85, 35 + index * 80, 290, 63 + index * 80).quad
                owned.append(quad)
                annotation = page.add_highlight_annot(quad)
                annotation.set_colors(stroke=HIGHLIGHT_STYLES[style]["color"])
                annotation.set_opacity(HIGHLIGHT_STYLES[style]["opacity"])
                annotation.set_info(title="PDF 净擦", subject=HIGHLIGHT_STYLES[style]["label"],
                                    content=HIGHLIGHT_STYLES[style]["label"])
                annotation.update()
            other = page.add_highlight_annot(other_quad)
            other.set_info(title="Another reader", subject="困惑", content="Keep my annotation")
            other.update()
            third_party_appearance = other._getAP()
            third_party_info = other.info
            doc.save(self.source)
        original_hash = digest(self.source)
        old_image = render_file(self.source)
        # Verify this fixture really reproduces round-cap protrusion.
        outside = point_at(owned[0], -.01, .5)
        self.assertNotEqual(old_image.getpixel((int(outside.x * SCALE), int(outside.y * SCALE))),
                            (255, 255, 255))
        with PdfDocument(self.source) as pdf:
            self.assertTrue(pdf.needs_highlight_upgrade)
            self.assertEqual(digest(self.source), original_hash, "Opening must not modify the original")
            upgraded_image = image_from_render(pdf.render_page(0, SCALE))
            for quad, style in zip(owned, HIGHLIGHT_STYLES):
                self.assert_pixels_flat(upgraded_image, quad, style)
            output = self.directory / "upgraded-copy.pdf"
            pdf.export(output, [])
            self.assertTrue(pdf.needs_highlight_upgrade, "Saving a copy must not mark the original saved")
            self.assertEqual(digest(self.source), original_hash)
            pdf.save_in_place([])
            self.assertFalse(pdf.needs_highlight_upgrade)
            self.assertNotEqual(digest(self.source), original_hash)
            pdf.save_in_place([])
            # Continue editing the same immutable session after its migration
            # was saved. Undo / redo must not restore the baseline's round AP,
            # drop a category, or duplicate old marks when replaying actions.
            extra_quad = fitz.Rect(85, 245, 290, 273).quad
            extra_action = highlight_action(extra_quad, "useful")
            for queue in ([extra_action], [], [extra_action], []):
                pdf.save_in_place(queue)
                self.assertFalse(pdf.needs_highlight_upgrade)
                with fitz.open(self.source) as check:
                    page = check[0]
                    annotations = list(page.annots())
                    self.assertEqual(len(annotations), 4 + len(queue))
                    for annotation, quad, style in zip(annotations[:3], owned, HIGHLIGHT_STYLES):
                        self.assert_annotation(annotation, style, [quad])
                    if queue:
                        self.assert_annotation(annotations[-1], "useful", [extra_quad])
                image = render_file(self.source)
                for quad, style in zip(owned, HIGHLIGHT_STYLES):
                    self.assert_pixels_flat(image, quad, style)
                if queue:
                    self.assert_pixels_flat(image, extra_quad, "useful")
        self.assert_preview_matches(upgraded_image, self.source)
        with fitz.open(self.source) as doc:
            page = doc[0]
            annotations = list(page.annots())
            self.assertEqual(len(annotations), 4, "Migration must replace appearances without duplicating marks")
            for annotation, quad, style in zip(annotations[:3], owned, HIGHLIGHT_STYLES):
                self.assert_annotation(annotation, style, [quad])
            other = annotations[3]
            # Full-PDF cleanup canonicalizes stream whitespace; drawing
            # operators, numeric geometry, colors and graphics state survive.
            tokens = rb"/[A-Za-z0-9_]+|[-+]?(?:\d*\.\d+|\d+)|[A-Za-z]+"
            self.assertEqual(re.findall(tokens, other._getAP()), re.findall(tokens, third_party_appearance))
            for key in ("title", "subject", "content"):
                self.assertEqual(other.info[key], third_party_info[key])
        with PdfDocument(self.source) as reopened:
            self.assertFalse(reopened.needs_highlight_upgrade)

    def test_07_poppler_independently_renders_flat_ends(self):
        candidate = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
                     / "native/poppler/Library/bin/pdftoppm.exe")
        binary = shutil.which("pdftoppm") or (str(candidate) if candidate.is_file() else None)
        if not binary:
            self.skipTest("Poppler is unavailable; MuPDF render assertions still cover all geometry")
        make_blank(self.source)
        quads = [fitz.Rect(85, 50 + index * 85, 290, 78 + index * 85).quad
                 for index in range(3)]
        with PdfDocument(self.source) as pdf:
            pdf.save_in_place([highlight_action(quad, style)
                               for quad, style in zip(quads, HIGHLIGHT_STYLES)])
        prefix = self.directory / "poppler"
        subprocess.run([binary, "-f", "1", "-singlefile", "-r", str(72 * SCALE), "-png",
                        str(self.source), str(prefix)], check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with Image.open(prefix.with_suffix(".png")) as source_image:
            image = source_image.convert("RGB")
        for quad, style in zip(quads, HIGHLIGHT_STYLES):
            self.assert_pixels_flat(image, quad, style)


def write_visual_fixture():
    """Create a durable proof image for review without opening a desktop app."""
    QA.mkdir(exist_ok=True)
    source = QA / "flat-highlights-source.pdf"
    output = QA / "flat-highlights.pdf"
    actions = []
    with fitz.open() as doc:
        page = doc.new_page(width=760, height=440)
        page.insert_text((40, 43), "直边高亮：保存后的 PDF", fontname="china-s", fontsize=23)
        page.insert_text((40, 72), "三种类别都保留原生高亮批注；右侧色带用于观察两端直边。",
                         fontname="china-s", fontsize=13)
        for index, style in enumerate(HIGHLIGHT_STYLES):
            y = 137 + index * 85
            page.insert_text((40, y), "采用启发式策略，对模型进行测试。", fontname="china-s", fontsize=17)
            area = page.search_for("启发式策略", quads=True)[-1]
            actions.append(highlight_action(area, style))
            band = fitz.Rect(485, y - 22, 700, y + 6).quad
            actions.append(highlight_action(band, style))
            page.insert_text((408, y), HIGHLIGHT_STYLES[style]["label"], fontname="china-s", fontsize=15)
        page.insert_text((40, 395), "此图由 Poppler 直接渲染，不操作桌面或用户文件。", fontname="china-s", fontsize=13)
        page = doc.new_page(width=760, height=440)
        page.insert_text((40, 43), "倾斜与多行选区的边界", fontname="china-s", fontsize=23)
        tilt = fitz.Quad(((85, 170), (370, 110), (91, 198), (376, 138)))
        actions.append(highlight_action(tilt, "confused", page=1))
        for y in (280, 330):
            page.insert_text((70, y), "有用的结果仍然可以继续高亮和擦除。", fontname="china-s", fontsize=18)
        multi_quads = page.search_for("仍然可以继续高亮", quads=True)
        multi_action = highlight_action(multi_quads[0], "useful", page=1)
        multi_action["quads"] = [flatten(quad) for quad in multi_quads]
        multi_action["rect"] = list(multi_quads[0].rect | multi_quads[1].rect)
        actions.append(multi_action)
        doc.save(source, garbage=4, deflate=True)
    with PdfDocument(source) as pdf:
        pdf.export(output, actions)
    candidate = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
                 / "native/poppler/Library/bin/pdftoppm.exe")
    binary = shutil.which("pdftoppm") or (str(candidate) if candidate.is_file() else None)
    if binary:
        for page_number, name in ((1, "flat-highlights"), (2, "flat-highlights-tilted")):
            subprocess.run([binary, "-f", str(page_number), "-singlefile", "-r", "144", "-png",
                            str(output), str(QA / name)], check=True, capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    else:
        with fitz.open(output) as doc:
            doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(QA / "flat-highlights.png")
    return output


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FlatHighlightTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("Visual QA PDF:", write_visual_fixture())
    raise SystemExit(0 if result.wasSuccessful() else 1)
