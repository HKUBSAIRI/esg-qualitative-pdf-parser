from __future__ import annotations

"""Build final, auditable natural-paragraph JSONL from a complete PDF.

Docling supplies semantic regions and reading order.  The project paragraph
assembler joins sentence-sized regions into natural paragraphs and performs
multi-track cross-page matching.  Raw Docling objects are never exposed as the
final chunk unit.
"""

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader

from .common import read_json, sha256_file, write_json
from .docling_normalizer import normalize_docling
from .hybrid_paragraphs import assemble_hybrid_artifacts


SCHEMA_VERSION = "natural-paragraph-v4"
TOC_TOKENS = ("table of contents", "contents", "目录", "目錄")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def top_left_bbox(
    bbox: dict[str, Any] | list[float] | None, page_height: float
) -> list[float] | None:
    if isinstance(bbox, list) and len(bbox) == 4:
        x0, y0, x1, y1 = map(float, bbox)
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
    if not isinstance(bbox, dict) or not all(key in bbox for key in ("l", "t", "r", "b")):
        return None
    left, right = float(bbox["l"]), float(bbox["r"])
    top, bottom = float(bbox["t"]), float(bbox["b"])
    if str(bbox.get("coord_origin", "")).upper() == "BOTTOMLEFT":
        top, bottom = page_height - top, page_height - bottom
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def docling_page_sizes(payload: dict[str, Any]) -> dict[int, tuple[float, float]]:
    return {
        int(page): (float(value["size"]["width"]), float(value["size"]["height"]))
        for page, value in payload.get("pages", {}).items()
    }


def extract_picture_tables(
    payload: dict[str, Any], page_sizes: dict[int, tuple[float, float]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Recover grid-like text tables that Docling wrapped as a picture."""
    texts = {
        str(item.get("self_ref")): item
        for item in payload.get("texts", []) if item.get("self_ref")
    }
    recovered: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for picture in payload.get("pictures", []):
        picture_ref = str(picture.get("self_ref") or "")
        items: list[dict[str, Any]] = []
        for child in picture.get("children") or []:
            ref = str(child.get("$ref") or "")
            item = texts.get(ref)
            if not item:
                continue
            text = re.sub(r"\s+", " ", str(item.get("text") or item.get("orig") or "")).strip()
            prov = (item.get("prov") or [{}])[0]
            page = int(prov.get("page_no", 0) or 0)
            if not text or page not in page_sizes:
                continue
            bbox = top_left_bbox(prov.get("bbox"), page_sizes[page][1])
            if not bbox or bbox[1] >= page_sizes[page][1] * 0.94:
                continue
            items.append({"ref": ref, "text": text, "page": page, "bbox": bbox})
        if len(items) < 6 or len({item["page"] for item in items}) != 1:
            continue
        page = int(items[0]["page"])
        heights = sorted(max(1.0, item["bbox"][3] - item["bbox"][1]) for item in items)
        typical_height = heights[len(heights) // 2]
        tolerance = max(3.0, min(8.0, typical_height * 0.75))
        row_groups: list[list[dict[str, Any]]] = []
        for item in sorted(items, key=lambda value: ((value["bbox"][1] + value["bbox"][3]) / 2, value["bbox"][0])):
            center = (item["bbox"][1] + item["bbox"][3]) / 2
            if row_groups:
                prior_center = sum((value["bbox"][1] + value["bbox"][3]) / 2 for value in row_groups[-1]) / len(row_groups[-1])
                if abs(center - prior_center) <= tolerance:
                    row_groups[-1].append(item)
                    continue
            row_groups.append([item])
        multi_rows = [
            group for group in row_groups if len(group) >= 2
            and max(value["bbox"][0] for value in group) - min(value["bbox"][0] for value in group)
            >= page_sizes[page][0] * 0.12
        ]
        if len(multi_rows) < 3:
            continue
        clusters: list[list[list[dict[str, Any]]]] = []
        for group in multi_rows:
            center = sum((value["bbox"][1] + value["bbox"][3]) / 2 for value in group) / len(group)
            if not clusters:
                clusters.append([group])
                continue
            previous = clusters[-1][-1]
            previous_center = sum((value["bbox"][1] + value["bbox"][3]) / 2 for value in previous) / len(previous)
            if center - previous_center <= max(36.0, typical_height * 5):
                clusters[-1].append(group)
            else:
                clusters.append([group])
        grid = max(clusters, key=len)
        if len(grid) < 3:
            continue
        numeric_cells = sum(bool(re.search(r"\d", value["text"])) for group in grid for value in group)
        if numeric_cells < 2:
            continue
        first_center = min(sum((value["bbox"][1] + value["bbox"][3]) / 2 for value in group) / len(group) for group in grid)
        caption_candidates = [
            group[0] for group in row_groups if len(group) == 1
            and 0 < first_center - (group[0]["bbox"][1] + group[0]["bbox"][3]) / 2 <= 40
        ]
        caption = max(caption_candidates, key=lambda value: (value["bbox"][1] + value["bbox"][3]) / 2, default=None)
        selected = [value for group in grid for value in group]
        if caption:
            selected.insert(0, caption)
        rows = [caption["text"]] if caption else []
        rows.extend(" | ".join(value["text"] for value in sorted(group, key=lambda value: value["bbox"][0])) for group in grid)
        bbox = [
            min(value["bbox"][0] for value in selected), min(value["bbox"][1] for value in selected),
            max(value["bbox"][2] for value in selected), max(value["bbox"][3] for value in selected),
        ]
        source_id = f"{picture_ref}::text_table"
        recovered.append({
            "document_id": "", "page_number": page, "paragraph_id": "",
            "block_type": "table", "text": "\n".join(rows), "table_rows": rows,
            "bbox": bbox, "bbox_coordinate_system": "pdf_points_top_left",
            "source_parser": "docling", "source_id": source_id,
            "source_label": "visual_table", "parent_ref": picture_ref,
            "inside_picture": True, "is_auxiliary": False,
            "include_in_text_score": False,
        })
        consumed.update(value["ref"] for value in selected)
    return recovered, consumed


def derive_sections(
    payload: dict[str, Any], page_sizes: dict[int, tuple[float, float]]
) -> list[dict[str, Any]]:
    """Create page-range section context from headings near the page top."""
    headings: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
    for item in payload.get("texts", []):
        if item.get("label") != "section_header":
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            continue
        for prov in item.get("prov") or []:
            page = int(prov.get("page_no", 0) or 0)
            if page not in page_sizes:
                continue
            width, height = page_sizes[page]
            bbox = top_left_bbox(prov.get("bbox"), height)
            if bbox and bbox[1] <= height * 0.20:
                headings[page].append((bbox[1], bbox[0] / max(width, 1), text))

    page_title: dict[int, str] = {}
    last_title = "Front matter"
    for page in sorted(page_sizes):
        candidates = sorted(headings.get(page, []))
        selected: list[str] = []
        for _, _, text in candidates:
            if text not in selected:
                selected.append(text)
            if len(selected) == 2:
                break
        if selected:
            last_title = " / ".join(selected)
        page_title[page] = last_title

    sections: list[dict[str, Any]] = []
    start = 1
    current = page_title.get(1, "Front matter")
    for page in range(2, max(page_sizes, default=1) + 1):
        title = page_title.get(page, current)
        if title != current:
            sections.append({
                "section_id": f"SEC{len(sections) + 1:03d}",
                "title": current,
                "start_page": start,
                "end_page": page - 1,
            })
            start, current = page, title
    sections.append({
        "section_id": f"SEC{len(sections) + 1:03d}",
        "title": current,
        "start_page": start,
        "end_page": max(page_sizes, default=1),
    })
    return sections


def section_maps(sections: list[dict[str, Any]]) -> tuple[dict[int, str], dict[int, str]]:
    titles: dict[int, str] = {}
    identifiers: dict[int, str] = {}
    for section in sections:
        for page in range(int(section["start_page"]), int(section["end_page"]) + 1):
            titles[page] = str(section["title"])
            identifiers[page] = str(section["section_id"])
    return titles, identifiers


def run_docling(
    pdf: Path,
    output: Path,
    executable: str,
    timeout: int,
    artifacts_path: Path | None = None,
) -> Path:
    raw = output / "raw" / "docling"
    raw.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        str(pdf),
        "--from", "pdf",
        "--to", "json",
        "--image-export-mode", "placeholder",
        "--output", str(raw),
        "--abort-on-error",
    ]
    if artifacts_path is not None:
        command.extend(["--artifacts-path", str(artifacts_path)])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    (raw / f"{pdf.stem}.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)
    expected = raw / f"{pdf.stem}.json"
    if not expected.exists():
        candidates = sorted(raw.glob("*.json"))
        if len(candidates) != 1:
            raise RuntimeError(
                f"Expected {expected.name} or one Docling JSON under {raw}, "
                f"found {len(candidates)}"
            )
        return candidates[0]
    return expected


def is_toc_page(page: int, rows: list[dict[str, Any]]) -> bool:
    page_rows = [row for row in rows if int(row["page_number"]) == page and row.get("text")]
    values = [str(row["text"]).strip() for row in page_rows]
    early = " ".join(values[:8])[:700].casefold()
    explicit = any(token in early for token in TOC_TOKENS)
    list_items = sum(row.get("block_type") == "list" for row in page_rows)
    page_references = sum(bool(re.search(r"(?:\s|^)\d{1,3}\s*$", value)) for value in values)
    mostly_short = len(values) >= 8 and sum(len(value) <= 90 for value in values) / len(values) >= 0.70
    return explicit and (list_items >= 3 or page_references >= 3 or mostly_short)


def build_final_artifacts(pdf: Path, docling_json: Path, output: Path) -> dict[str, Any]:
    payload = read_json(docling_json)
    page_sizes = docling_page_sizes(payload)
    pdf_pages = len(PdfReader(str(pdf)).pages)
    if sorted(page_sizes) != list(range(1, pdf_pages + 1)):
        raise ValueError(f"Docling page coverage mismatch: PDF={pdf_pages}, JSON={sorted(page_sizes)}")

    sections = derive_sections(payload, page_sizes)
    title_by_page, section_id_by_page = section_maps(sections)
    rows = normalize_docling(pdf.stem, docling_json, list(range(1, pdf_pages + 1)), sections)
    recovered_tables, picture_table_members = extract_picture_tables(payload, page_sizes)
    for row in recovered_tables:
        row["document_id"] = pdf.stem
    rows.extend(recovered_tables)
    rows_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, source in enumerate(rows, start=1):
        row = dict(source)
        page = int(row["page_number"])
        row["hybrid_block_id"] = str(row.get("source_id") or f"docling-p{page}-{index:05d}")
        row["source_order"] = index
        row["is_toc"] = False
        row["include_in_text_output"] = bool(
            row.get("text")
            and not row.get("is_auxiliary")
            and row.get("block_type") not in {"table", "image", "chart", "header", "footer", "page_number", "footnote"}
        )
        rect = top_left_bbox(row.get("bbox"), page_sizes[page][1])
        text = str(row.get("text") or "").strip()
        source_id = str(row.get("source_id") or "")
        if source_id in picture_table_members:
            row["include_in_text_output"] = False
            row["exclusion_reason"] = "inside_visual_table"
        elif row.get("inside_picture") and row.get("block_type") == "paragraph" and not (
            len(text) >= 80 or (len(text) >= 40 and re.search(r"[.!?。！？；;：:]$", text))
        ):
            row["include_in_text_output"] = False
            row["exclusion_reason"] = "text_inside_picture"
        elif (
            row.get("block_type") == "paragraph"
            and len(text) <= 20
            and not re.search(r"[.!?。！？；;：:]$", text)
            and rect
            and rect[3] - rect[1] <= page_sizes[page][1] * 0.05
        ):
            row["include_in_text_output"] = False
            row["exclusion_reason"] = "short_visual_label_not_natural_paragraph"
        rows_by_page[page].append(row)
        rows[index - 1] = row

    toc_pages = {page for page in page_sizes if is_toc_page(page, rows)}
    for row in rows:
        if int(row["page_number"]) in toc_pages:
            row["is_toc"] = True
            row["include_in_text_output"] = False
            row["exclusion_reason"] = "toc_not_narrative_body"

    # Cover pages often contain product-label OCR fragments.  Exclude them
    # from natural paragraphs while retaining every raw block in blocks.jsonl.
    first_body = [
        row for row in rows_by_page.get(1, [])
        if row.get("block_type") == "paragraph" and row.get("text")
    ]
    cover_pages = {1} if first_body and max(map(lambda row: len(str(row["text"])), first_body), default=0) < 200 else set()

    paragraphs, diagnostics, tables, figures = assemble_hybrid_artifacts(
        rows, page_sizes, sections, excluded_pages=cover_pages
    )

    source_order = {str(row["hybrid_block_id"]): int(row["source_order"]) for row in rows}
    semantic: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        block_type = str(paragraph.get("block_type") or "paragraph")
        paragraph["section"] = title_by_page.get(int(paragraph["page"]), paragraph.get("section"))
        paragraph["section_id"] = section_id_by_page.get(int(paragraph["page"]), paragraph.get("section_id"))
        paragraph["_source_order"] = min(
            (source_order.get(value, 10**9) for value in paragraph.get("source_block_ids", [])),
            default=10**9,
        )
        paragraph["merge_method"] = "docling_layout_tracks_v4"
        semantic.append(paragraph)

    for row in rows:
        if row.get("block_type") != "title" or row.get("is_toc"):
            continue
        page = int(row["page_number"])
        bbox = top_left_bbox(row.get("bbox"), page_sizes[page][1])
        semantic.append({
            "type": "title",
            "paragraph_id": "",
            "section": title_by_page.get(page, "Unclassified"),
            "section_id": section_id_by_page.get(page),
            "page": page,
            "pages": [page],
            "text": str(row.get("text") or ""),
            "block_type": "title",
            "source_parser": "docling",
            "source_block_id": str(row["hybrid_block_id"]),
            "source_block_ids": [str(row["hybrid_block_id"])],
            "bbox": bbox,
            "bboxes": [{"page": page, "bbox": bbox, "source_block_id": str(row["hybrid_block_id"]), "source_parser": "docling"}],
            "bbox_coordinate_system": "pdf_points_top_left",
            "cross_page": False,
            "language": "mixed",
            "merge_method": "docling_semantic_title",
            "_source_order": int(row["source_order"]),
        })

    for table in tables:
        page = int(table["page"])
        source_id = str(table.get("source_block_id") or "")
        bbox = top_left_bbox(table.get("bbox"), page_sizes[page][1])
        table["bbox"] = bbox
        table["bbox_coordinate_system"] = "pdf_points_top_left"
        text = str(table.get("content") or "").strip()
        if not text:
            continue
        semantic.append({
            "type": "table_text", "paragraph_id": "",
            "section": title_by_page.get(page, "Unclassified"),
            "section_id": section_id_by_page.get(page), "page": page, "pages": [page],
            "text": text, "rows": table.get("rows") or [], "block_type": "table_text",
            "source_parser": str(table.get("source_parser") or "docling"),
            "source_block_id": source_id, "source_block_ids": [source_id] if source_id else [],
            "bbox": bbox,
            "bboxes": [{"page": page, "bbox": bbox, "source_block_id": source_id,
                         "source_parser": str(table.get("source_parser") or "docling")}],
            "bbox_coordinate_system": "pdf_points_top_left", "cross_page": False,
            "language": "mixed", "merge_method": "docling_table_to_readable_text",
            "_source_order": source_order.get(source_id, 10**9),
        })

    semantic.sort(key=lambda row: (int(row["page"]), int(row.get("_source_order", 10**9))))
    counters: dict[tuple[int, str], int] = defaultdict(int)
    prefixes = {"paragraph": "P", "title": "H", "list": "L", "table_text": "T"}
    for order, row in enumerate(semantic, start=1):
        kind = str(row.get("block_type") or "paragraph")
        key = (int(row["page"]), kind)
        counters[key] += 1
        row["paragraph_id"] = f"{prefixes.get(kind, 'P')}{row['page']}_{counters[key]:03d}"
        row["document_order"] = order
        row.pop("_source_order", None)

    retrieval_chunks = [
        {
            "chunk_id": row["paragraph_id"],
            "parent_paragraph_id": row["paragraph_id"],
            "document_id": pdf.stem,
            "pages": row["pages"],
            "text": row["text"],
            "block_type": row["block_type"],
            "section": row.get("section"),
            "section_id": row.get("section_id"),
            "bboxes": row.get("bboxes") or [],
        }
        for row in semantic if row["block_type"] in {"paragraph", "list", "table_text"}
    ]
    visual_texts = [
        {
            "visual_text_id": f"V{int(row['page_number'])}_{index:03d}",
            "page": int(row["page_number"]),
            "pages": [int(row["page_number"])],
            "text": str(row.get("text") or ""),
            "source_parser": "docling",
            "source_block_id": str(row["hybrid_block_id"]),
            "bbox": top_left_bbox(row.get("bbox"), page_sizes[int(row["page_number"])][1]),
            "bbox_coordinate_system": "pdf_points_top_left",
            "exclusion_reason": row.get("exclusion_reason"),
        }
        for index, row in enumerate(rows, start=1)
        if row.get("exclusion_reason") in {
            "short_visual_label_not_natural_paragraph", "text_inside_picture"
        }
    ]

    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "blocks.jsonl", rows)
    write_jsonl(output / "paragraphs.jsonl", semantic)
    write_jsonl(output / "retrieval_chunks.jsonl", retrieval_chunks)
    write_jsonl(output / "visual_texts.jsonl", visual_texts)
    write_jsonl(output / "tables.jsonl", tables)
    write_jsonl(output / "figures.jsonl", figures)
    write_json(output / "sections.json", {"sections": sections})
    write_json(output / "paragraph_diagnostics.json", diagnostics)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "document_id": pdf.stem,
        "page_count": pdf_pages,
        "semantic_block_count": len(semantic),
        "natural_paragraph_count": sum(row["block_type"] == "paragraph" for row in semantic),
        "title_count": sum(row["block_type"] == "title" for row in semantic),
        "list_count": sum(row["block_type"] == "list" for row in semantic),
        "table_text_count": sum(row["block_type"] == "table_text" for row in semantic),
        "cross_page_paragraph_count": sum(row.get("cross_page", False) for row in semantic),
        "visual_text_count": len(visual_texts),
        "toc_pages_excluded": sorted(toc_pages),
        "cover_pages_excluded": sorted(cover_pages),
    }
    write_json(output / "summary.json", summary)
    write_json(output / "parse_manifest.json", {
        **summary,
        "source_pdf": str(pdf),
        "source_pdf_sha256": sha256_file(pdf),
        "docling_json": str(docling_json),
        "paragraph_unit": "complete_natural_paragraph",
        "raw_docling_block_is_final_chunk": False,
        "forced_paragraph_character_split": False,
        "bbox_coordinate_system": "pdf_points_top_left",
        "artifacts": {
            "paragraphs": "paragraphs.jsonl",
            "retrieval_chunks": "retrieval_chunks.jsonl",
            "raw_blocks": "blocks.jsonl",
            "tables": "tables.jsonl",
            "figures": "figures.jsonl",
            "visual_texts": "visual_texts.jsonl",
            "diagnostics": "paragraph_diagnostics.json",
        },
    })
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Docling structure to complete natural paragraphs")
    parser.add_argument("pdf")
    parser.add_argument("--output", required=True)
    parser.add_argument("--docling-json", help="Reuse a complete Docling JSON instead of running Docling")
    parser.add_argument("--docling", default="docling", help="Docling CLI executable")
    parser.add_argument("--artifacts-path", help="Directory containing downloaded Docling model artifacts")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    pdf = Path(args.pdf).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    docling_json = (
        Path(args.docling_json).expanduser().resolve()
        if args.docling_json else run_docling(
            pdf,
            output,
            args.docling,
            args.timeout,
            Path(args.artifacts_path).expanduser().resolve() if args.artifacts_path else None,
        )
    )
    summary = build_final_artifacts(pdf, docling_json, output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
