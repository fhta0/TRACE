# 收尾清理任务（两处，协调方已验证 v1 功能正确，这是小清理）

只做以下两处，别改其它逻辑。改完不要运行任何东西、不要 pip install。

## 1. 删除死代码：trace/runner.py 的 `_run_vector_doc_injection`

`runner.py` 里的函数 `_run_vector_doc_injection`（约 24–31 行，带那段"截图 IO 在上层"的注释）**从未被调用**——`_run_once` 已内联了投放+派发逻辑。整段函数（含其上方 `# --- vector 分发（v1 只有 doc_injection）---` 注释）删掉。删完确认 `run_case` / `_run_once` / `_aggregate` 仍完整、其它函数不受影响。

## 2. 修正契约文档：CONTRACT.md 的 `permission_mode` 必填标注

CONTRACT.md 第 1 节输入表里，`permission_mode` 当前标为必填（✅）。但代码实际是 `case.get("permission_mode", "default")`，可省略、缺省为 `"default"`。把该行的"必填"从 ✅ 改为 ❌，说明列改为：`"full_access" 或 "default"，可省略，缺省 "default"`。让文档与代码一致。

## 约束
- 只改 `trace/runner.py` 和 `CONTRACT.md` 两个文件。
- 不运行、不安装、不执行工具。
- 改完打印：runner.py 删除后剩余的顶层函数名列表 + CONTRACT.md 里 permission_mode 那一行的新内容。
