from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import normalize_text, read_json


def _section_for_page(sections: list[dict[str, Any]], page: int) -> dict[str, Any]:
    for section in sections:
        if int(section["start_page"]) <= page <= int(section["end_page"]):
            return section
    return {"section_id": None, "title": None}


def _docling_order(payload: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in seen:
                return
            seen.add(ref)
            parts = ref[2:].split("/")
            value: Any = payload
            try:
                for part in parts:
                    value = value[int(part)] if isinstance(value, list) else value[part]
            except (KeyError, IndexError, ValueError, TypeError):
                return
            if parts[0] in {"texts", "tables", "pictures", "key_value_items"}:
                ordered.append(value)
            for child in value.get("children", []) if isinstance(value, dict) else []:
                visit(child)
            return
        for child in node.get("children", []):
            visit(child)

    visit(payload.get("body", {}))
    if not ordered:
        ordered.extend(payload.get("texts", []))
        ordered.extend(payload.get("tables", []))
        ordered.extend(payload.get("pictures", []))
    return ordered


def normalize_docling(
    document_id: str,
    json_path: str | Path,
    page_map: list[int],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Docling JSON into the stable intermediate block contract."""
    payload = read_json(json_path)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(_docling_order(payload), start=1):
        prov = (item.get("prov") or [{}])[0]
        subset_page = int(prov.get("page_no", 1))
        if not 1 <= subset_page <= len(page_map):
            continue
        page = page_map[subset_page - 1]
        section = _section_for_page(sections, page)
        label = str(item.get("label") or "text")
        block_type = {
            "text": "paragraph",
            "section_header": "title",
            "page_header": "header",
            "page_footer": "footer",
            "caption": "paragraph",
            "picture": "image",
            "formula": "equation",
            "list_item": "list",
        }.get(label, label)
        allowed = {
            "title", "paragraph", "list", "table", "image", "chart",
            "equation", "header", "footer", "page_number", "footnote",
        }
        if block_type not in allowed:
            block_type = "other"
        text = item.get("text") or item.get("orig") or ""
        table_rows: list[str] = []
        if block_type == "table" and not text:
            cells = (item.get("data") or {}).get("table_cells") or []
            rows: dict[int, list[tuple[int, str]]] = defaultdict(list)
            for cell in cells:
                value = cell.get("text") or cell.get("orig") or ""
                if str(value).strip():
                    rows[int(cell.get("start_row_offset_idx", 0))].append((
                        int(cell.get("start_col_offset_idx", 0)), str(value)
                    ))
            table_rows = [
                " | ".join(value for _, value in sorted(row))
                for _, row in sorted(rows.items())
            ]
            text = "\n".join(table_rows)
        parent_ref = str((item.get("parent") or {}).get("$ref") or "")
        records.append({
            "document_id": document_id,
            "page_number": page,
            "section_id": section.get("section_id"),
            "section_title": section.get("title"),
            "paragraph_id": f"{document_id}__docling__p{page}_{index:03d}",
            "block_type": block_type,
            "text": normalize_text(str(text)),
            "bbox": prov.get("bbox"),
            "bbox_coordinate_system": "docling_provenance",
            "source_parser": "docling",
            "source_id": item.get("self_ref"),
            "source_label": label,
            "parent_ref": parent_ref,
            "inside_picture": parent_ref.startswith("#/pictures/"),
            "table_rows": table_rows,
            "is_auxiliary": block_type in {
                "header", "footer", "page_number", "footnote"
            },
            "include_in_text_score": block_type not in {
                "table", "image", "chart", "header", "footer",
                "page_number", "footnote",
            },
        })
    return records
