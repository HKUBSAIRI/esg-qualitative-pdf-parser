# 主流程迁移清单

1. 在独立环境安装 `pdf_natural_paragraph_parser[all]`。
2. 固定一份 PDF 和对应的 Docling JSON，分别运行旧入口与新入口。
3. 比较 `paragraphs.jsonl`、`tables.jsonl`、`figures.jsonl`、`sections.json`。
4. 主流程改为调用 `pdf-natural-paragraphs parse`，或导入
   `natural_pdf_parser.build_final_artifacts`。
5. 下游正文检索读取 `block_type=paragraph`；定性指标检索同时读取
   `block_type=table_text`，标题和列表按业务需要处理。
6. 下游溯源使用 `pages`、`bboxes` 和 `source_block_ids`，不要只读取单值 `bbox`。
7. 保留 `parse_manifest.json` 中的输入 SHA-256，便于版本审计。
8. 新入口稳定运行后，再决定是否删除实验工程中的重复脚本；本次迁移不删除旧代码。
