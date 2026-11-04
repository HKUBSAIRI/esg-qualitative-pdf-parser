from __future__ import annotations

"""Build database-ready natural paragraphs from already selected parser blocks.

This module deliberately does not parse or reinterpret the PDF.  It consumes
the normalized blocks produced by the existing Docling/BruteForce hybrid
pipeline and changes only the final organisation layer:

* narrative text -> natural paragraphs with stable P{start_page}_{index} IDs;
* tables and figures -> independent element streams;
* headings -> section/paragraph context, never paragraph IDs;
* headers, footers, TOC and suppressed text -> audit only.
"""

from collections import defaultdict
import re
from typing import Any

from .text import normalize_text
from .paragraphs import (
    MERGE_RULES_VERSION,
    PARSER_VERSION,
    ParagraphCandidate,
    _assign_columns,
    _can_continue_across_page,
    _can_merge_body,
    _merge_pair,
    _reading_order,
    _split_long_candidate,
)


# BruteForce page fallback currently emits recovered page text as ``other``.
# Treat it as narrative input as well, otherwise scan-like pages are selected
# successfully but then silently disappear from the paragraph stream.
TEXT_TYPES = {"paragraph", "list", "other"}
HEADING_TYPES = {"title"}
TABLE_TYPES = {"table"}
FIGURE_TYPES = {"image", "chart"}


def _language(text: str) -> str:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk >= max(2, latin // 4):
        return "zh"
    if latin >= 2:
        return "en"
    return "unknown"


def build_auxiliary_text_elements(
    blocks: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Preserve meaningful non-body text without consuming paragraph IDs.

    ESG methodology, scope and emission-factor disclosures are frequently
    printed as table footnotes.  Designed infographics also contain text that
    is correctly suppressed from the narrative paragraph stream.  Both are
    evidence-bearing text, so keep them in independent, auditable streams.
    """
    section_title, section_id = _section_maps(sections)
    note_index: dict[int, int] = defaultdict(int)
    visual_index: dict[int, int] = defaultdict(int)
    notes: list[dict[str, Any]] = []
    visual_texts: list[dict[str, Any]] = []
    for source_order, row in enumerate(blocks, start=1):
        page = int(row["page_number"])
        table_rows = [
            normalize_text(str(value))
            for value in row.get("table_rows") or []
            if normalize_text(str(value))
        ]
        text = "\n".join(table_rows) if table_rows else normalize_text(str(row.get("text") or ""))
        if not text:
            continue
        block_type = str(row.get("block_type") or "other")
        reason = str(row.get("exclusion_reason") or "")
        common = {
            "section": section_title.get(page, "Unclassified"),
            "section_id": section_id.get(page),
            "page": page,
            "pages": [page],
            "content": text,
            "bbox": row.get("bbox"),
            "bbox_coordinate_system": row.get("bbox_coordinate_system"),
            "source_parser": row.get("source_parser"),
            "source_tools": [row.get("source_parser")] if row.get("source_parser") else [],
            "source_block_id": str(row.get("hybrid_block_id") or row.get("source_id") or ""),
            "source_order": source_order,
        }
        if block_type == "footnote":
            note_index[page] += 1
            notes.append({
                "type": "note",
                "note_id": f"N{page}_{note_index[page]:02d}",
                "note_role": "footnote",
                **common,
            })
        elif block_type in TEXT_TYPES and reason == "inside_table_or_image_region":
            visual_index[page] += 1
            visual_texts.append({
                "type": "visual_text",
                "visual_text_id": f"V{page}_{visual_index[page]:02d}",
                "visual_role": "text_inside_structured_visual",
                **common,
            })
    return notes, visual_texts


def _rect(
    row: dict[str, Any], page_width: float, page_height: float
) -> tuple[float, float, float, float] | None:
    bbox = row.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        x0, y0, x1, y1 = (float(value) for value in bbox)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    if isinstance(bbox, dict) and all(key in bbox for key in ("l", "t", "r", "b")):
        left, right = float(bbox["l"]), float(bbox["r"])
        top, bottom = float(bbox["t"]), float(bbox["b"])
        if str(bbox.get("coord_origin", "")).upper() == "BOTTOMLEFT":
            top, bottom = page_height - top, page_height - bottom
        return min(left, right), min(top, bottom), max(left, right), max(top, bottom)
    return None


def _section_maps(sections: list[dict[str, Any]]) -> tuple[dict[int, str], dict[int, str | None]]:
    title_by_page: dict[int, str] = {}
    id_by_page: dict[int, str | None] = {}
    for section in sections:
        start = int(section.get("start_page", 1))
        end = int(section.get("end_page", start))
        for page in range(start, end + 1):
            title_by_page[page] = str(section.get("title") or "Unclassified")
            value = section.get("section_id")
            id_by_page[page] = None if value is None else str(value)
    return title_by_page, id_by_page


def _candidate(
    row: dict[str, Any], page_sizes: dict[int, tuple[float, float]]
) -> ParagraphCandidate | None:
    page = int(row["page_number"])
    width, height = page_sizes[page]
    bbox = _rect(row, width, height)
    text = normalize_text(str(row.get("text") or "")).replace("\n", " ")
    if not bbox or not text:
        return None
    x0, y0, x1, y1 = bbox
    block_type = str(row.get("block_type") or "paragraph")
    source_id = str(row.get("hybrid_block_id") or row.get("source_id") or "")
    source_parser = str(row.get("source_parser") or "unknown")
    font_size = float(row.get("font_size") or 9.0)
    block_height = max(1.0, y1 - y0)
    estimated_lines = max(1, round(block_height / max(font_size * 1.25, 1.0)))
    return ParagraphCandidate(
        start_page=page,
        pages=[page],
        text=text,
        is_heading=block_type in HEADING_TYPES,
        top=y0,
        bottom=y1,
        left=x0,
        right=x1,
        page_width=width,
        page_height=height,
        font_size=font_size,
        line_height=block_height / estimated_lines,
        kind=(
            "heading" if block_type in HEADING_TYPES
            else "list" if block_type == "list"
            else "body"
        ),
        source=source_parser,
        source_id=source_id,
        source_ids=[source_id] if source_id else [],
        source_parsers=[source_parser],
        language=_language(text),
    )


def _boundary_score(left: ParagraphCandidate, right: ParagraphCandidate) -> float:
    """Score a possible paragraph continuation across adjacent pages."""
    if not _can_continue_across_page(left, right):
        return float("-inf")
    if right.local_heading and right.local_heading != left.local_heading:
        # A new heading at the top of the next page is a hard paragraph and
        # section boundary, even when both boxes touch the page edges.
        return float("-inf")
    score = 0.0
    score += min(2.0, max(0.0, (left.bottom / left.page_height - 0.80) * 10))
    score += min(2.0, max(0.0, (0.25 - right.top / right.page_height) * 8))
    left_x = left.left / max(left.page_width, 1.0)
    right_x = right.left / max(right.page_width, 1.0)
    if abs(left_x - right_x) <= 0.08:
        score += 3.0
    elif left_x >= 0.42 and right_x <= 0.28:
        score += 1.5
    if left.language == right.language and left.language != "unknown":
        score += 2.0
    if left.local_heading and left.local_heading == right.local_heading:
        score += 1.0
    return score


def _merge_page_boundary(
    previous: list[ParagraphCandidate], current: list[ParagraphCandidate]
) -> list[ParagraphCandidate]:
    """Match all plausible column tails/heads, including parallel bilingual flows."""
    if not previous or not current:
        return current
    prior_page = previous[-1].pages[-1]
    tails = [
        (index, item) for index, item in enumerate(previous)
        if item.pages[-1] == prior_page and item.kind == "body"
        and item.bottom >= item.page_height * 0.80
    ]
    heads = [
        (index, item) for index, item in enumerate(current)
        if item.kind == "body" and item.top <= item.page_height * 0.25
    ]
    # Keep only the terminal paragraph and initial paragraph of each track.
    tail_by_column: dict[int, tuple[int, ParagraphCandidate]] = {}
    for value in tails:
        if value[1].column not in tail_by_column or value[1].bottom > tail_by_column[value[1].column][1].bottom:
            tail_by_column[value[1].column] = value
    head_by_column: dict[int, tuple[int, ParagraphCandidate]] = {}
    for value in heads:
        if value[1].column not in head_by_column or value[1].top < head_by_column[value[1].column][1].top:
            head_by_column[value[1].column] = value
    tails, heads = list(tail_by_column.values()), list(head_by_column.values())
    edges = [
        (score, left_index, right_index)
        for left_index, left in tails
        for right_index, right in heads
        if (score := _boundary_score(left, right)) >= 4.0
    ]
    # Exhaustive matching is tiny (normally <= 2 x 2) and avoids greedily
    # choosing right->left when left->left plus right->right is stronger.
    best: tuple[float, list[tuple[int, int]]] = (0.0, [])
    def search(pos: int, used_left: set[int], used_right: set[int], total: float, pairs: list[tuple[int, int]]) -> None:
        nonlocal best
        if pos == len(edges):
            if total > best[0]:
                best = (total, list(pairs))
            return
        search(pos + 1, used_left, used_right, total, pairs)
        score, left_index, right_index = edges[pos]
        if left_index not in used_left and right_index not in used_right:
            search(pos + 1, used_left | {left_index}, used_right | {right_index}, total + score, pairs + [(left_index, right_index)])
    search(0, set(), set(), 0.0, [])
    consumed: set[int] = set()
    for left_index, right_index in best[1]:
        previous[left_index] = _merge_pair(previous[left_index], current[right_index])
        consumed.add(right_index)
    return [item for index, item in enumerate(current) if index not in consumed]


def _merge_page_text(items: list[ParagraphCandidate]) -> list[ParagraphCandidate]:
    """Reuse the proven column/order/continuation rules without type guessing."""
    _assign_columns(items)
    # On a single-column page a long line can cross the generic 72% width
    # threshold and be labelled full-width (-1), while the previous line is
    # labelled column 0.  They are still consecutive lines of one paragraph.
    # Preserve -1 only when a genuine right column exists.
    body_columns = {item.column for item in items if not item.is_heading}
    if 1 not in body_columns:
        for item in items:
            if not item.is_heading and item.column == -1:
                item.column = 0
    ordered = _reading_order(items)
    output: list[ParagraphCandidate] = []
    headings: dict[int, str] = {}
    first_body_top = {
        column: min(
            (value.top for value in ordered if value.column == column and not value.is_heading),
            default=float("inf"),
        )
        for column in (0, 1)
    }
    for item in ordered:
        if item.is_heading:
            headings[item.column] = item.text
            if item.column == -1 or item.top < min(first_body_top.values()):
                headings[0] = item.text
                headings[1] = item.text
            continue
        item.local_heading = headings.get(item.column, headings.get(-1, ""))
        if output and _can_merge_body(output[-1], item):
            output[-1] = _merge_pair(output[-1], item)
        else:
            output.append(item)
    return output


def _source_bboxes(
    candidate: ParagraphCandidate,
    row_by_id: dict[str, dict[str, Any]],
    page_sizes: dict[int, tuple[float, float]],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for source_id in candidate.source_ids:
        row = row_by_id.get(source_id)
        if not row:
            continue
        page = int(row["page_number"])
        rect = _rect(row, *page_sizes[page])
        values.append({
            "page": page,
            "bbox": [round(value, 2) for value in rect] if rect else None,
            "source_block_id": source_id,
            "source_parser": row.get("source_parser"),
        })
    return values


def assemble_hybrid_artifacts(
    blocks: list[dict[str, Any]],
    page_sizes: dict[int, tuple[float, float]],
    sections: list[dict[str, Any]],
    *,
    excluded_pages: set[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    excluded = excluded_pages or set()
    section_title, section_id = _section_maps(sections)
    by_page: dict[int, list[ParagraphCandidate]] = defaultdict(list)
    row_by_id: dict[str, dict[str, Any]] = {}
    tables: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    table_index: dict[int, int] = defaultdict(int)
    figure_index: dict[int, int] = defaultdict(int)

    for row in blocks:
        page = int(row["page_number"])
        source_id = str(row.get("hybrid_block_id") or row.get("source_id") or "")
        if source_id:
            row_by_id[source_id] = row
        block_type = str(row.get("block_type") or "other")
        label = str(row.get("source_label") or "")
        table_rows = [
            normalize_text(str(value))
            for value in row.get("table_rows") or []
            if normalize_text(str(value))
        ]
        text = "\n".join(table_rows) if table_rows else normalize_text(str(row.get("text") or ""))
        common = {
            "section": section_title.get(page, "Unclassified"),
            "section_id": section_id.get(page),
            "page": page,
            "pages": [page],
            "content": text,
            "bbox": row.get("bbox"),
            "bbox_coordinate_system": row.get("bbox_coordinate_system"),
            "source_parser": row.get("source_parser"),
            "source_tools": [row.get("source_parser")] if row.get("source_parser") else [],
            "source_block_id": source_id,
        }
        if block_type in TABLE_TYPES:
            table_index[page] += 1
            tables.append({
                "type": "table", "table_id": f"T{page}_{table_index[page]:02d}",
                "rows": table_rows, **common
            })
            continue
        if block_type in FIGURE_TYPES or label == "caption":
            figure_index[page] += 1
            figures.append({
                "type": "figure", "figure_id": f"F{page}_{figure_index[page]:02d}", **common
            })
            continue
        # Only body narrative participates in paragraph numbering. Titles are
        # retained as context; TOC, headers/footers and suppressed nested text
        # remain available in blocks.jsonl for audit but never consume P IDs.
        if page in excluded or row.get("is_toc") or row.get("is_auxiliary"):
            continue
        if block_type not in TEXT_TYPES | HEADING_TYPES:
            continue
        if block_type in TEXT_TYPES and not row.get("include_in_text_output", False):
            continue
        candidate = _candidate(row, page_sizes)
        if candidate:
            by_page[page].append(candidate)

    ordered: list[ParagraphCandidate] = []
    for page in sorted(page_sizes):
        current = _merge_page_text(by_page.get(page, []))
        current = _merge_page_boundary(ordered, current)
        ordered.extend(current)

    page_index: dict[int, int] = defaultdict(int)
    paragraphs: list[dict[str, Any]] = []
    paragraph_diagnostics: list[dict[str, Any]] = []
    for document_order, candidate in enumerate(ordered, start=1):
        page_index[candidate.start_page] += 1
        paragraph_id = f"P{candidate.start_page}_{page_index[candidate.start_page]:02d}"
        bboxes = _source_bboxes(candidate, row_by_id, page_sizes)
        record = {
            "type": "paragraph",
            "paragraph_id": paragraph_id,
            "section": section_title.get(candidate.start_page, "Unclassified"),
            "section_id": section_id.get(candidate.start_page),
            "page": candidate.start_page,
            "pages": candidate.pages,
            "text": candidate.text,
            "block_type": "list" if candidate.kind == "list" else "paragraph",
            "paragraph_role": "body",
            "body_only": True,
            "local_heading": candidate.local_heading or None,
            "source_block_ids": candidate.source_ids,
            "source_block_id": "|".join(candidate.source_ids),
            "source_parsers": candidate.source_parsers,
            "source_parser": "|".join(candidate.source_parsers),
            "source_tools": candidate.source_parsers,
            "bboxes": bboxes,
            "bbox": next((value["bbox"] for value in bboxes if value["page"] == candidate.start_page), None),
            "bbox_coordinate_system": "pdf_points_top_left",
            "cross_page": len(candidate.pages) > 1,
            "language": candidate.language,
            "document_order": document_order,
        }
        paragraphs.append(record)
        paragraph_diagnostics.append({
            "paragraph_id": paragraph_id,
            "pages": candidate.pages,
            "column": candidate.column,
            "source_block_count": candidate.source_block_count,
            "source_block_ids": candidate.source_ids,
            "source_parsers": candidate.source_parsers,
            "cross_page": len(candidate.pages) > 1,
            "text_characters": len(candidate.text),
        })

    diagnostics = {
        "parser_version": PARSER_VERSION,
        "merge_rules_version": MERGE_RULES_VERSION,
        "paragraph_unit": "natural_paragraph",
        "body_only": True,
        "paragraph_count": len(paragraphs),
        "cross_page_paragraph_count": sum(len(row["pages"]) > 1 for row in paragraphs),
        "table_count": len(tables),
        "figure_count": len(figures),
        "paragraphs": paragraph_diagnostics,
    }
    return paragraphs, diagnostics, tables, figures
