# 任务：整理 trace/ 的文档（纯移动/整理 + 一致性核对，不改代码）

现在 `trace/` 顶层的 .md 文件混着两类：交付文档 vs 开发过程件。把它们分开、加索引、做跨文档一致性检查。

## 1. 分类与归位

**交付文档（留在 `trace/` 顶层，是门面）：**
- `README.md` — 启动说明
- `CONTRACT.md` — 平台对接契约

**开发过程件（移动到新目录 `trace/docs/dev-notes/`）：**
- `BRIEF-v1.md`
- `BRIEF-report.md`
- `BRIEF-readme.md`
- `BRIEF-organize.md`（本文件，也一起移过去）
- `FIX-oracle.md`
- `FIX-cleanup.md`

用 `mkdir -p docs/dev-notes` 建目录，再把上述 6 个文件移进去（可用 `git mv` 若是 git 仓库，否则 `mv`）。移动后 `trace/` 顶层只剩 `README.md`、`CONTRACT.md` 两个 md（外加代码目录 `trace/`、`cases/`）。

## 2. 加一个文档索引

在 `trace/docs/dev-notes/` 下建 `README.md`（简短），一句话说明这里是"开发过程留痕（协调方给容器内 Claude 的开发 brief 与修正记录），非使用文档；使用请看仓库根的 README.md”，并列出每个文件一行说明：
- BRIEF-v1 — v1 工具首版开发规格
- BRIEF-report — HTML 报告功能规格
- BRIEF-readme — README 编写规格
- BRIEF-organize — 文档整理规格
- FIX-oracle — canary 判定 bug（P0）修正
- FIX-cleanup — 死代码 + 契约小不一致清理

## 3. 跨文档一致性核对（发现问题在你的输出里报告，不要擅自改判定逻辑）

对 `README.md` 和 `CONTRACT.md` 做一遍核对，确认：
- 字段名一致（如 `agent_security` / `system_protection` / `failure_rate` / `verdict_basis` / `root_cause` / `canary` 等在两份里拼写一致）；
- CLI 用法一致（`python -m trace.cli run --case --out --session --report` 参数在两份里描述一致）；
- 版本/边界描述不矛盾（如 v1 只支持 doc_injection + file_exists、system_protection 靠 permission_mode 推断、HITL 在 v2）；
- README 第 8 节到 CONTRACT 的引用路径正确（CONTRACT.md 仍在顶层）。

若发现**文档措辞层面**的不一致（错别字、字段拼写、失效的相对路径引用），可直接在对应 md 里修正。若发现**涉及代码行为**的疑似矛盾，不要改，写进你的输出报告里交给协调方判断。

## 4. 约束
- 只做文档的移动/整理与文档内措辞修正；不改任何 `.py` 文件、不改判定逻辑、不改 CONTRACT 的 schema 语义。
- 不运行工具、不 pip install、不发网络请求。
- 改完打印：最终 `trace/` 顶层文件列表 + `docs/dev-notes/` 下文件列表 + 一致性核对结论（发现的问题或"未发现不一致"）。
