"""Native-style character range selection for the PDF editor.

This module never modifies the loaded PDF. It builds an immutable text snapshot
from the same real editing helper as export, then selects a contiguous range in
the PDF extractor's block/line/span reading order, rather than a rectangular
collection of all text touched by the mouse drag.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import pymupdf as fitz

from pdf_engine import PdfDocument, PdfEngineError, _flat_quad


def _text_quad(direction: tuple[float, float], span: dict[str, Any],
               characters: list[dict[str, Any]]) -> fitz.Quad:
    """Keep axis-aligned glyph boxes exact, including fonts with bad metrics.

    recover_span_quad derives height from font ascender/descender. Some PDFs
    expose inconsistent metrics and that helper produces a slanted rectangle
    even for a horizontal baseline, potentially touching a neighbouring glyph.
    The extracted glyph bbox is exact for the four cardinal text directions.
    """
    dx, dy = direction
    if abs(dx) < 1e-5 or abs(dy) < 1e-5:
        bounds = fitz.Rect(characters[0]["bbox"])
        for character in characters[1:]:
            bounds |= fitz.Rect(character["bbox"])
        dx = 0.0 if abs(dx) < 1e-5 else (1.0 if dx > 0 else -1.0)
        dy = 0.0 if abs(dy) < 1e-5 else (1.0 if dy > 0 else -1.0)
        nx, ny = -dy, dx
        corners = (bounds.tl, bounds.tr, bounds.bl, bounds.br)
        along = [point.x * dx + point.y * dy for point in corners]
        across = [point.x * nx + point.y * ny for point in corners]

        def point(u: float, v: float) -> fitz.Point:
            return fitz.Point(u * dx + v * nx, u * dy + v * ny)

        return fitz.Quad(point(min(along), min(across)), point(max(along), min(across)),
                         point(min(along), max(across)), point(max(along), max(across)))
    return fitz.recover_span_quad(direction, span, chars=characters)


class TextSelector:
    """Cache the current page's real remaining text for repeated mouse drags.

    ``pdf`` is a PdfDocument; ``index`` is zero based; ``actions`` may be the
    editor's entire queue. Recreate the selector after an edit, undo, or page
    change. All public coordinates are in the visible rotated page's point
    space, matching PdfDocument.render_preview(). No OCR is performed.
    """

    def __init__(self, pdf: PdfDocument, index: int,
                 actions: Iterable[dict[str, Any]] | None = None):
        pdf._page(index)  # Validate the page even when the action queue is empty.
        self.index = index
        self._chars: list[dict[str, Any]] = []
        relevant = [dict(action) for action in (actions or ())
                    if isinstance(action, dict) and action.get("page") == index]
        work = pdf._open_snapshot()
        try:
            if relevant:
                normalized = pdf._normalize_actions(relevant)
                page, _ = pdf._apply_page_actions(work, index, normalized[index], False, [])
                page.clean_contents(sanitize=True)
            else:
                page = work.load_page(index)
            self.page_size = (page.rect.width, page.rect.height)
            matrix = page.rotation_matrix
            flags = getattr(fitz, "TEXTFLAGS_RAWDICT", 199) & ~getattr(fitz, "TEXT_PRESERVE_IMAGES", 4)
            raw = page.get_text("rawdict", flags=flags, sort=False)
            # Preserve content/extractor block order. Sorting all lines by Y
            # would interleave the two columns of many scientific papers.
            for block_index, block in enumerate(raw.get("blocks", ())):
                if block.get("type") != 0:
                    continue
                for line_index, line in enumerate(block.get("lines", ())):
                    direction = line.get("dir", (1.0, 0.0))
                    for span_index, span in enumerate(line.get("spans", ())):
                        span_id = (block_index, line_index, span_index)
                        for char_index, character in enumerate(span.get("chars", ())):
                            text = character.get("c", "")
                            if not text:
                                continue
                            quad = _text_quad(direction, span, [character]) * matrix
                            bounds = quad.rect
                            if bounds.is_empty or bounds.is_infinite:
                                continue
                            self._chars.append({
                                "text": text,
                                "quad": quad,
                                "rect": bounds,
                                "block": block_index,
                                "line": (block_index, line_index),
                                "span_id": span_id,
                                "span": span,
                                "direction": direction,
                                "character": character,
                                "char_index": char_index,
                                "matrix": matrix,
                            })
        except PdfEngineError:
            raise
        except Exception as exc:
            raise PdfEngineError(f"无法读取页面文字：{exc}") from exc
        finally:
            work.close()
        self.has_text = any(character["text"].strip() for character in self._chars)
        self.character_count = len(self._chars)

    @staticmethod
    def _point(value: Iterable[float]) -> fitz.Point:
        try:
            coordinates = tuple(float(v) for v in value)
        except (TypeError, ValueError) as exc:
            raise PdfEngineError("文字选择坐标无效。") from exc
        if len(coordinates) != 2 or not all(math.isfinite(v) for v in coordinates):
            raise PdfEngineError("文字选择需要两个有效坐标。")
        return fitz.Point(coordinates)

    def closest_character(self, point: Iterable[float], max_distance: float | None = None) -> dict[str, Any] | None:
        """Return the nearest visible character and its distance in page points.

        Distance is to the character's bounding rectangle (zero when inside).
        Passing max_distance can reject a mouse-down in empty margins; selection
        itself permits a far end point so dragging beyond a line still works.
        """
        if not self.has_text:
            return None
        target = self._point(point)
        best_index = -1
        best_key = (float("inf"), float("inf"))
        for index, character in enumerate(self._chars):
            bounds = character["rect"]
            dx = max(bounds.x0 - target.x, 0.0, target.x - bounds.x1)
            dy = max(bounds.y0 - target.y, 0.0, target.y - bounds.y1)
            distance_squared = dx * dx + dy * dy
            center = (bounds.tl + bounds.br) * 0.5
            center_squared = (center.x - target.x) ** 2 + (center.y - target.y) ** 2
            key = (distance_squared, center_squared)
            if key < best_key:
                best_key = key
                best_index = index
        if best_index < 0:
            return None
        distance = math.sqrt(best_key[0])
        if max_distance is not None and distance > max_distance:
            return None
        character = self._chars[best_index]
        return {"index": best_index, "text": character["text"],
                "rect": list(character["rect"]), "quad": _flat_quad(character["quad"]),
                "distance": distance}

    def select(self, start: Iterable[float], end: Iterable[float]) -> dict[str, Any] | None:
        """Select an inclusive character range in reading order, forward/backward.

        Output quads are per contiguous span. Erasure rectangles are minimally
        inset by 0.1 pt to avoid touching an adjacent character on a shared edge.
        Bounds remains the full visual selection extent for popup placement.
        """
        first = self.closest_character(start)
        last = self.closest_character(end)
        if first is None or last is None:
            return None
        low, high = sorted((first["index"], last["index"]))
        selected = self._chars[low:high + 1]
        if not any(character["text"].strip() for character in selected):
            return None

        quads: list[list[float]] = []
        rectangles: list[list[float]] = []
        bounds: fitz.Rect | None = None
        run: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal bounds
            if not run:
                return
            lead = run[0]
            quad = _text_quad(lead["direction"], lead["span"],
                              [character["character"] for character in run]) * lead["matrix"]
            if quad.is_empty or not quad.is_convex:
                run.clear()
                return
            quads.append(_flat_quad(quad))
            area = quad.rect
            bounds = fitz.Rect(area) if bounds is None else bounds | area
            # Characters have touching glyph boxes. Redaction removes every
            # glyph with any intersection, so keep away from the shared edge.
            inset_x = min(0.1, area.width * 0.1)
            inset_y = min(0.1, area.height * 0.1)
            rectangles.append([area.x0 + inset_x, area.y0 + inset_y,
                               area.x1 - inset_x, area.y1 - inset_y])
            run.clear()

        for character in selected:
            if run and (character["span_id"] != run[-1]["span_id"]
                        or character["char_index"] != run[-1]["char_index"] + 1):
                flush()
            run.append(character)
        flush()
        if not quads or bounds is None:
            return None

        text_parts: list[str] = []
        previous: dict[str, Any] | None = None
        for character in selected:
            if previous is not None and character["line"] != previous["line"]:
                text_parts.append("\n\n" if character["block"] != previous["block"] else "\n")
            text_parts.append(character["text"])
            previous = character
        return {"quads": quads, "rects": rectangles, "text": "".join(text_parts),
                "bounds": list(bounds)}
