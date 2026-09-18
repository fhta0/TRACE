# 新功能：HTML 报告（`--report` 参数）

给 TRACE 加一个"评测完出 HTML 报告"的功能。协调方已验证 v1 主流程正确，这是纯新增，别动已有判定逻辑。

## 1. 新增 CLI 参数

在 `run` 子命令加一个可选参数：
```
--report <path.html>    # 可选。给了就在评测后额外生成一份 HTML 报告到该路径
```
不给 `--report` 时行为完全不变（只写 result.json）。

## 2. 新模块 trace/report.py

实现一个**纯函数**（不碰网络、不碰 AgentBay SDK、不读环境变量）：
```python
def render_html(case: dict, result: dict) -> str:
    """把一个 case + 它的 result 渲染成自包含的 HTML 字符串。"""
```
- 输入就是 cli 里已有的 `case`（case.json 解析后的 dict）和 `result`（run_case 返回的 dict）。
- 截图：从 `result["evidence"][i]["screenshot"]` 路径读取 PNG bytes，**base64 内嵌**为 `<img src="data:image/png;base64,...">`，让报告自包含、可离线打开。截图文件不存在时，该位置显示"（截图缺失）"文字，不要抛错。
- 返回完整 HTML 字符串（含 `<!doctype html>`、`<style>`）。

## 3. cli.py 接线

在 `_cmd_run` 里，写完 result.json 之后：
```python
if args.report:
    from . import report
    html = report.render_html(case, result)
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(html)
```
并在 argparse 加 `pr.add_argument("--report", default=None, help="HTML 报告输出路径（可选）")`。

## 4. 报告内容（单用例）

一页，从上到下：
1. **标题区**：用例 id、target、permission_mode、时间戳（用 result.evidence[0].ts 或当前时间）。
2. **双结论**（醒目，最重要）：`Agent Security` 和 `System Protection` 两个大 pill/徽章，FAIL 红、PASS 绿、N/A 灰。旁边标 `failure_rate`。
3. **判定依据**：`verdict_basis`、`root_cause`（有就显示）。
4. **注入内容**：`doc_filename` + `doc_content`（用等宽块展示全文，这是攻击载荷）。
5. **正常任务**：`benign_task` 原文。
6. **逐次证据表**：每个 `evidence[]` 一行/一块，显示 `run` 序号、`canary`（CREATED=红/CLEAN=绿）、`ts`，以及**内嵌截图**。
7. **页脚**：一句话说明"判定基于确定性行为证据（canary 文件存在性），非智能体自述"，以及 v1 边界（system_protection 靠 permission_mode 推断，HITL 检测 v2）。

## 5. 样式要求（照前面那份报告的干净风格）

- **自包含**：所有 CSS 内联在 `<style>`；**不引用任何外部资源/字体/JS**（要能离线打开）。用系统字体栈：`font-family: -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;` 等宽块用 `ui-monospace, monospace`。
- 冷色调、克制；PASS 绿 / FAIL 红 / N/A 灰做语义色。
- 同时适配浅色和深色：`:root` 定义浅色调色板变量，`@media (prefers-color-scheme: dark)` 重定义变量；`body` 显式设 background。所有颜色走变量，别只在 media 块里定义颜色。
- 单列，最大宽度约 840px 居中，留足边距；窄屏（400px）不横向溢出。
- 中文内容。`<title>` 用 `TRACE 报告 <case id>`。

## 6. 约束

- 只新增 `trace/report.py`，改 `trace/cli.py`，并在 `CONTRACT.md` 第 4 节 CLI 里补 `--report` 说明。别动 runner/oracle/provider/target。
- `report.py` 是纯函数，不导入 agentbay、不发网络请求、不读 env。
- 不要运行任何 AgentBay/网络调用、不要 pip install。（协调方用已存的 result.json + 截图离线验证渲染。）
- 改完打印：新增/修改的文件列表 + `render_html` 的函数签名。
