from __future__ import annotations

"""Render final semantic chunks over their source PDF pages.

Unlike the raw-parser audit overlays, this renderer consumes paragraphs.jsonl.
Every box carrying the same chunk id therefore represents one final chunk.  A
multi-region chunk gets one heavy union outline plus its lighter source-region
outlines, making paragraph merging visible without losing provenance.
"""

import argparse
import colorsys
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw, ImageFont


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def chunk_color(chunk_id: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(chunk_id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535
    saturation = 0.68 + (digest[2] / 255) * 0.17
    value = 0.66 + (digest[3] / 255) * 0.20
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return int(red * 255), int(green * 255), int(blue * 255)


def valid_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = map(float, value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def page_regions(row: dict[str, Any]) -> dict[int, list[list[float]]]:
    regions: dict[int, list[list[float]]] = defaultdict(list)
    for item in row.get("bboxes") or []:
        if not isinstance(item, dict):
            continue
        bbox = valid_bbox(item.get("bbox"))
        if bbox:
            regions[int(item.get("page") or row["page"])].append(bbox)
    if not regions:
        bbox = valid_bbox(row.get("bbox"))
        if bbox:
            regions[int(row["page"])].append(bbox)
    return regions


def union_bbox(boxes: list[list[float]]) -> list[float]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def scaled(box: list[float], sx: float, sy: float) -> tuple[int, int, int, int]:
    return (
        round(box[0] * sx), round(box[1] * sy),
        round(box[2] * sx), round(box[3] * sy),
    )


def draw_chunk(
    draw: ImageDraw.ImageDraw,
    row: dict[str, Any],
    boxes: list[list[float]],
    sx: float,
    sy: float,
    label_font: ImageFont.ImageFont,
) -> None:
    chunk_id = str(row["paragraph_id"])
    color = chunk_color(chunk_id)
    for box in boxes:
        rect = scaled(box, sx, sy)
        draw.rectangle(rect, fill=(*color, 27), outline=(*color, 205), width=2)

    outer = scaled(union_bbox(boxes), sx, sy)
    draw.rectangle(outer, outline=(*color, 255), width=5)
    if len(boxes) > 1:
        centers = []
        for box in boxes:
            rect = scaled(box, sx, sy)
            centers.append(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2))
        for start, end in zip(centers, centers[1:]):
            draw.line((start, end), fill=(*color, 185), width=3)

    label = f"{chunk_id}  {row.get('block_type', 'paragraph')}"
    label_box = draw.textbbox((0, 0), label, font=label_font)
    label_width = label_box[2] - label_box[0] + 12
    label_height = label_box[3] - label_box[1] + 8
    label_x = max(0, min(outer[0], outer[2] - label_width))
    label_y = max(38, outer[1] - label_height)
    draw.rectangle(
        (label_x, label_y, label_x + label_width, label_y + label_height),
        fill=(*color, 238),
    )
    draw.text((label_x + 6, label_y + 2), label, fill="white", font=label_font)


def make_contact_sheets(
    page_images: list[Path], output: Path, columns: int = 2, rows: int = 2
) -> list[Path]:
    board_paths: list[Path] = []
    per_board = columns * rows
    for board_index in range(math.ceil(len(page_images) / per_board)):
        batch = page_images[board_index * per_board:(board_index + 1) * per_board]
        opened = [Image.open(path).convert("RGB") for path in batch]
        try:
            thumb_width = 900
            thumbs: list[Image.Image] = []
            for image in opened:
                height = round(image.height * thumb_width / image.width)
                thumb = image.resize((thumb_width, height), Image.Resampling.LANCZOS)
                thumbs.append(thumb)
            cell_height = max(image.height for image in thumbs) + 46
            board = Image.new("RGB", (columns * thumb_width, rows * cell_height), "white")
            draw = ImageDraw.Draw(board)
            title_font = load_font(25, bold=True)
            for position, (path, thumb) in enumerate(zip(batch, thumbs)):
                col, row = position % columns, position // columns
                x, y = col * thumb_width, row * cell_height
                board.paste(thumb, (x, y + 42))
                draw.text((x + 12, y + 7), path.stem.replace("_", " "), fill="black", font=title_font)
            board_path = output / f"overview_{board_index + 1:02d}.jpg"
            board.save(board_path, quality=91, optimize=True)
            board_paths.append(board_path)
        finally:
            for image in opened:
                image.close()
    return board_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Render final natural-paragraph chunk overlays")
    parser.add_argument("pdf")
    parser.add_argument("paragraphs_jsonl")
    parser.add_argument("output")
    parser.add_argument("--pages", nargs="+", type=int)
    parser.add_argument("--width", type=int, default=1800)
    parser.add_argument("--no-contact-sheets", action="store_true")
    args = parser.parse_args()

    pdf = Path(args.pdf).expanduser().resolve()
    paragraphs = Path(args.paragraphs_jsonl).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    rows = [json.loads(line) for line in paragraphs.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped: dict[int, list[tuple[dict[str, Any], list[list[float]]]]] = defaultdict(list)
    for row in rows:
        for page, boxes in page_regions(row).items():
            grouped[page].append((row, boxes))

    document = fitz.open(pdf)
    selected_pages = args.pages or list(range(1, document.page_count + 1))
    page_font = load_font(22, bold=True)
    label_font = load_font(16, bold=True)
    page_images: list[Path] = []
    index_rows: list[dict[str, Any]] = []
    try:
        for page_number in selected_pages:
            if not 1 <= page_number <= document.page_count:
                raise ValueError(f"Page {page_number} outside 1..{document.page_count}")
            page = document[page_number - 1]
            scale = args.width / page.rect.width
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            draw = ImageDraw.Draw(image, "RGBA")
            sx, sy = image.width / page.rect.width, image.height / page.rect.height

            page_chunks = sorted(
                grouped.get(page_number, []),
                key=lambda item: int(item[0].get("document_order", 10**9)),
            )
            for row, boxes in page_chunks:
                draw_chunk(draw, row, boxes, sx, sy, label_font)

            banner = (
                f"{pdf.stem}  |  page {page_number}/{document.page_count}  |  "
                f"final chunks: {len(page_chunks)}  |  thick = final chunk, thin = merged source region"
            )
            draw.rectangle((0, 0, image.width, 40), fill=(255, 255, 255, 235))
            draw.text((12, 7), banner, fill="black", font=page_font)
            page_path = output / f"page_{page_number:04d}_chunks.png"
            image.save(page_path, optimize=True)
            page_images.append(page_path)
            index_rows.append({
                "page": page_number,
                "chunk_count": len(page_chunks),
                "chunk_ids": [row["paragraph_id"] for row, _ in page_chunks],
                "image": page_path.name,
            })
            image.close()
    finally:
        document.close()

    boards = [] if args.no_contact_sheets else make_contact_sheets(page_images, output)
    manifest = {
        "source_pdf": str(pdf),
        "source_paragraphs": str(paragraphs),
        "page_count": len(page_images),
        "visual_semantics": {
            "heavy_outline": "one final chunk boundary on this page",
            "thin_outline": "source bbox belonging to that final chunk",
            "connector": "multiple source bboxes merged into the same final chunk",
            "same_id_and_color": "same final chunk",
        },
        "pages": index_rows,
        "contact_sheets": [path.name for path in boards],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "rendered_pages": len(page_images),
        "contact_sheets": len(boards),
        "output": str(output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
