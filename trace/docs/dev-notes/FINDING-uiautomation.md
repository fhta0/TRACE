# 实测结论：AgentBay Windows 镜像里 UIAutomation 可用，且能穿透 Chromium 看到网页 DOM

日期：2026-09-19　方式：起干净 Windows 沙箱跑 PowerShell 探针，跑完立即销毁（共 4 次，零残留）

## 为什么要验这个

坐标方案反复失效是本项目最大的返工来源：DPI 1.25 与 1.0 系统性偏移、
WorkBuddy 至少四种布局导致输入框在 0.40H / 0.44H / 0.78H 之间跳、换机器全部作废。
现有缓解手段是穷举搜索 + 每次评测前现场校准，能用但脆。

UIAutomation 如果可用，就能**按控件类型/名称定位**而不是按像素 —— 确定性，
且换分辨率/DPI/布局全不受影响。关键是：**这不需要引入任何 LLM**，红线不动。

## 实测结果

沙箱环境：`windows_latest`，1024x768，DPI 1.0。

| 探针 | 结果 |
|---|---|
| `Add-Type UIAutomationClient, UIAutomationTypes` | **OK** —— 程序集可加载 |
| 枚举顶层窗口 | OK，能拿到 Name / ClassName / ProcessId |
| Chromium 窗口定位（`ClassName -like "*Chrome_WidgetWin*"`） | OK |
| **按 aria-label 找网页里的 textarea** | **HIT** `[Edit] rect=12,151 600x121 center=312,212` |
| **ValuePattern 直接写入文本（不用鼠标）** | **OK**，`readback=TRACE_WEB_PROBE_991` |
| 按 aria-label 找网页里的 button | **HIT** `[Button] rect=612,256 55x22` |
| 经典记事本的编辑区 | **暴露不出来**（只有两个 Pane，ValuePattern 数 = 0）|
| 按 `ControlType::Document` 找页面节点 | 数量 0 —— **不能拿 Document 当锚点** |

最关键的一条：**Chromium 把网页 DOM 暴露给了 UIA**，
`aria-label="TraceInputBox"` 的 textarea 能被 `NameProperty` 直接命中，
拿到精确 rect，还能直接写值。WorkBuddy 是 Electron（同一渲染引擎），
所以它的输入框很可能同样可以确定性定位。

## 尚未验证的三件事（不要当成已经成立）

1. **WorkBuddy 启动时没有 `--force-renderer-accessibility`。**
   本次探针给 Edge 加了这个 flag。Chromium 通常在 UIA 客户端来查询时会按需打开
   无障碍树，但不保证。**必须在 WorkBuddy 上实测。**
2. **WorkBuddy 的 DOM 未必有有用的 `aria-label`。**
   若没有，退一步按 `ControlType=Edit` + rect 定位仍然是确定性的，
   仍然免疫分辨率/DPI/布局 —— 只是要处理"多个 Edit 选哪个"。
3. **ValuePattern.SetValue 在 React/Electron 上可能不触发框架的 onChange。**
   DOM 的 value 变了，但应用自己"没看见"。这是常见坑。

## 因此推荐的用法（保守版）

> **用 UIA 定位，用真实键鼠输入。**

即：UIA 负责回答"输入框在哪、发送按钮在哪"（拿 rect），
然后仍然走 `click_mouse(center)` + `input_text` + `press_keys(["Enter"])`。

这样拿到坐标健壮性的全部好处，同时避开 SetValue 不被框架感知的风险，
也保持输入路径与真人一致（对评测保真度重要）。
`ValuePattern` 可以留作可选加速路径，但要先在 WorkBuddy 上验证 readback + 应用确实响应。

## 适用范围提醒

UIA 的暴露程度**因应用而异** —— 本次实测里经典记事本几乎什么都不暴露。
所以 UIA 应当作为**首选定位手段 + 坐标搜索兜底**，不是完全替代。
新接一个 agent 时，先跑 UIA 探针；探不到再退回现有的 `calibrate` 穷举搜索。

## 复现

探针脚本见 scratchpad `uia4.ps1` / `uia_probe4.py`（未入库，需要时重建）。
要点：
- PowerShell 里**不要把函数命名为 `R`** —— 那是 `Invoke-History` 的内置别名，
  `R $rect` 会被解析成调历史命令。本次踩过。
- 探针输出**写进沙箱文件再回读**，不要依赖 `execute_command` 的 stdout（会丢）。
- `BoundingRectangle` 可能是 `∞`（离屏元素），`[int]` 强转会炸，必须先判 `IsInfinity`。
- `.ps1` 里避免 here-string（`@"..."@`）和中文注释，曾触发 ParserError；
  需要写文件就从 Python 侧用 `filesystem.write_file`。

---

# 续：在真实 WorkBuddy 上实测 —— UIA 路线**不成立**

日期：2026-09-19，会话 `s-04poml2aghimnh90b`，WorkBuddy 5.5.6 已登录，屏幕 1920x1060 DPI 1.25。

## 结果

```
[[WB_WINDOW]]        WorkBuddy | class=Chrome_WidgetWin_1 | rect=-6,-6 1549x821
[[WB_TREE]]          [Pane] name='Chrome Legacy Window' id='696720'
[[WB_NODE_COUNT]]    1          <- 整棵控件树只有这一个节点
[[WB_EDIT_COUNT]]    0
[[WB_DOCUMENT_COUNT]] 0
[[WB_BUTTON_COUNT]]  0
```

连续探测三次（间隔 5 秒、15 秒），结果完全一致。

## 三个未知项的答案

| 未知项 | 答案 |
|---|---|
| WorkBuddy 不带 `--force-renderer-accessibility` 时无障碍树开不开 | **不开。** 只暴露一个不透明的 `Chrome Legacy Window` Pane |
| Chromium 会不会被 UIA 查询惰性触发建树 | **不会。** 三次查询都没有触发 |
| DOM 有没有可用的 `aria-label` | **无从谈起** —— DOM 根本没暴露出来 |

第三条（`ValuePattern.SetValue` 会不会被 Electron 框架忽略）本轮**无法验证**，
因为前置条件就不成立。

## 为什么 Edge 那次是 HIT

前一轮探针给 Edge 显式加了 `--force-renderer-accessibility`。
那证明的是"**Chromium 开了无障碍树之后** DOM 能被 UIA 看到"，
**不等于**"Chromium 默认就能被看到"。这是两回事，我当时把它当成了同一回事。

又是同一类错误：**在有利条件下验证，然后推广到真实条件。**
（与 `-m 300` 那次同源：在健康状态下验证，然后推广到故障状态。）

## 对 TRACE 的结论

- **坐标搜索 + 现场校准（`calibrate`）仍然是 WorkBuddy 的唯一可行路径**，不要替换。
- UIA 只能作为**接入新 agent 时的可选探测手段**：先探一次，
  暴露得好就用（省掉穷举搜索），暴露不出来就退回坐标搜索。
  `target_workbuddy` 不需要改动。
- 若将来确实需要在 Electron 目标上用 UIA，唯一已知途径是
  **用 `--force-renderer-accessibility` 启动被测应用**。但那是对被测环境的改动，
  必须在 Evidence Package 里显式记录为"非出厂配置"，否则测出来的不是那个产品。
  本轮没有走这条路（会打断用户已完成的扫码登录，且不影响当日主线目标）。

## 附带确认的两件事

1. `get_screen_size` 的读数和**是否有观看端接入**相关（2026-09-18 更正）。
   原记录写作"`1024x768` 是启动早期的过渡值，稳定后才是真实值 `1920x1060`"——
   **这是错的**。实测：会话创建后长期稳定在 1024x768 DPI1.0，
   **用户扫码打开云桌面之后**才切到 1920x1060 DPI1.25。
   分辨率是在**有观看端接入**时才切换的；没人连云桌面时 1024x768 就是合法的稳定态，
   不是"过渡值"，也不会自己变成别的值。
   这一错误推断直接导致了 `wait_for_screen_stable` 里写死 `width > 1024` 的稳定判据，
   结果是 session create 在没人连云桌面时必然空转 180 秒报超时
   （详见 `FIX-session-create-stall.md`）。
   **教训**：观察到的"稳定态"未必是"过渡态"——
   在没有证明它会自动变化之前，不要把一个稳定读数当成过渡值。
2. UIA 的 `BoundingRectangle` 是**逻辑单位**（窗口 1549x821），
   而截图与 `click_mouse` 用的是**物理像素**（1920x1060），比值正好等于 DPI 1.25。
   将来若真用 UIA 坐标驱动点击，**必须乘以 DPI 缩放系数**。
