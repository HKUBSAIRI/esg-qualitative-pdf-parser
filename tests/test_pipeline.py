from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfWriter

from natural_pdf_parser.pipeline import build_final_artifacts, extract_picture_tables


def test_complete_pipeline_writes_stable_contract(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    with pdf.open("wb") as handle:
        writer.write(handle)

    text = (
        "The Group measures and reports its environmental and social performance "
        "using a consistent reporting boundary. This natural paragraph contains "
        "multiple complete sentences and remains one final retrieval unit while "
        "preserving its physical page and source bounding box for audit purposes."
    )
    payload = {
        "pages": {"1": {"size": {"width": 600, "height": 800}}},
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]},
        "texts": [{
            "self_ref": "#/texts/0",
            "label": "text",
            "text": text,
            "children": [],
            "prov": [{
                "page_no": 1,
                "bbox": {
                    "l": 50, "t": 100, "r": 550, "b": 150,
                    "coord_origin": "TOPLEFT",
                },
            }],
        }],
        "tables": [{
            "self_ref": "#/tables/0",
            "label": "table",
            "parent": {"$ref": "#/body"},
            "children": [],
            "prov": [{
                "page_no": 1,
                "bbox": {"l": 50, "t": 500, "r": 550, "b": 350, "coord_origin": "BOTTOMLEFT"},
            }],
            "data": {"table_cells": [
                {"start_row_offset_idx": 0, "start_col_offset_idx": 0, "text": "Grade"},
                {"start_row_offset_idx": 0, "start_col_offset_idx": 1, "text": "Measure"},
                {"start_row_offset_idx": 1, "start_col_offset_idx": 0, "text": "A"},
                {"start_row_offset_idx": 1, "start_col_offset_idx": 1, "text": "Preferred supplier"},
            ]},
        }],
        "pictures": [],
    }
    docling_json = tmp_path / "sample.json"
    docling_json.write_text(json.dumps(payload), encoding="utf-8")

    output = tmp_path / "result"
    summary = build_final_artifacts(pdf, docling_json, output)
    rows = [
        json.loads(line)
        for line in (output / "paragraphs.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert summary["page_count"] == 1
    assert summary["natural_paragraph_count"] == 1
    assert summary["table_text_count"] == 1
    paragraph = next(row for row in rows if row["block_type"] == "paragraph")
    table = next(row for row in rows if row["block_type"] == "table_text")
    assert paragraph["text"] == text
    assert paragraph["pages"] == [1]
    assert paragraph["source_block_ids"] == ["#/texts/0"]
    assert paragraph["bbox_coordinate_system"] == "pdf_points_top_left"
    assert table["rows"] == ["Grade | Measure", "A | Preferred supplier"]
    retrieval = [
        json.loads(line)
        for line in (output / "retrieval_chunks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["block_type"] for row in retrieval} == {"paragraph", "table_text"}
    assert (output / "parse_manifest.json").exists()


def test_picture_children_with_aligned_rows_are_recovered_as_text_table() -> None:
    def child(index: int, text: str, x0: float, y0: float, x1: float, y1: float) -> dict:
        return {
            "self_ref": f"#/texts/{index}", "label": "text", "text": text,
            "parent": {"$ref": "#/pictures/0"}, "children": [],
            "prov": [{
                "page_no": 1,
                "bbox": {"l": x0, "t": 800 - y0, "r": x1, "b": 800 - y1,
                         "coord_origin": "BOTTOMLEFT"},
            }],
        }

    values = [
        child(0, "Subsidiary", 100, 100, 200, 112),
        child(1, "Number", 350, 100, 430, 112),
        child(2, "Foshan", 100, 130, 200, 142),
        child(3, "4,043", 350, 130, 400, 142),
        child(4, "Guangzhou", 100, 160, 200, 172),
        child(5, "462", 350, 160, 400, 172),
        child(6, "Chongqing", 100, 190, 200, 202),
        child(7, "702", 350, 190, 400, 202),
    ]
    payload = {
        "texts": values,
        "pictures": [{
            "self_ref": "#/pictures/0",
            "children": [{"$ref": value["self_ref"]} for value in values],
        }],
    }
    tables, consumed = extract_picture_tables(payload, {1: (600.0, 800.0)})
    assert len(tables) == 1
    assert tables[0]["table_rows"] == [
        "Subsidiary | Number", "Foshan | 4,043",
        "Guangzhou | 462", "Chongqing | 702",
    ]
    assert consumed == {value["self_ref"] for value in values}
