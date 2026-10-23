"""PDF editing backend: real text removal and reading highlight annotations.

All public coordinates use the visible, rotated page (the same space as
render_page). The loaded session baseline stays immutable. Export writes a
distinct copy; explicit save_in_place atomically replaces the source after
checking for external changes. Both replay the queued edits on an independent
snapshot and fully rewrite it with unused objects removed.
"""

from __future__ import annotations

import math
import hashlib
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import pymupdf as fitz


HIGHLIGHT_STYLES: dict[str, dict[str, Any]] = {
    "highlight": {
        "label": "高亮", "color": (1.0, 1.0, 0.0), "rgb": (1.0, 1.0, 0.0),
        "opacity": 0.38, "swatch": "#E1C900", "hover": "#FFF8BE",
    },
    "confused": {
        "label": "困惑", "color": (0.64, 0.40, 0.98), "rgb": (0.64, 0.40, 0.98),
        "opacity": 0.38, "swatch": "#A366FA", "hover": "#EFE3FF",
    },
    "useful": {
        "label": "有用", "color": (0.25, 0.80, 0.45), "rgb": (0.25, 0.80, 0.45),
        "opacity": 0.38, "swatch": "#40CC73", "hover": "#DDF8E6",
    },
}


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


def _pdf_number(value: float) -> str:
    """Write an ordinary PDF number, never scientific notation."""
    result = f"{value:.8f}".rstrip("0").rstrip(".")
    return "0" if result in ("", "-0") else result


def _highlight_quad_points(doc: fitz.Document, annot: fitz.Annot) -> list[tuple[float, ...]]:
    """Read native PDF coordinates, independent of page rotation or crop box."""
    kind, value = doc.xref_get_key(annot.xref, "QuadPoints")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
        kind = "array" if value.lstrip().startswith("[") else kind
    if kind != "array":
        raise PdfEngineError("高亮批注缺少有效的文字选区。")
    # xref_get_key canonicalizes the PDF array. Check the whole representation
    # before parsing so indirect references or non-numeric array entries cannot
    # silently become coordinates.
    numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    if not re.fullmatch(r"\[\s*(?:" + numeric + r"\s*)+\]", value):
        raise PdfEngineError("高亮批注的文字选区无效。")
    values = [float(number) for number in re.findall(numeric, value)]
    if len(values) % 8 or not values or not all(math.isfinite(number) for number in values):
        raise PdfEngineError("高亮批注的文字选区无效。")
    return [tuple(values[offset:offset + 8]) for offset in range(0, len(values), 8)]


def _has_flat_highlight_appearance(doc: fitz.Document, annot: fitz.Annot) -> bool:
    kind, value = doc.xref_get_key(annot.xref, "AP/N")
    if kind != "xref":
        return False
    appearance = int(value.split()[0])
    return doc.xref_get_key(appearance, "PDFCleanFlatHighlight") == ("name", "/V1")


def _set_flat_highlight_appearance(doc: fitz.Document, annot: fitz.Annot) -> None:
    """Retain a standard Highlight annotation with four straight quad edges.

    MuPDF's normal Highlight appearance adds Bezier caps outside QuadPoints.
    Use the annotation's actual PDF QuadPoints for a filled polygon instead.
    Keeping PDF coordinates and an identity appearance Matrix avoids assumptions
    about rotated/cropped pages and also retains slanted text quads exactly.
    """
    quads = _highlight_quad_points(doc, annot)
    xs = [quad[index] for quad in quads for index in (0, 2, 4, 6)]
    ys = [quad[index] for quad in quads for index in (1, 3, 5, 7)]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise PdfEngineError("高亮批注的文字选区不能为空。")
    rect = "[" + " ".join(_pdf_number(number) for number in bounds) + "]"
    color = annot.colors.get("stroke") or (1.0, 1.0, 0.0)
    if len(color) == 1:
        paint = f"{_pdf_number(color[0])} g"
    elif len(color) == 3:
        paint = " ".join(_pdf_number(number) for number in color) + " rg"
    elif len(color) == 4:
        paint = " ".join(_pdf_number(number) for number in color) + " k"
    else:
        raise PdfEngineError("高亮批注的颜色无效。")
    opacity = annot.opacity
    opacity = opacity if 0 <= opacity <= 1 else 1.0
    alpha = _pdf_number(opacity)
    commands = ["q", "/GS0 gs", paint]
    for quad in quads:
        # PDF text markup QuadPoints use UL, UR, LL, LR, not perimeter order.
        for position, point_index in enumerate((0, 2, 6, 4)):
            commands.append(f"{_pdf_number(quad[point_index])} "
                            f"{_pdf_number(quad[point_index + 1])} {'m' if position == 0 else 'l'}")
        commands.append("h")
    commands.extend(("f", "Q", ""))
    # Each annotation owns its new Form. An old PDF can share appearance
    # streams, so replacing a shared stream could alter an unrelated annotation.
    appearance = doc.get_new_xref()
    doc.update_object(appearance, (
        "<< /Type /XObject /Subtype /Form /FormType 1 "
        f"/BBox {rect} /Matrix [1 0 0 1 0 0] "
        "/PDFCleanFlatHighlight /V1 "
        "/Resources << /ExtGState << /GS0 << /Type /ExtGState "
        f"/CA {alpha} /ca {alpha} /BM /Multiply >> >> >> >>"
    ))
    doc.update_stream(appearance, "\n".join(commands).encode("ascii"))
    doc.xref_set_key(annot.xref, "AP/N", f"{appearance} 0 R")
    doc.xref_set_key(annot.xref, "Rect", rect)
    doc.xref_set_key(annot.xref, "BM", "/Multiply")


def _flatten_owned_highlights(doc: fitz.Document) -> int:
    """Upgrade this app's older highlights in memory; leave other authors alone."""
    labels = {style["label"] for style in HIGHLIGHT_STYLES.values()} | {"黄色高亮"}
    upgraded = 0
    for page in doc:
        changed = False
        for annot in list(page.annots() or []):
            info = annot.info
            if (annot.type[0] != fitz.PDF_ANNOT_HIGHLIGHT
                    or info.get("title") != "PDF 净擦"
                    or not ({info.get("subject"), info.get("content")} & labels)
                    or _has_flat_highlight_appearance(doc, annot)):
                continue
            _set_flat_highlight_appearance(doc, annot)
            changed = True
            upgraded += 1
        if changed:
            doc.reload_page(page)
    return upgraded


class PdfDocument:
    """An immutable session baseline with export and atomic original saving."""

    def __init__(self, path: str | os.PathLike[str], password: str | None = None):
        self.path = Path(path).expanduser().resolve()
        self._password = password
        self.needs_highlight_upgrade = False
        self._source_data = self.path.read_bytes()
        self.source_size = len(self._source_data)
        self._last_disk_state = self._fingerprint(self.path)
        self._last_known_hash = self._last_disk_state["sha256"]
        if self._last_known_hash != hashlib.sha256(self._source_data).hexdigest():
            raise PdfEngineError("原 PDF 在读取期间发生了变化，请重新打开后再编辑。")
        self.doc = self._open_snapshot(track_highlight_upgrade=True)
        if not self.doc.is_pdf:
            self.doc.close()
            raise PdfEngineError("请选择 PDF 文件。")
        self.page_count = self.doc.page_count
        if not self.page_count:
            self.doc.close()
            raise PdfEngineError("这份 PDF 没有可编辑的页面。")

    @staticmethod
    def _fingerprint(path: Path) -> dict[str, Any]:
        """Hash file contents and identity; reject a change during the read."""
        def signature(stat: os.stat_result) -> tuple[int, ...]:
            # On Windows Python 3.12, stat() and fstat() can expose different
            # legacy st_ctime meanings. Their explicit birthtime agrees.
            creation_or_change = getattr(stat, "st_birthtime_ns", stat.st_ctime_ns)
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, creation_or_change)

        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if signature(before) != signature(opened):
                raise PdfEngineError("原 PDF 在读取期间被替换，请重新打开。")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after_read = os.fstat(stream.fileno())
        after = path.stat()
        if signature(before) != signature(after_read) or signature(before) != signature(after):
            raise PdfEngineError("原 PDF 在读取期间发生了变化，请重新打开。")
        return {"signature": signature(after), "sha256": digest.hexdigest(), "size": after.st_size}

    def _assert_source_unchanged(self) -> None:
        try:
            current = self._fingerprint(self.path)
        except FileNotFoundError as exc:
            raise PdfEngineError("原 PDF 已被移动或删除，无法原地保存。请重新打开文件或另存副本。") from exc
        except PermissionError as exc:
            raise PdfEngineError("无法读取原 PDF 的当前状态，文件可能被其他程序占用。请解除占用后重试。") from exc
        if current != self._last_disk_state:
            raise PdfEngineError("原 PDF 已被其他程序修改或替换，已阻止覆盖。请重新打开文件，或将当前编辑另存为副本。")

    def _open_snapshot(self, *, track_highlight_upgrade: bool = False) -> fitz.Document:
        try:
            doc = fitz.open(stream=self._source_data, filetype="pdf")
            if doc.needs_pass and (not self._password or not doc.authenticate(self._password)):
                doc.close()
                raise PdfEngineError("这份 PDF 需要正确的打开密码。")
            upgraded = _flatten_owned_highlights(doc) if doc.is_pdf else 0
            if track_highlight_upgrade:
                self.needs_highlight_upgrade = bool(upgraded)
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
            if kind == "highlight":
                style = action.get("style", "highlight")
                if not isinstance(style, str) or style not in HIGHLIGHT_STYLES:
                    raise PdfEngineError("不支持的高亮类别，请选择高亮、困惑或有用。")
                clean_action["style"] = style
                if action.get("quads"):
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
                    style_key = action.get("style", "highlight")
                    if not isinstance(style_key, str) or style_key not in HIGHLIGHT_STYLES:
                        raise PdfEngineError("不支持的高亮类别，请选择高亮、困惑或有用。")
                    style = HIGHLIGHT_STYLES[style_key]
                    quads = action.get("quads")
                    if quads:
                        transformed = [_quad(q) * page.derotation_matrix for q in quads]
                        annot = page.add_highlight_annot(transformed)
                    else:
                        annot = page.add_rect_annot(region)
                        annot.set_border(width=0)
                        annot.set_colors(stroke=None, fill=style["color"])
                        annot.set_blendmode("Multiply")
                    annot.set_opacity(style["opacity"])
                    if quads:
                        annot.set_colors(stroke=style["color"])
                    annot.set_info(title="PDF 净擦", subject=style["label"], content=style["label"])
                    annot.update()
                    if quads:
                        _set_flat_highlight_appearance(work, annot)
                        page = work.reload_page(page)
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
        return self._save(output_path, actions, optimize_images, in_place=False)

    def save_in_place(self, actions: Iterable[dict[str, Any]],
                      optimize_images: bool = False) -> dict[str, Any]:
        """Atomically replace the source PDF after checking for outside changes.

        The initial session bytes remain the replay baseline after saving. Thus
        repeated saves, appended edits, undo and redo always rebuild the full
        intended result without applying old edits twice to a previously saved
        PDF. Only the expected on-disk fingerprint is advanced on success.
        """
        return self._save(self.path, actions, optimize_images, in_place=True)

    def _save(self, output_path: str | os.PathLike[str], actions: Iterable[dict[str, Any]],
              optimize_images: bool, *, in_place: bool) -> dict[str, Any]:
        destination = Path(output_path).expanduser().resolve()
        if not in_place and (destination == self.path or (destination.exists() and self.path.exists()
                                                        and os.path.samefile(destination, self.path))):
            raise PdfEngineError("请另存为新文件，原始 PDF 会保持不变。")
        if destination.suffix.lower() != ".pdf":
            raise PdfEngineError("导出文件需要使用 .pdf 扩展名。")
        if not destination.parent.is_dir():
            raise PdfEngineError("导出文件夹不存在。")
        if in_place:
            self._assert_source_unchanged()
        source_bytes = self._last_disk_state["size"] if in_place else self.source_size
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
            # Validate the completed temporary file before replacing the
            # destination, including the original path for in-place saves.
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
            replacement_hash = None
            if in_place:
                # Recheck immediately before publishing; a second application
                # may have saved while this full rewrite was being prepared.
                replacement_hash = self._fingerprint(Path(temp_name))["sha256"]
                self._assert_source_unchanged()
            try:
                os.replace(temp_name, destination)
            except PermissionError as exc:
                if in_place:
                    raise PdfEngineError("原 PDF 可能正被其他程序占用，无法原地替换。原文件未改动，请关闭占用后重试，或另存为副本。") from exc
                raise
            temp_name = None
            if in_place:
                current_state = self._fingerprint(self.path)
                if current_state["sha256"] != replacement_hash:
                    raise PdfEngineError("保存完成后，原 PDF 又被其他程序修改。请重新打开文件再继续编辑。")
                self._last_disk_state = current_state
                self._last_known_hash = current_state["sha256"]
                self.needs_highlight_upgrade = False
            saved = source_bytes - output_bytes
            if saved <= 0:
                explanation = "本次文件未缩小：原文件可能已经压缩充分，或高亮、擦除图片的重写增加了数据。"
                if count_erased:
                    explanation += "擦除区域的原内容已实际删除。"
                warnings.append(explanation)
            if optimize_images and compressed_images:
                warnings.append(f"已对 {compressed_images} 幅擦除后生成的图片使用 JPEG 90 质量重压缩，存在轻微有损变化。")
            return {
                "source_bytes": source_bytes,
                "output_bytes": output_bytes,
                "percent_saved": saved * 100.0 / source_bytes if source_bytes else 0.0,
                "erased_regions": count_erased,
                "highlights": count_highlights,
                "removed_annotations": removed_annotations,
                "optimized_images": compressed_images,
                "removed_crossing_vectors": removed_crossing_vectors,
                "warnings": warnings,
                "output_path": str(destination),
                "in_place": in_place,
            }
        except PdfEngineError:
            raise
        except Exception as exc:
            raise PdfEngineError(f"{'保存' if in_place else '导出'}失败：{exc}") from exc
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
                          and (annot.info.get("subject") == "黄色高亮"
                               or (annot.info.get("title") == "PDF 净擦"
                                   and annot.info.get("subject") in
                                   {style["label"] for style in HIGHLIGHT_STYLES.values()}))))]
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
