# ESG 定性 PDF 解析

面向 ESG 定性分析、证据召回和评分任务的结构化 PDF 解析流水线。项目将完整 PDF 转换为可审计的自然段、列表、文字表格、章节和版面定位数据；正文以自然段为稳定 chunk，不按固定字符数强制切断。

## 项目定位

- 为 ESG Agent、RAG、证据标注和人工复核提供统一 PDF 输入契约。
- 保留页码、章节、bbox、源块、阅读顺序和跨页关系，支持从模型结论回查原文版面。
- 将正文、表格、图片、视觉短文本和辅助文本分流，避免非正文内容污染定性检索。
- 解析结果可缓存、复用和版本追踪；评分或业务 Agent 不需要重复解析 PDF。

这是从 `pdf_parser_benchmark/` 实验工程中抽出的稳定版本。实验工程继续用于比较解析器；
本目录只保留最终生产链路：

```text
完整 PDF
  -> Docling 版面与结构识别
  -> 标准化标题、正文、列表、页眉页脚、表格、图片和 bbox
  -> 栏位与页内阅读顺序恢复
  -> 依据间距、缩进、字体和语言合并完整自然段
  -> 相邻页面按栏位轨道进行跨页续接
  -> paragraphs.jsonl
```

Docling 的原始 `text` 块不是最终 chunk。最终正文 chunk 是
`paragraphs.jsonl` 中 `block_type="paragraph"` 的记录，其 `text` 保存完整自然段。
文字表格以 `block_type="table_text"` 保存为逐行可读文本，参与定性检索。

## 安装

建议单独创建环境：

```bash
git clone https://github.com/HKUBSAIRI/esg-qualitative-pdf-parser.git
cd esg-qualitative-pdf-parser
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[all,test]'
```

Docling 首次运行会下载开放模型。也可以把已下载模型放在固定目录，通过
`--artifacts-path` 传入，避免生产环境重复下载。

## 解析 PDF

从 PDF 开始完整运行：

```bash
pdf-natural-paragraphs parse report.pdf \
  --output runs/report \
  --artifacts-path /path/to/docling-models
```

复用已有的完整 Docling JSON：

```bash
pdf-natural-paragraphs parse report.pdf \
  --docling-json /path/to/report.json \
  --output runs/report
```

不安装包时也可以在本目录直接运行：

```bash
python3 -m natural_pdf_parser parse report.pdf \
  --docling-json /path/to/report.json \
  --output runs/report
```

## 生成最终 chunk 可视图

```bash
pdf-natural-paragraphs visualize report.pdf \
  runs/report/paragraphs.jsonl \
  runs/report/chunk_overlays
```

粗框表示一个最终 chunk，细框表示合并进该 chunk 的 Docling 源区域。相同 ID
和颜色始终属于同一个最终 chunk。跨页 chunk 会在两页保留同一 ID 和颜色。

## 稳定输出

- `paragraphs.jsonl`：最终标题、列表、完整自然段和可读文字表格。
- `retrieval_chunks.jsonl`：正文、列表与 `table_text` 检索输入，并保留父块 ID。
- `blocks.jsonl`：标准化后的 Docling 原始块，只用于审计。
- `visual_texts.jsonl`：短标签和设计元素文字，不污染自然段正文。
- `tables.jsonl`、`figures.jsonl`：独立的结构化元素流。
- `sections.json`：物理页到章节的映射。
- `paragraph_diagnostics.json`：源块数量、栏位和跨页合并统计。
- `summary.json`、`parse_manifest.json`：页数、版本、输入哈希和数据契约。

`paragraphs.jsonl` 的主要字段：

```json
{
  "paragraph_id": "P31_004",
  "section": "Environmental Management",
  "section_id": "SEC017",
  "page": 31,
  "pages": [31, 32],
  "text": "A complete natural paragraph...",
  "block_type": "paragraph",
  "source_parser": "docling",
  "source_block_ids": ["#/texts/851", "#/texts/859"],
  "bbox": [62.1, 610.2, 529.4, 731.8],
  "bboxes": [
    {"page": 31, "bbox": [62.1, 610.2, 529.4, 731.8]},
    {"page": 32, "bbox": [62.0, 62.3, 528.9, 143.1]}
  ],
  "bbox_coordinate_system": "pdf_points_top_left",
  "cross_page": true
}
```

## 合并主流程

推荐主流程只依赖以下接口：

```python
from pathlib import Path
from natural_pdf_parser import build_final_artifacts

summary = build_final_artifacts(
    Path("report.pdf"),
    Path("report.docling.json"),
    Path("runs/report"),
)
```

如果主流程需要自动启动 Docling，调用命令行入口更适合，因为它会同时保留
Docling 日志和原始 JSON。主流程不要再导入 `pdf_parser_benchmark` 或原项目的
`esg_groundtruth` 包。

## 当前规则

- 句号不是页内硬切段条件，几何间距和缩进才是主要边界。
- 中英文平行栏不会互相连接。
- 多栏跨页按每条栏位轨道一对一匹配，不只比较页尾最后块和页首第一块。
- 下一页出现新标题时禁止跨页续接。
- 页眉、页脚、目录、表格、图片和短视觉标签不进入正文自然段。
- 图片内部 OCR 短文本进入 `visual_texts.jsonl`，不会伪装成自然段。
- Docling 原生表格按行输出；被包成 picture 但具有稳定行列对齐的文字表格
  也会重建为 `table_text`，同时继续保留 `tables.jsonl` 结构化结果。
- 完整自然段不会因为字符数过长而在 `paragraphs.jsonl` 中强制拆分。

## 验证

```bash
python -m pytest -q
```

迁移时应先用固定 PDF 和固定 Docling JSON 运行新旧两套实现，并比较所有稳定
产物的 SHA-256 或逐行 JSON 内容；确认一致后再让主流程切换入口。
