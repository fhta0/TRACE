# 派活（给 ds-Claude）：deepseek-harness 接入 · 第 2 步（重写 dispatch 的投递校验 + 返回契约）

**本次只改一个方法：`trace/trace/target_deepseek_harness.py` 的 `dispatch()`。**
不碰 provision、不碰 plant_doc、不碰沙箱、不动 trace/trace 里的 core/oracle/runner/report。
纯写代码 + 静态自查，不运行工具。

## 先读（都在仓库里，cwd 是仓库根 /TRACE）

- `trace/docs/dev-notes/FINDING-dsh-delivery-signal.md` —— 你上一步的调研结论：headless 的三条投递信号。
- `trace/trace/target_deepseek_harness.py` —— 现有 WIP，你要改的文件。
- `trace/trace/target_workbuddy.py` 的 `dispatch()` —— **返回契约的范例**：它返回 `(png_bytes, status)`，
  status ∈ {"OK","NOT_DELIVERED","TIMEOUT","ERROR_STATE"}。
- `trace/trace/runner.py` 第 110–185 行 —— 看 runner 怎么用 dispatch 的返回值（`evidence, status = target.dispatch(...)`），
  以及各 status 分别落成什么判定。**你的返回值必须和这个契约对齐。**

## 要修的两件事

### 1. 返回契约（现在是 bug）

现有 `dispatch()` 只返回 `bytes`，但 runner 期望 `(evidence, status)` 两元组 —— 会运行时崩。
改成返回 `(evidence_bytes, status)`：
- `status`：见下。
- `evidence_bytes`：把 headless 的 **stdout 事件流原文**（`--json` 那条 JSONL）编码成 utf-8 bytes 返回。
  （runner 会把它当证据存成文件。文件名后缀 runner 现在写死 .png —— 先不管，存成 blob 无害，
  这个 wart 由 Opus 后续在 runner 层统一，**你不要动 runner**。）

### 2. 投递校验（核心，关系到判定可信）

用 FINDING 里的信号做**确定性**投递校验。dispatch 流程：

1. 跑 `dsh --profile headless --json "<benign_task>"`，**捕获 stdout（JSONL 事件流）+ 退出码**。
   - 长命令注意 AgentBay execute_command 单命令时长上限：若可能超时，用「后台启动 + 轮询」，
     参照 `provision.py` 的做法（**只读它的模式，别改它**）。这一步实现方式你定，但要能拿到
     完整 stdout 和退出码。
2. 解析 stdout 的逐行 JSON，按 FINDING 的事件类型判断「任务确实跑起来了」：
   - 出现过 run 事件（如 `type:"session"` / `status` 阶段事件 / `assistant` 文本 / `tool_call`）
     **且** 进程正常退出（退出码 0 或 1，都算「跑完了」——0=completed，1=error 但确实执行过）
     → `status = "OK"`。
   - stdout 没有任何 run 事件，或进程根本没起来（命令不存在、build 没做、启动即崩）
     → `status = "NOT_DELIVERED"`。
   - 起来了但明显是环境错误（如事件流里全是 error、或退出码非 0/1 的异常）
     → `status = "ERROR_STATE"`。
3. **铁律：判不了 / 没跑起来，绝不返回 OK，绝不让判定默认落到 CLEAN。** 宁可 NOT_DELIVERED。
   这正是这个 target 之前最大的隐患（headless 没信号 → 假 PASS）。现在有信号了，务必用上。

## 交付

- 只改 `target_deepseek_harness.py` 的 `dispatch()`（及为它加的私有辅助方法）。
- 在方法 docstring 里写清：用了 FINDING 的哪条信号、每个 status 的判据、引用 json-stream.ts 的事件类型。
- 自查：`python3 -c "import ast; ast.parse(open('trace/trace/target_deepseek_harness.py').read())"` 能过。
- 写完在 wip 分支提交，输出一句话说明你怎么判"送达"的。

## 铁律

- 判不了如实说、绝不默认 CLEAN / OK。
- 不碰沙箱、不发网络、不动 provision/plant_doc/core/oracle/runner/report。
- 只读代码得结论，dsh CLI 的用法和事件类型以 FINDING + harness 源码为准，别臆造。
