"""PDF editing backend: real content removal and yellow annotations.

All public coordinates use the visible, rotated page (the same space as
render_page).  Nothing is written back to the source PDF.  Export always opens
an independent snapshot, applies the queued edits in order, then rewrites the
whole document with unused objects removed rather than incrementally appending.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import pymupdf as fitz


class PdfEngineError(RuntimeError):
    """A user-facing PDF operation error."""


def _rect(value: Iterable[float]) -> fitz.Rect:
    try:
        values = tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise PdfEngineError("选区坐标无效。") from exc
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise PdfEngineError("选区需要 4 个有效坐标。")
    rect = fitz.Rect(values)
    rect.normalize()
    if rect.is_empty or rect.is_infinite:
        raise PdfEngineError("选区不能为空。")
    return rect


def _quad(value: Iterable[float]) -> fitz.Quad:
    try:
        values = tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise PdfEngineError("高亮坐标无效。") from exc
    if len(values) != 8 or not all(math.isfinite(v) for v in values):
        raise PdfEngineError("高亮四边形需要 8 个有效坐标。")
    quad = fitz.Quad([values[i:i + 2] for i in range(0, 8, 2)])
    if not quad.is_convex or quad.is_empty:
        raise PdfEngineError("高亮选区无效。")
    return quad


def _flat_quad(quad: fitz.Quad) -> list[float]:
    return [coordinate for point in quad for coordinate in (point.x, point.y)]


def _overlap(first: fitz.Rect, second: fitz.Rect) -> bool:
    intersection = first & second
    return not intersection.is_empty and intersection.width > 0 and intersection.height > 0


class PdfDocument:
    """A read-only loaded PDF plus export helpers for the editor UI."""

    def __init__(self, path: str | os.PathLike[str], password: str | None = None):
        self.path = Path(path).expanduser().resolve()
        self._password = password
        self._source_data = self.path.read_bytes()
        self.source_size = len(self._source_data)
        self.doc = self._open_snapshot()
        if not self.doc.is_pdf:
            self.doc.close()
            raise PdfEngineError("请选择 PDF 文件。")
        self.page_count = self.doc.page_count
        if not self.page_count:
            self.doc.close()
            raise PdfEngineError("这份 PDF 没有可编辑的页面。")

    def _open_snapshot(self) -> fitz.Document:
        try:
            doc = fitz.open(stream=self._source_data, filetype="pdf")
            if doc.needs_pass and (not self._password or not doc.authenticate(self._password)):
                doc.close()
                raise PdfEngineError("这份 PDF 需要正确的打开密码。")
            return doc
        except PdfEngineError:
            raise
        except Exception as exc:
            raise PdfEngineError(f"无法打开这份 PDF：{exc}") from exc

    def _page(self, index: int) -> fitz.Page:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < self.page_count:
            raise PdfEngineError("页码超出范围。")
        return self.doc.load_page(index)

    def close(self) -> None:
        if not self.doc.is_closed:
            self.doc.close()

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def page_size(self, index: int) -> tuple[float, float]:
        bounds = self._page(index).rect
        return bounds.width, bounds.height

    def render_page(self, index: int, scale: float) -> tuple[bytes, int, int]:
        return self._render(self._page(index), scale)

    @staticmethod
    def _render(page: fitz.Page, scale: float) -> tuple[bytes, int, int]:
        if not math.isfinite(scale) or not 0.05 <= scale <= 8:
            raise PdfEngineError("页面缩放比例无效。")
        if page.rect.width * page.rect.height * scale * scale > 80_000_000:
            raise PdfEngineError("页面过大，请降低缩放比例。")
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB,
                                 alpha=False, annots=True)
        return pixmap.tobytes("png"), pixmap.width, pixmap.height

    def render_preview(self, index: int, scale: float, actions: Iterable[dict[str, Any]],
                       optimize_images: bool = False) -> tuple[bytes, int, int]:
        """Render the actual edited page on a disposable in-memory document.

        This intentionally uses the same operation helper as export. A touched
        vector path can disappear beyond the selection, and an erased existing
        annotation can disappear in its entirety; the preview reflects both.
        Other pages' operations are ignored without mutating the loaded source.
        """
        self._page(index)
        relevant = [action for action in actions if isinstance(action, dict) and action.get("page") == index]
        if not relevant:
            return self.render_page(index, scale)
        grouped = self._normalize_actions(relevant)
        work = self._open_snapshot()
        try:
            page, _ = self._apply_page_actions(work, index, grouped[index], optimize_images, [])
            # Export's clean=True performs the same resource sanitization.
            page.clean_contents(sanitize=True)
            return self._render(page, scale)
        except PdfEngineError:
            raise
        except Exception as exc:
            raise PdfEngineError(f"无法预览编辑结果：{exc}") from exc
        finally:
            work.close()

    def text_quads(self, index: int, rect_tuple: Iterable[float]) -> list[list[float]]:
        """Snap a dragged visible rectangle to characters whose centres it contains.

        Character runs are combined within a span, respecting a rotated text
        baseline; a skim across a neighbouring line cannot highlight that line.
        Image-only pages return an empty list for the UI's rectangular fallback.
        """
        page = self._page(index)
        selected = _rect(rect_tuple) & page.rect
        if selected.is_empty:
            return []
        matrix = page.rotation_matrix
        result: list[list[float]] = []
        # Text only; get_text(rawdict)'s default can include full image binaries.
        flags = getattr(fitz, "TEXTFLAGS_RAWDICT", 199) & ~getattr(fitz, "TEXT_PRESERVE_IMAGES", 4)
        raw = page.get_text("rawdict", flags=flags)
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                direction = line.get("dir", (1.0, 0.0))
                for span in line.get("spans", []):
                    run: list[dict[str, Any]] = []

                    def flush() -> None:
                        if not run:
                            return
                        quad = fitz.recover_span_quad(direction, span, chars=run) * matrix
                        if quad.is_convex and not quad.is_empty:
                            result.append(_flat_quad(quad))
                        run.clear()

                    for character in span.get("chars", []):
                        bounds = fitz.Rect(character["bbox"])
                        center = (bounds.tl + bounds.br) * 0.5 * matrix
                        if selected.contains(center):
                            run.append(character)
                        else:
                            flush()
                    flush()
        return result

    def _normalize_actions(self, actions: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for action in actions:
            if not isinstance(action, dict):
                raise PdfEngineError("编辑操作格式无效。")
            index = action.get("page")
            self._page(index)
            kind = action.get("kind")
            if kind not in ("erase", "highlight"):
                raise PdfEngineError("不支持的编辑操作。")
            clean_action = dict(action)
            clean_action["rect"] = tuple(_rect(action.get("rect", ())))
            if kind == "highlight" and action.get("quads"):
                clean_action["quads"] = [_flat_quad(_quad(q)) for q in action["quads"]]
            grouped[index].append(clean_action)
        return grouped

    def _apply_page_actions(self, work: fitz.Document, index: int, page_actions: list[dict[str, Any]],
                            optimize_images: bool, warnings: list[str]) -> tuple[fitz.Page, dict[str, int]]:
        """One canonical ordered edit implementation, shared by preview/export."""
        stats = {"erased_regions": 0, "highlights": 0, "removed_annotations": 0,
                 "optimized_images": 0, "removed_crossing_vectors": 0}
        page = work.load_page(index)
        # apply_redactions() processes *all* Redact annotations. Hide unrelated
        # pending redactions, retaining their original object and appearance.
        suspended: list[int] = []
        if any(action["kind"] == "erase" for action in page_actions):
            for annot in list(page.annots() or []):
                if annot.type[0] == fitz.PDF_ANNOT_REDACT:
                    suspended.append(annot.xref)
                    work.xref_set_key(annot.xref, "Subtype", "/Square")
            if suspended:
                page = work.reload_page(page)
        created_images: set[int] = set()
        try:
            for action in page_actions:
                visible = _rect(action["rect"]) & page.rect
                if visible.is_empty:
                    raise PdfEngineError(f"第 {index + 1} 页的选区位于页面之外。")
                region = visible * page.derotation_matrix
                if action["kind"] == "erase":
                    text_only = bool(action.get("text_only", False))
                    stats["removed_annotations"] += self._remove_overlapping_annotations(page, region, text_only=text_only)
                    candidates = Counter() if text_only else Counter(
                        self._drawing_key(drawing) for drawing in page.get_drawings()
                        if _overlap(fitz.Rect(drawing["rect"]), region)
                        and not region.contains(fitz.Rect(drawing["rect"]))
                    )
                    old_images = set() if text_only else {item[0] for item in page.get_images(full=True)}
                    page.add_redact_annot(region, fill=None if text_only else (1, 1, 1), cross_out=False)
                    if not page.apply_redactions(images=0 if text_only else 2,
                                                 graphics=0 if text_only else 2, text=0):
                        raise PdfEngineError(f"第 {index + 1} 页未能应用擦除。")
                    page = work.reload_page(page)
                    if candidates:
                        after = Counter(self._drawing_key(drawing) for drawing in page.get_drawings())
                        stats["removed_crossing_vectors"] += sum((candidates - after).values())
                    if not text_only:
                        created_images.update({item[0] for item in page.get_images(full=True)} - old_images)
                    stats["erased_regions"] += 1
                else:
                    quads = action.get("quads")
                    if quads:
                        transformed = [_quad(q) * page.derotation_matrix for q in quads]
                        annot = page.add_highlight_annot(transformed)
                    else:
                        annot = page.add_rect_annot(region)
                        annot.set_border(width=0)
                        annot.set_colors(stroke=None, fill=(1, 1, 0))
                        annot.set_blendmode("Multiply")
                    annot.set_opacity(0.38)
                    if quads:
                        annot.set_colors(stroke=(1, 1, 0))
                    annot.set_info(title="PDF 净擦", subject="黄色高亮")
                    annot.update()
                    stats["highlights"] += 1
            if optimize_images and created_images:
                stats["optimized_images"] += self._compress_new_images(work, page, created_images, warnings)
        finally:
            existing = {xref for xref, *_ in page.annot_xrefs()}
            for xref in suspended:
                if xref in existing:
                    work.xref_set_key(xref, "Subtype", "/Redact")
            if suspended:
                page = work.reload_page(page)
        if stats["removed_crossing_vectors"]:
            warnings.append(f"第 {index + 1} 页有 {stats['removed_crossing_vectors']} 个跨越擦除框的矢量图形已整体删除，其框外部分也随该路径移除。")
        return page, stats

    @staticmethod
    def _drawing_key(drawing: dict[str, Any]) -> tuple[Any, ...]:
        # A content-stream rewrite can change insignificant decimal precision.
        # Compare geometry and style, not sequence numbers which necessarily shift.
        def normalize(value: Any) -> Any:
            if isinstance(value, float):
                return round(value, 3)
            if isinstance(value, (list, tuple, fitz.Rect, fitz.Point, fitz.Quad)):
                return tuple(normalize(part) for part in value)
            return value
        return tuple(normalize(drawing.get(key)) for key in (
            "items", "type", "width", "color", "fill", "lineCap", "lineJoin",
            "dashes", "fill_opacity", "stroke_opacity", "even_odd"))

    def export(self, output_path: str | os.PathLike[str], actions: Iterable[dict[str, Any]],
               optimize_images: bool = False) -> dict[str, Any]:
        """Apply edit queue and atomically write a cleaned, independent PDF.

        Optional image optimization recompresses only images newly created by
        pixel erasure. It never rasterizes a page or downscales an untouched
        image. JPEG quality 90 is lossy, and is only used when its stream is
        smaller. In lossless mode, a small source can still grow: actual output
        size is reported rather than promising that every erasure shrinks it.
        """
        destination = Path(output_path).expanduser().resolve()
        if destination == self.path or (destination.exists() and self.path.exists()
                                        and os.path.samefile(destination, self.path)):
            raise PdfEngineError("请另存为新文件，原始 PDF 会保持不变。")
        if destination.suffix.lower() != ".pdf":
            raise PdfEngineError("导出文件需要使用 .pdf 扩展名。")
        if not destination.parent.is_dir():
            raise PdfEngineError("导出文件夹不存在。")
        grouped = self._normalize_actions(actions)
        count_erased = 0
        count_highlights = 0

        warnings: list[str] = []
        removed_annotations = 0
        compressed_images = 0
        removed_crossing_vectors = 0
        temp_name: str | None = None
        work = self._open_snapshot()
        try:
            for index, page_actions in grouped.items():
                _, stats = self._apply_page_actions(work, index, page_actions, optimize_images, warnings)
                count_erased += stats["erased_regions"]
                count_highlights += stats["highlights"]
                removed_annotations += stats["removed_annotations"]
                compressed_images += stats["optimized_images"]
                removed_crossing_vectors += stats["removed_crossing_vectors"]

            if removed_annotations:
                warnings.append(f"擦除范围内的 {removed_annotations} 个批注、链接或表单字段已一并删除。")

            # Redaction removes content operators. clean=True also sanitizes
            # resource dictionaries before garbage collection, preventing an
            # unused original scan from surviving solely as a page resource.
            handle, temp_name = tempfile.mkstemp(prefix=".pdf-clean-", suffix=".pdf", dir=destination.parent)
            os.close(handle)
            work.save(temp_name, incremental=False, garbage=4, clean=True,
                      deflate=True, deflate_images=True, deflate_fonts=True,
                      use_objstms=1, compression_effort=100, encryption=fitz.PDF_ENCRYPT_KEEP)
            # Read back the completed file before publishing it over any
            # existing destination. Source and destination are always distinct.
            check = fitz.open(temp_name)
            try:
                if check.needs_pass and not check.authenticate(self._password or ""):
                    raise PdfEngineError("导出 PDF 的密码校验未通过。")
                if check.page_count != self.page_count:
                    raise PdfEngineError("导出 PDF 的页数校验未通过。")
                for index in grouped:
                    check.load_page(index)
            finally:
                check.close()
            output_bytes = os.path.getsize(temp_name)
            os.replace(temp_name, destination)
            temp_name = None
            saved = self.source_size - output_bytes
            if saved <= 0:
                explanation = "本次文件未缩小：原文件可能已经压缩充分，或高亮、擦除图片的重写增加了数据。"
                if count_erased:
                    explanation += "擦除区域的原内容已实际删除。"
                warnings.append(explanation)
            if optimize_images and compressed_images:
                warnings.append(f"已对 {compressed_images} 幅擦除后生成的图片使用 JPEG 90 质量重压缩，存在轻微有损变化。")
            return {
                "source_bytes": self.source_size,
                "output_bytes": output_bytes,
                "percent_saved": saved * 100.0 / self.source_size if self.source_size else 0.0,
                "erased_regions": count_erased,
                "highlights": count_highlights,
                "removed_annotations": removed_annotations,
                "optimized_images": compressed_images,
                "removed_crossing_vectors": removed_crossing_vectors,
                "warnings": warnings,
                "output_path": str(destination),
            }
        except PdfEngineError:
            raise
        except Exception as exc:
            raise PdfEngineError(f"导出失败：{exc}") from exc
        finally:
            work.close()
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _remove_overlapping_annotations(page: fitz.Page, region: fitz.Rect, text_only: bool = False) -> int:
        removed = 0
        xrefs = [annot.xref for annot in page.annots() or []
                 if _overlap(annot.rect, region)
                 and (not text_only or annot.type[0] == fitz.PDF_ANNOT_HIGHLIGHT
                      or (annot.type[0] == fitz.PDF_ANNOT_SQUARE
                          and annot.info.get("subject") == "黄色高亮"))]
        for xref in xrefs:
            annot = page.load_annot(xref)
            page.delete_annot(annot)
            removed += 1
        if text_only:
            return removed
        for link in page.get_links():
            if _overlap(fitz.Rect(link["from"]), region):
                page.delete_link(link)
                removed += 1
        widget_xrefs = [widget.xref for widget in page.widgets() or [] if _overlap(widget.rect, region)]
        for xref in widget_xrefs:
            page.delete_widget(page.load_widget(xref))
            removed += 1
        return removed

    @staticmethod
    def _compress_new_images(doc: fitz.Document, page: fitz.Page, candidates: set[int],
                             warnings: list[str]) -> int:
        count = 0
        # Ignore obsolete intermediate versions created by earlier erasures.
        live = {item[0] for item in page.get_images(full=True)}
        for xref in sorted(candidates & live):
            try:
                extracted = doc.extract_image(xref)
                if not extracted or extracted.get("smask", 0) or extracted.get("width", 0) < 64:
                    continue
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.colorspace is None:
                    continue
                if pix.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                jpeg = pix.tobytes("jpeg", jpg_quality=90)
                current = doc.xref_stream_raw(xref)
                if len(jpeg) + 256 < len(current):
                    page.replace_image(xref, stream=jpeg)
                    count += 1
            except Exception as exc:
                warnings.append(f"一幅图片无法重压缩，已保留无损擦除结果：{exc}")
        return count
