from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .text import normalize_text


PARSER_VERSION = "3.3.0"
MERGE_RULES_VERSION = "natural-paragraph-v3"
TERMINAL_PUNCTUATION = re.compile(r"[.!?。！？；;][\"'”’》〉】）)]*$")
LIST_PREFIX = re.compile(
    r"^(?:[\-‒•●▪◦·]|\(?\d+[.)、]|[A-Za-z][.)]|[一-鿿]、)\s*"
)
ONLY_VALUE = re.compile(r"^[\s\d.,%/+:()\-–—NAnanotCO₂eHK$m³]+$")


@dataclass
class ParagraphCandidate:
    start_page: int
    pages: list[int]
    text: str
    is_heading: bool
    top: float
    bottom: float
    left: float
    right: float
    page_width: float
    page_height: float
    font_size: float = 0.0
    line_height: float = 0.0
    column: int = 0
    kind: str = "body"
    local_heading: str = ""
    source_block_count: int = 1
    source: str = "pymupdf"
    source_id: str = ""
    source_ids: list[str] = field(default_factory=list)
    source_parsers: list[str] = field(default_factory=list)
    language: str = "unknown"


def _join_text(left: str, right: str) -> str:
    left = re.sub(r"\s+", " ", left).strip()
    right = re.sub(r"\s+", " ", right).strip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") and re.match(r"^[a-z]", right):
        # PDF line wrapping often splits a word after a visible hyphen. Keep
        # the source glyph: removing it would corrupt genuine compounds.
        return left + right
    if re.search(r"[㐀-鿿]$", left) and re.match(r"^[㐀-鿿]", right):
        return left + right
    return left + " " + right


def _collapse_lines(lines: Iterable[str]) -> str:
    value = ""
    for line in lines:
        value = _join_text(value, line)
    return normalize_text(value).replace("\n", " ")


def _block_metadata(
    block: dict[str, Any]
) -> tuple[str, float, float, bool, int]:
    lines: list[str] = []
    sizes: list[float] = []
    heights: list[float] = []
    rotated = False
    for line in block.get("lines", []):
        direction = line.get("dir", (1.0, 0.0))
        if abs(float(direction[1])) > 0.2:
            rotated = True
        parts: list[str] = []
        bbox = line.get("bbox", (0, 0, 0, 0))
        heights.append(max(0.0, float(bbox[3]) - float(bbox[1])))
        for span in line.get("spans", []):
            text = str(span.get("text", ""))
            if text.strip():
                parts.append(text)
                sizes.append(float(span.get("size", 0.0)))
        if parts:
            lines.append("".join(parts))
    return (
        _collapse_lines(lines),
        median(sizes) if sizes else 0.0,
        median(heights) if heights else 0.0,
        rotated,
        len(lines),
    )


def _looks_like_heading(text: str, font_size: float, body_font: float) -> bool:
    if not text or len(text) > 180 or LIST_PREFIX.match(text):
        return False
    if font_size >= max(body_font + 1.4, body_font * 1.18):
        return True
    letters = re.sub(r"[^A-Za-z]", "", text)
    return bool(
        letters
        and len(text) <= 100
        and text.upper() == text
        and len(letters) >= 5
    )


def _is_decorative_or_margin_block(
    *, bbox: tuple[float, float, float, float], page_width: float,
    page_height: float, text: str, rotated: bool,
) -> bool:
    x0, y0, x1, y1 = bbox
    width = max(0.0, x1 - x0)
    height = max(0.0, y1 - y0)
    if y1 >= page_height * 0.945 or y0 <= page_height * 0.035:
        return True
    if rotated:
        return True
    if width <= page_width * 0.075 and height >= page_height * 0.08:
        return True
    if (x1 <= page_width * 0.075 or x0 >= page_width * 0.925) and height > width:
        return True
    return bool(re.fullmatch(r"(?:page\s*)?\d{1,4}", text, re.IGNORECASE))


def _body_font(raw: list[tuple[dict[str, Any], str, float, float, int]]) -> float:
    weighted: list[float] = []
    for _, text, size, _, _ in raw:
        if 5 <= size <= 14:
            weighted.extend([round(size, 1)] * min(max(len(text) // 30, 1), 20))
    return median(weighted) if weighted else 9.0


def _table_grid_candidates(
    page: Any, page_number: int
) -> tuple[list[ParagraphCandidate], list[tuple[float, float, float, float]]]:
    candidates: list[ParagraphCandidate] = []
    boxes: list[tuple[float, float, float, float]] = []
    try:
        tables = page.find_tables().tables
    except Exception:
        return candidates, boxes
    for table in tables:
        bbox = tuple(float(value) for value in table.bbox)
        data = table.extract()
        if not data or not _is_semantic_table(
            data, bbox=bbox, page_height=float(page.rect.height)
        ):
            continue
        boxes.append(bbox)
        for index, row in enumerate(data):
            cells = [
                re.sub(r"\s+", " ", str(value)).strip()
                for value in row if value is not None and str(value).strip()
            ]
            # Normalize the complete row only after joining its cells. Calling
            # normalize_text on a standalone numeric cell would mistake a
            # legitimate category number for a page-number line and remove it.
            text = _collapse_lines(cells)
            if not text:
                continue
            row_bbox = bbox
            if index < len(table.rows):
                row_bbox = tuple(float(value) for value in table.rows[index].bbox)
            candidates.append(ParagraphCandidate(
                start_page=page_number, pages=[page_number], text=text,
                is_heading=False, top=row_bbox[1], bottom=row_bbox[3],
                left=row_bbox[0], right=row_bbox[2],
                page_width=float(page.rect.width), page_height=float(page.rect.height),
                font_size=8.0, line_height=max(1.0, row_bbox[3] - row_bbox[1]),
                column=-1, kind="table",
            ))
    return candidates, boxes


def _external_table_candidates(
    page: Any,
    page_number: int,
    tables: list[dict[str, Any]],
) -> tuple[list[ParagraphCandidate], list[tuple[float, float, float, float]]]:
    candidates: list[ParagraphCandidate] = []
    boxes: list[tuple[float, float, float, float]] = []
    for table in tables:
        bbox = tuple(float(value) for value in table.get("bbox", []))
        if len(bbox) != 4:
            continue
        rows = [str(value).strip() for value in table.get("rows", []) if str(value).strip()]
        if not rows:
            continue
        boxes.append(bbox)
        row_height = max(1.0, (bbox[3] - bbox[1]) / len(rows))
        for index, text in enumerate(rows):
            top = bbox[1] + index * row_height
            bottom = bbox[3] if index == len(rows) - 1 else top + row_height
            candidates.append(
                ParagraphCandidate(
                    start_page=page_number,
                    pages=[page_number],
                    text=text,
                    is_heading=False,
                    top=top,
                    bottom=bottom,
                    left=bbox[0],
                    right=bbox[2],
                    page_width=float(page.rect.width),
                    page_height=float(page.rect.height),
                    font_size=8.0,
                    line_height=row_height,
                    column=-1,
                    kind="table",
                    source="chandra_ocr",
                    source_id=str(table.get("source_table_block_id") or table.get("external_table_key") or ""),
                )
            )
    return candidates, boxes


def _is_semantic_table(
    data: list[list[Any]], *,
    bbox: tuple[float, float, float, float] | None = None,
    page_height: float | None = None,
) -> bool:
    """Reject chart/card layout grids while retaining meaningful data tables."""
    rows = [
        [str(value).strip() for value in row if value is not None and str(value).strip()]
        for row in data
    ]
    rows = [row for row in rows if row]
    if len(rows) < 2:
        return False
    cells = [cell for row in rows for cell in row]
    alphabetic = sum(character.isalpha() for cell in cells for character in cell)
    longest = max((len(cell) for cell in cells), default=0)
    total = sum(len(cell) for cell in cells)
    column_count = max((len(row) for row in data), default=0)
    multi_cell_rows = sum(len(row) >= 2 for row in rows)
    textual_rows = sum(any(sum(character.isalpha() for character in cell) >= 3 for cell in row) for row in rows)
    height_ratio = (
        (bbox[3] - bbox[1]) / page_height
        if bbox is not None and page_height else 0.0
    )
    giant_cell_ratio = longest / max(total, 1)

    # Decorative case-study cards and background panels are frequently
    # reported as a shallow 2-column table whose final cell contains almost
    # the entire page. That is a layout container, not a semantic table.
    if (
        height_ratio >= 0.45 and len(data) <= 3 and column_count <= 2
        and giant_cell_ratio >= 0.65
    ):
        return False
    # Sparse panels covering a material part of a page are another common
    # false positive. Keep compact two-row tables such as page 27.
    if height_ratio >= 0.12 and len(cells) <= 2 and multi_cell_rows < 2:
        return False
    # ESG charts are often detected as large numeric grids. Real disclosure
    # tables normally have either a descriptive cell or several textual rows.
    return alphabetic >= 20 and (longest >= 20 or textual_rows >= 2)


def _inside_table(
    bbox: tuple[float, float, float, float],
    table_boxes: list[tuple[float, float, float, float]],
) -> bool:
    x0, y0, x1, y1 = bbox
    area = max(1.0, (x1 - x0) * (y1 - y0))
    for tx0, ty0, tx1, ty1 in table_boxes:
        overlap = max(0.0, min(x1, tx1) - max(x0, tx0)) * max(
            0.0, min(y1, ty1) - max(y0, ty0)
        )
        if overlap / area >= 0.55:
            return True
    return False


def _assign_columns(items: list[ParagraphCandidate]) -> None:
    if not items:
        return
    width = items[0].page_width
    narrow = [
        item for item in items
        if not item.is_heading and item.right - item.left <= width * 0.58
        and len(item.text) >= 20
    ]
    left = [item for item in narrow if item.left < width * 0.42]
    right = [item for item in narrow if item.left > width * 0.42]
    two_columns = bool(
        len(left) >= 2 and len(right) >= 2
        and median(item.left for item in right) - median(item.left for item in left)
        > width * 0.25
    )
    for item in items:
        crosses_midline = item.left < width * 0.46 < item.right
        if item.right - item.left >= width * 0.72 or (
            item.is_heading and crosses_midline
        ):
            item.column = -1
        elif two_columns:
            item.column = 0 if (item.left + item.right) / 2 < width / 2 else 1
        else:
            item.column = 0


def _classify_structured(items: list[ParagraphCandidate]) -> None:
    if not items:
        return
    width = items[0].page_width
    landscape = width > items[0].page_height * 1.15
    dense_page = len(items) >= 24
    for item in items:
        if item.kind in {"table", "ocr"}:
            continue
        if item.is_heading:
            item.kind = "heading"
        elif LIST_PREFIX.match(item.text):
            item.kind = "list"
        elif (
            landscape and dense_page
            or (item.right - item.left > width * 0.7 and dense_page)
            or item.text.count(" N/A ") >= 2
            or (
                dense_page and len(re.findall(r"\d", item.text)) >= 8
                and len(item.text) < 600
            )
        ):
            item.kind = "table"
        elif dense_page and (
            ONLY_VALUE.fullmatch(item.text)
            or (len(item.text) < 20 and any(character.isdigit() for character in item.text))
        ):
            item.kind = "figure"

    # A PDF table often emits each cell as an independent block. If blocks in
    # different horizontal regions occupy the same visual row, both are table
    # cells even when one contains no numbers.
    for index, item in enumerate(items):
        if item.is_heading:
            continue
        for other in items[index + 1:]:
            if other.is_heading:
                continue
            overlap = min(item.bottom, other.bottom) - max(item.top, other.top)
            height = max(1.0, min(item.bottom - item.top, other.bottom - other.top))
            horizontally_separate = (
                item.right <= other.left + 8 or other.right <= item.left + 8
            )
            separate_narrative_columns = {item.column, other.column} == {0, 1}
            if (
                overlap / height >= 0.7 and horizontally_separate
                and not separate_narrative_columns
                and (dense_page or item.kind in {"table", "figure"} or other.kind in {"table", "figure"})
            ):
                item.kind = "table"
                other.kind = "table"


def _reading_order(items: list[ParagraphCandidate]) -> list[ParagraphCandidate]:
    """Order columns without moving a full-width separator past nearby text."""
    separators = sorted(
        (item for item in items if item.column == -1), key=lambda item: item.top
    )

    def key(item: ParagraphCandidate) -> tuple[int, int, float, float]:
        band = sum(separator.top < item.top for separator in separators)
        if item.column == -1:
            # A full-width separator closes the current column band. It must
            # follow content above it and precede content below it.
            return (band, 2, item.top, item.left)
        return (band, item.column, item.top, item.left)

    return sorted(items, key=key)


def _same_style(left: ParagraphCandidate, right: ParagraphCandidate) -> bool:
    return abs(left.font_size - right.font_size) <= max(0.8, left.font_size * 0.1)


def _compatible_language(left: ParagraphCandidate, right: ParagraphCandidate) -> bool:
    return (
        left.language == "unknown"
        or right.language == "unknown"
        or left.language == right.language
    )


def _can_merge_body(left: ParagraphCandidate, right: ParagraphCandidate) -> bool:
    if left.is_heading or right.is_heading or left.kind != "body" or right.kind != "body":
        return False
    if left.start_page != right.start_page:
        return False
    if LIST_PREFIX.match(right.text):
        return False
    if not _same_style(left, right) or not _compatible_language(left, right):
        return False
    column_wrap = (
        left.column == 0 and right.column == 1
        and left.bottom >= left.page_height * 0.30
        and right.top <= right.page_height * 0.32
    )
    full_width_tail = (
        left.column == -1 and right.column in {0, 1}
        and left.right - left.left >= left.page_width * 0.7
        and len(right.text) <= 80
    )
    if left.column != right.column and not (column_wrap or full_width_tail):
        return False
    gap = right.top - left.bottom
    normal = max(left.line_height, right.line_height, left.font_size, 1.0)
    if not column_wrap and (gap < -normal * 0.45 or gap > normal * 1.8):
        return False
    left_edge_close = abs(left.left - right.left) <= max(10.0, left.page_width * 0.025)
    # A sentence-ending mark is not a paragraph boundary.  PDF parsers often
    # emit one sentence per layout block.  Geometry/indentation decides the
    # paragraph; punctuation only relaxes or tightens the allowable gap.
    visibly_indented = right.left - left.left > max(12.0, left.font_size * 1.6)
    tight_same_track = (
        left.column == right.column
        and left_edge_close
        and gap <= normal * 0.80
        and not visibly_indented
    )
    continuation = bool(
        re.match(r"^[a-z㐀-鿿]", right.text.lstrip())
        or re.search(r"(?:and|or|of|to|for|with|及|与|并|和|的|为)$", left.text, re.I)
    )
    if tight_same_track:
        if TERMINAL_PUNCTUATION.search(left.text.rstrip()):
            # A block gap close to the inter-line gap can still separate two
            # sentences inside one natural paragraph. A visibly larger gap is
            # a paragraph break even when font, column and indentation match.
            # This distinction matters on designed ESG spreads where Docling
            # emits one block per printed paragraph.
            terminal_gap_limit = max(2.5, min(5.0, normal * 0.48))
            return gap <= terminal_gap_limit
        return True
    if TERMINAL_PUNCTUATION.search(left.text.rstrip()):
        return False
    return (left_edge_close or column_wrap or full_width_tail) and continuation


def _merge_pair(left: ParagraphCandidate, right: ParagraphCandidate) -> ParagraphCandidate:
    return replace(
        left,
        pages=list(dict.fromkeys(left.pages + right.pages)),
        text=_join_text(left.text, right.text),
        top=min(left.top, right.top),
        bottom=max(left.bottom, right.bottom),
        left=min(left.left, right.left),
        right=max(left.right, right.right),
        source_block_count=left.source_block_count + right.source_block_count,
        source_ids=list(dict.fromkeys(left.source_ids + right.source_ids)),
        source_parsers=list(dict.fromkeys(left.source_parsers + right.source_parsers)),
    )


def _merge_horizontal_table_fragments(
    items: list[ParagraphCandidate],
) -> list[ParagraphCandidate]:
    """Join PDF blocks that are separate cells on the same visual table row."""
    output: list[ParagraphCandidate] = []
    used: set[int] = set()
    for index, item in enumerate(items):
        if index in used:
            continue
        if item.kind not in {"table", "figure"}:
            output.append(item)
            continue
        row = [item]
        for other_index in range(index + 1, len(items)):
            if other_index in used:
                continue
            other = items[other_index]
            if other.kind not in {"table", "figure"}:
                continue
            overlap = min(item.bottom, other.bottom) - max(item.top, other.top)
            height = max(1.0, min(item.bottom - item.top, other.bottom - other.top))
            same_visual_row = (
                overlap / height >= 0.75
                and abs(item.top - other.top) <= max(item.line_height, other.line_height, 4.0)
                and not (
                    (item.bottom - item.top) > height * 1.8
                    or (other.bottom - other.top) > height * 1.8
                )
            )
            if same_visual_row:
                row.append(other)
                used.add(other_index)
        if len(row) == 1:
            output.append(item)
            continue
        row.sort(key=lambda value: value.left)
        combined = row[0]
        for fragment in row[1:]:
            combined = _merge_pair(combined, fragment)
        combined.kind = "table"
        combined.column = -1
        output.append(combined)
    return _reading_order(output)


def _merge_page(items: list[ParagraphCandidate]) -> list[ParagraphCandidate]:
    _assign_columns(items)
    _classify_structured(items)
    ordered = _merge_horizontal_table_fragments(_reading_order(items))
    output: list[ParagraphCandidate] = []
    local_heading_by_column: dict[int, str] = {}
    first_body_top = {
        column: min(
            (value.top for value in ordered if value.column == column and not value.is_heading),
            default=float("inf"),
        )
        for column in (0, 1)
    }
    for item in ordered:
        if item.is_heading:
            local_heading_by_column[item.column] = item.text
            if item.column == -1:
                local_heading_by_column[0] = item.text
                local_heading_by_column[1] = item.text
            elif item.top < min(first_body_top.values()):
                # A page title may occupy only the left column geometrically
                # while governing the complete two-column page.
                local_heading_by_column[0] = item.text
                local_heading_by_column[1] = item.text
            # Headings provide structure but are not evidence paragraphs.
            continue
        item.local_heading = local_heading_by_column.get(
            item.column, local_heading_by_column.get(-1, "")
        )
        if output and _can_merge_body(output[-1], item):
            output[-1] = _merge_pair(output[-1], item)
        else:
            output.append(item)
    return output


def _can_continue_across_page(left: ParagraphCandidate, right: ParagraphCandidate) -> bool:
    if left.pages[-1] + 1 != right.start_page:
        return False
    if left.kind != "body" or right.kind != "body":
        return False
    if left.bottom < left.page_height * 0.82 or right.top > right.page_height * 0.24:
        return False
    if LIST_PREFIX.match(right.text):
        return False
    if not _same_style(left, right) or not _compatible_language(left, right):
        return False
    left_relative = left.left / max(left.page_width, 1)
    right_relative = right.left / max(right.page_width, 1)
    same_column = abs(left_relative - right_relative) <= 0.08
    wrapped_to_next_page = left_relative >= 0.42 and right_relative <= 0.28
    continuation_start = bool(re.match(r"^[a-z㐀-鿿]", right.text.lstrip()))
    # A natural paragraph can contain several complete sentences.  When a
    # two-column text box reaches the physical page bottom and resumes at the
    # same horizontal track at the very top of the next page, a full stop and
    # an uppercase next sentence are not reliable paragraph boundaries.  Use
    # this stronger geometry only for same-track continuation; the caller also
    # blocks a new heading and performs one-to-one column matching.
    strong_same_track = (
        same_column
        and left.bottom >= left.page_height * 0.88
        and right.top <= right.page_height * 0.16
        # Exclude short one-line labels that Docling occasionally classifies
        # as body text near the next page top.
        and (
            len(right.text) >= 60
            or right.bottom - right.top >= 30.0
        )
    )
    if TERMINAL_PUNCTUATION.search(left.text.rstrip()):
        return strong_same_track
    return (same_column or wrapped_to_next_page) and (
        continuation_start or strong_same_track
    )


def _split_long_candidate(
    item: ParagraphCandidate, max_characters: int = 2400,
) -> list[ParagraphCandidate]:
    if len(item.text) <= max_characters or item.kind in {"table", "figure"}:
        return [item]
    sentences = re.split(r"(?<=[.!?。！？；;])\s+", item.text)
    if len(sentences) == 1:
        return [item]
    output: list[ParagraphCandidate] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > max_characters:
            output.append(replace(item, text=current, source_block_count=1))
            current = sentence
        else:
            current = _join_text(current, sentence)
    if current:
        output.append(replace(item, text=current, source_block_count=1))
    return output


def extract_paragraph_candidates(
    pdf_path: str | Path,
    external_tables_by_page: dict[int, list[dict[str, Any]]] | None = None,
) -> list[ParagraphCandidate]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF is required for layout-aware paragraph extraction") from exc

    document = fitz.open(str(pdf_path))
    try:
        by_page: list[list[ParagraphCandidate]] = []
        for page_number in range(1, document.page_count + 1):
            page = document[page_number - 1]
            external_tables = (external_tables_by_page or {}).get(page_number, [])
            if external_tables:
                table_candidates, table_boxes = _external_table_candidates(
                    page, page_number, external_tables
                )
            else:
                table_candidates, table_boxes = _table_grid_candidates(page, page_number)
            raw: list[tuple[dict[str, Any], str, float, float, int]] = []
            for block in page.get_text("dict", sort=True).get("blocks", []):
                if int(block.get("type", -1)) != 0:
                    continue
                text, size, line_height, rotated, line_count = _block_metadata(block)
                if not text:
                    continue
                bbox = tuple(float(value) for value in block.get("bbox", (0, 0, 0, 0)))
                if _inside_table(bbox, table_boxes):
                    continue
                if _is_decorative_or_margin_block(
                    bbox=bbox, page_width=float(page.rect.width),
                    page_height=float(page.rect.height), text=text, rotated=rotated,
                ):
                    continue
                raw.append((block, text, size, line_height, line_count))
            body_font = _body_font(raw)
            candidates = []
            for block, text, size, line_height, _ in raw:
                bbox = tuple(float(value) for value in block.get("bbox", (0, 0, 0, 0)))
                candidates.append(ParagraphCandidate(
                    start_page=page_number, pages=[page_number], text=text,
                    is_heading=_looks_like_heading(text, size, body_font),
                    top=bbox[1], bottom=bbox[3], left=bbox[0], right=bbox[2],
                    page_width=float(page.rect.width), page_height=float(page.rect.height),
                    font_size=size, line_height=line_height,
                ))
            candidates.extend(table_candidates)
            by_page.append(_merge_page(candidates))
    finally:
        document.close()

    output: list[ParagraphCandidate] = []
    for current in by_page:
        if output and current and _can_continue_across_page(output[-1], current[0]):
            output[-1] = _merge_pair(output[-1], current.pop(0))
        output.extend(current)
    return [part for item in output for part in _split_long_candidate(item)]


def build_paragraph_artifacts(
    pdf_path: str | Path, sections: list[dict[str, Any]],
    pages_artifact: list[dict[str, Any]] | None = None,
    external_tables_by_page: dict[int, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = (
        extract_paragraph_candidates(pdf_path)
        if external_tables_by_page is None
        else extract_paragraph_candidates(
            pdf_path, external_tables_by_page=external_tables_by_page
        )
    )
    excluded_pages = {
        int(page["physical_page"]) for page in pages_artifact or []
        if page.get("excluded_from_paragraphs")
    }
    candidates = [item for item in candidates if item.start_page not in excluded_pages]
    native_pages = {item.start_page for item in candidates}
    for page in pages_artifact or []:
        number = int(page["physical_page"])
        text = str(page.get("text", "")).strip()
        if (
            number in excluded_pages or number in native_pages
            or not page.get("ocr", {}).get("applied") or not text
        ):
            continue
        candidates.append(ParagraphCandidate(
            start_page=number, pages=[number], text=text, is_heading=False,
            top=0.0, bottom=1.0, left=0.0, right=1.0,
            page_width=1.0, page_height=1.0, kind="ocr",
        ))
    # extract_paragraph_candidates already returns deterministic page/column
    # reading order. Re-sorting by y/x here would interleave two columns.
    page_to_section = {
        page: str(section["title"])
        for section in sections
        for page in range(int(section["start_page"]), int(section["end_page"]) + 1)
    }
    order_by_page: dict[int, int] = {}
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for candidate in candidates:
        order_by_page[candidate.start_page] = order_by_page.get(candidate.start_page, 0) + 1
        paragraph_id = f"P{candidate.start_page}_{order_by_page[candidate.start_page]:02d}"
        records.append({
            "type": "paragraph", "paragraph_id": paragraph_id,
            "section": page_to_section.get(candidate.start_page, "Unclassified"),
            "page": candidate.start_page, "pages": candidate.pages,
            "text": candidate.text,
        })
        diagnostics.append({
            "paragraph_id": paragraph_id, "kind": candidate.kind,
            "column": candidate.column, "local_heading": candidate.local_heading,
            "source_block_count": candidate.source_block_count,
            "font_size": round(candidate.font_size, 3),
            "source": candidate.source,
            "source_id": candidate.source_id,
            "source_ids": candidate.source_ids,
            "source_parsers": candidate.source_parsers,
            "bbox": [round(value, 2) for value in (
                candidate.left, candidate.top, candidate.right, candidate.bottom
            )],
        })
    return records, {
        "parser_version": PARSER_VERSION,
        "merge_rules_version": MERGE_RULES_VERSION,
        "paragraphs": diagnostics,
    }


def build_paragraph_records(
    pdf_path: str | Path, sections: list[dict[str, Any]],
    pages_artifact: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return build_paragraph_artifacts(pdf_path, sections, pages_artifact)[0]


def write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def paragraphs_sha256(records: Iterable[dict[str, Any]]) -> str:
    canonical = "\n".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
