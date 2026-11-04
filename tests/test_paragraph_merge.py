from natural_pdf_parser.hybrid_paragraphs import (
    assemble_hybrid_artifacts,
    build_auxiliary_text_elements,
)


PAGE_SIZES = {1: (600.0, 800.0), 2: (600.0, 800.0)}
SECTIONS = [{
    "section_id": "S01",
    "title": "Environmental Management",
    "start_page": 1,
    "end_page": 2,
}]


def block(
    page: int,
    index: int,
    text: str,
    bbox: list[float],
    block_type: str = "paragraph",
    **extra,
) -> dict:
    return {
        "page_number": page,
        "hybrid_block_id": f"b{page}_{index}",
        "text": text,
        "bbox": bbox,
        "block_type": block_type,
        "source_parser": "docling",
        "include_in_text_output": block_type in {"paragraph", "list"},
        "is_auxiliary": False,
        **extra,
    }


def test_body_blocks_are_merged_into_a_natural_paragraph() -> None:
    rows = [
        block(1, 1, "Environmental Management", [40, 70, 400, 90], "title"),
        block(1, 2, "The Group measures greenhouse gas", [50, 110, 450, 125]),
        block(1, 3, "emissions in accordance with the GHG Protocol.", [50, 127, 500, 142]),
    ]
    paragraphs, diagnostics, tables, figures = assemble_hybrid_artifacts(
        rows, PAGE_SIZES, SECTIONS
    )
    assert [row["paragraph_id"] for row in paragraphs] == ["P1_01"]
    assert paragraphs[0]["text"].endswith("GHG Protocol.")
    assert paragraphs[0]["section"] == "Environmental Management"
    assert paragraphs[0]["source_block_ids"] == ["b1_2", "b1_3"]
    assert diagnostics["paragraph_unit"] == "natural_paragraph"
    assert tables == []
    assert figures == []


def test_table_and_figure_do_not_consume_paragraph_ids() -> None:
    rows = [
        block(1, 1, "First body paragraph.", [50, 100, 400, 120]),
        block(1, 2, "Scope 1 | 100 tCO2e", [50, 150, 500, 250], "table"),
        block(1, 3, "Second body paragraph.", [50, 280, 400, 300]),
        block(1, 4, "Emissions chart", [50, 330, 500, 500], "image"),
        block(1, 5, "Third body paragraph.", [50, 530, 400, 550]),
    ]
    paragraphs, _, tables, figures = assemble_hybrid_artifacts(rows, PAGE_SIZES, SECTIONS)
    assert [row["paragraph_id"] for row in paragraphs] == ["P1_01", "P1_02", "P1_03"]
    assert [row["table_id"] for row in tables] == ["T1_01"]
    assert [row["figure_id"] for row in figures] == ["F1_01"]


def test_cross_page_continuation_uses_start_page_id() -> None:
    rows = [
        block(1, 1, "The calculation boundary includes all entities under", [330, 730, 570, 790]),
        block(2, 1, "operational control and the principal emission sources.", [40, 10, 280, 45]),
        block(2, 2, "A new paragraph starts here.", [40, 100, 280, 120]),
    ]
    paragraphs, diagnostics, _, _ = assemble_hybrid_artifacts(rows, PAGE_SIZES, SECTIONS)
    assert paragraphs[0]["paragraph_id"] == "P1_01"
    assert paragraphs[0]["pages"] == [1, 2]
    assert paragraphs[1]["paragraph_id"] == "P2_01"
    assert diagnostics["cross_page_paragraph_count"] == 1


def test_parallel_bilingual_columns_continue_on_their_own_tracks() -> None:
    rows = [
        block(1, 1, "本段中文自然段内容将在下一页继续并保持左栏轨道", [50, 730, 280, 790]),
        block(1, 2, "The English paragraph continues on the next", [330, 730, 570, 790]),
        block(2, 1, "继续书写而不会错误连接到旁边的英文栏自然段", [50, 10, 280, 45]),
        block(2, 2, "page without joining the Chinese column", [330, 10, 570, 45]),
        # Extra blocks make the two-column geometry unambiguous on both pages.
        block(1, 3, "这是左栏中另一个已经结束并且长度足够的完整自然段。", [50, 500, 280, 520]),
        block(1, 4, "A complete earlier paragraph in the right column.", [330, 500, 570, 520]),
        block(2, 3, "这是下一页左栏中另一个新开始的完整自然段。", [50, 120, 280, 140]),
        block(2, 4, "A complete new paragraph in the right column.", [330, 120, 570, 140]),
    ]
    paragraphs, diagnostics, _, _ = assemble_hybrid_artifacts(rows, PAGE_SIZES, SECTIONS)
    cross_page = [row for row in paragraphs if row["cross_page"]]
    assert len(cross_page) == 2
    assert any("本段中文自然段内容将在下一页继续" in row["text"].replace(" ", "") for row in cross_page)
    assert any("English paragraph continues" in row["text"] for row in cross_page)
    assert all(not ("中文" in row["text"] and "English" in row["text"]) for row in cross_page)
    assert diagnostics["cross_page_paragraph_count"] == 2


def test_parallel_columns_can_cross_page_between_complete_sentences() -> None:
    """A physical page break is not necessarily a natural-paragraph break."""
    rows = [
        block(1, 1, "中文自然段的第一個完整句子在這一頁結束。", [50, 730, 280, 790]),
        block(1, 2, "The first complete sentence ends on this page.", [330, 730, 570, 790]),
        block(2, 1, "下一個完整句子在下一頁同一欄繼續向下書寫。", [50, 10, 280, 45]),
        block(2, 2, "The next sentence resumes in the same column.", [330, 10, 570, 45]),
        # Extra blocks make the two-column geometry unambiguous.
        block(1, 3, "較早結束的左欄自然段內容足以讓系統識別欄位。", [50, 500, 280, 520]),
        block(1, 4, "An earlier complete right-column paragraph.", [330, 500, 570, 520]),
        block(2, 3, "下一頁左欄中另外一個新自然段的完整內容。", [50, 120, 280, 140]),
        block(2, 4, "A new right-column paragraph on the next page.", [330, 120, 570, 140]),
    ]
    paragraphs, diagnostics, _, _ = assemble_hybrid_artifacts(
        rows, PAGE_SIZES, SECTIONS
    )
    cross_page = [row for row in paragraphs if row["cross_page"]]
    assert len(cross_page) == 2
    assert any(row["pages"] == [1, 2] and "下一個完整句子" in row["text"] for row in cross_page)
    assert any(row["pages"] == [1, 2] and "The next sentence" in row["text"] for row in cross_page)
    assert diagnostics["cross_page_paragraph_count"] == 2


def test_toc_and_suppressed_nested_text_never_receive_paragraph_ids() -> None:
    rows = [
        block(1, 1, "Contents  Environmental  20", [50, 100, 400, 120], is_toc=True),
        block(
            2, 1, "Text embedded in a figure", [50, 100, 400, 120],
            include_in_text_output=False,
        ),
        block(2, 2, "Actual narrative body.", [50, 150, 400, 170]),
    ]
    paragraphs, _, _, _ = assemble_hybrid_artifacts(rows, PAGE_SIZES, SECTIONS)
    assert [row["paragraph_id"] for row in paragraphs] == ["P2_01"]
    assert paragraphs[0]["text"] == "Actual narrative body."


def test_footnote_and_visual_text_are_preserved_without_paragraph_ids() -> None:
    rows = [
        block(1, 1, "Narrative body.", [50, 100, 400, 120]),
        block(
            1, 2, "Scope 1 follows the GHG Protocol.", [50, 700, 400, 720],
            "footnote", is_auxiliary=True, include_in_text_output=False,
        ),
        block(
            1, 3, "Confirm ESG indicator accuracy", [100, 300, 350, 320],
            "list", include_in_text_output=False,
            exclusion_reason="inside_table_or_image_region",
        ),
    ]
    paragraphs, _, _, _ = assemble_hybrid_artifacts(rows, PAGE_SIZES, SECTIONS)
    notes, visual_texts = build_auxiliary_text_elements(rows, SECTIONS)
    assert [row["paragraph_id"] for row in paragraphs] == ["P1_01"]
    assert notes[0]["note_id"] == "N1_01"
    assert notes[0]["content"].startswith("Scope 1")
    assert visual_texts[0]["visual_text_id"] == "V1_01"


def test_bruteforce_other_block_is_kept_as_body_text() -> None:
    rows = [
        block(
            1,
            1,
            "OCR-recovered narrative from a scan-like page.",
            [20, 20, 580, 780],
            "other",
            source_parser="bruteforce",
            include_in_text_output=True,
        )
    ]
    paragraphs, _, _, _ = assemble_hybrid_artifacts(rows, PAGE_SIZES, SECTIONS)
    assert [row["paragraph_id"] for row in paragraphs] == ["P1_01"]
    assert paragraphs[0]["text"].startswith("OCR-recovered narrative")
    assert paragraphs[0]["source_parsers"] == ["bruteforce"]
