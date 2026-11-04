# 参与开发

## 本地环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[all,test]'
python -m pytest -q
```

## 变更要求

- 修改自然段合并、阅读顺序、跨页续接或输出字段时，必须补充对应测试。
- 修改稳定输出契约时，应同步升级 `SCHEMA_VERSION`，并更新 README、技术报告和迁移说明。
- 不提交原始 ESG 报告、Docling 模型、解析运行目录、缓存、密钥或包含本机绝对路径的产物。
- 新增解析规则时优先保留源块 ID、页码和 bbox，不能为了简化检索而破坏可审计性。
- 提交前运行 `python -m pytest -q`。

## 推荐评审内容

- 自然段是否被错误拆分或合并；
- 中英文平行栏是否串栏；
- 跨页续接是否保持同一栏位轨道；
- 表格和图片文字是否错误进入正文；
- `paragraph_id`、页码、bbox 和源块 ID 是否可回查；
- 新旧稳定产物是否发生非预期变化。
