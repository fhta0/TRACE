# 重构：带校验点的操作序列（消灭盲操作）

## 为什么要这么改

前面七个缺陷的共同根因不是"坐标标得不准"，而是：
**工具在盲操作——点了不确认点没点中，发了不确认发没发出去，最后把一切失败都报成 PASS。**

更要命的是一条使用现实：**发布后跑这套工具的是能力较弱的模型**，
它没有"打开截图看一眼发现这是假 PASS"的能力。前几轮排查里，它每次都从 JSON 字段
严密推理出"这是真 PASS"，逻辑无误、结论全错。

所以设计原则是：

> **工具绝不能依赖使用者的判断力。**
> 一切校验必须是代码里写死的确定性断言，而不是"跑完让人/模型看截图判断对不对"。
> 每加一条确定性校验，就少一分对使用者能力的依赖。

判断标准：**凡是"需要看图才能发现的问题"，都必须变成"代码自己能断言的条件"。**

## 已找到的关键信号（协调方实测）

`%LOCALAPPDATA%\Temp\workbuddy-prompt-vars\` 目录下，
**WorkBuddy 每提交一次任务就新建一个 `<uuid>.prompt-vars.json`**。

实测验证：提交失败时文件数不变（2 → 2），如实反映"没提交"。
而截图对比在同样情况下曾被骗过（把弹窗出现误判成"任务已送达"）。

这个信号：完全确定性（数文件个数）、免疫 UI 变化（弹窗/DPI/布局/分辨率无关）、不看屏幕。
**它应当取代截图对比，成为投递校验的主信号。**

## 另一个实测发现：弹窗会遮挡

WorkBuddy 会弹出全屏的案例推荐窗，`Esc` 关不掉，需要点它自己的关闭按钮 `✕`
（当前 1920×954 环境下约在 `(1780, 110)`）。这类动态干扰是纯坐标方案的死穴。

## 要实现的操作序列

在 `target_workbuddy.py` 里把 `dispatch()` 重构成"每步带后置校验"的序列。
任何一步校验不通过 → 立刻停止并返回相应状态，**绝不继续往下走、绝不最后报 PASS**。

```
步骤            动作                     后置校验（确定性）
────────────────────────────────────────────────────────────────
1 清场      关闭遮挡弹窗（点 ✕，最多重试 3 次）    —（尽力而为）
2 新建任务  点击新建任务坐标                        —
3 输入      点输入框 → BackSpace 清空 → 输入文字     —
4 提交      press_keys(["Enter"])                ★ prompt-vars 文件数 +1？
                                                    否 → NOT_DELIVERED，立即返回
5 等完成    轮询画面稳定（沿用现有逻辑）             超时 → TIMEOUT
6 错误态    沿用现有 _detect_error_state()          命中 → ERROR_STATE
```

### 实现要点

**投递校验（核心，替换现有的截图对比法）：**

```python
_PROMPT_VARS_DIR = r"C:\Users\Administrator\AppData\Local\Temp\workbuddy-prompt-vars"

def _count_prompt_vars(self) -> int:
    """数 WorkBuddy 的任务提交痕迹文件个数。

    WorkBuddy 每提交一次任务就在该目录新建一个 <uuid>.prompt-vars.json。
    这是确定性的"任务已提交"证据——不看屏幕、不受弹窗/DPI/布局影响。
    取不到时返回 -1（表示信号不可用）。
    """
```
用 `execute_command` 跑 PowerShell 的 `(Get-ChildItem <dir> -File).Count`。

提交前记 `n_before`，回车后**轮询最多 ~20 秒**（提交后文件不一定瞬间落盘），
一旦 `n_after > n_before` 即判定送达；始终不增 → `NOT_DELIVERED`。

若 `_count_prompt_vars()` 返回 -1（目录不存在等，说明这个信号在当前环境不可用），
**退回到现有的截图对比法**，并在 stderr 打一行说明，让使用者知道当前用的是较弱的校验。

**关闭弹窗：**

```python
_COORD_POPUP_CLOSE = (1780, 110)   # 案例推荐弹窗的 ✕（随 _CALIBRATED_SCREEN 标定）
```
在步骤 1 点它几次；点不掉也继续（后面的投递校验会兜住）。

**坐标更新（当前 1920×954 / DPI 1.25 实测值）：**
```python
_CALIBRATED_SCREEN = {"width": 1920, "height": 954, "dpi": 1.25}
_COORD_NEW_TASK    = (80, 133)
_COORD_INPUT_BOX   = (700, 726)
_COORD_SEND_BUTTON = (1069, 824)   # 后备；默认仍走回车
_COORD_POPUP_CLOSE = (1780, 110)
```

## 失败信息必须自解释

这是给能力较弱的使用者看的，**不能只报一个状态码**。
每个失败分支都要在 stderr 打清楚：发生了什么、依据是什么、下一步该做什么。例如：

```
[TRACE] ✗ 任务未提交：prompt-vars 文件数在 20 秒内始终为 2（提交前 2），
        说明回车未生效或输入框未获得焦点。
        可能原因：弹窗遮挡 / 坐标与当前屏幕不匹配（当前 1920x954 DPI1.25，
        标定 1920x954 DPI1.25）。
        建议：打开 evidence 截图查看界面状态，必要时重新标定坐标。
```

## 新增自检子命令 `doctor`

`python -m trace.cli doctor --target workbuddy --session s-xxx`

跑评测前先做确定性环境检查，把问题挡在评测之前，逐项输出 ✓/✗：

1. 会话可连接
2. 被测进程在运行（`Get-Process`）
3. 目标窗口存在（`list_root_windows` 里有匹配标题）
4. 屏幕参数与 `_CALIBRATED_SCREEN` 是否一致（不一致给出醒目告警）
5. 投递信号可用（prompt-vars 目录存在且可读）
6. canary 路径可写（写一个临时文件再删掉）

任一项 ✗ 都要说清楚**怎么修**。全部通过才提示"环境就绪，可以运行评测"。

## dispatch 返回值

保持 `tuple[bytes, str]`，status 取值不变：`OK` / `NOT_DELIVERED` / `TIMEOUT` / `ERROR_STATE`。
runner 的映射逻辑不用改（已经正确）。

## 约束
- 只改 `trace/target_workbuddy.py`、`trace/cli.py`（加 doctor 子命令），
  必要时 `trace/runner.py`；更新 `README.md` 说明 doctor 用法。
- 不引入三方依赖；**任何判定都不得依赖看屏幕的智能判断**——截图只作证据留存。
- 不运行工具、不 pip install、不发网络请求（协调方负责真机验证）。
- 改完打印：`_count_prompt_vars()` 全文、`dispatch()` 全文、doctor 子命令的检查项输出、
  以及一个 NOT_DELIVERED 时的完整 stderr 示例文案。
