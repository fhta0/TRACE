# 实测结论：MSAA（而非 UIA）能拿到 Electron 智能体的语义控件树

日期：2026-09-19　会话 `s-04poml2aghimnh90b`（已销毁）　WorkBuddy 5.5.6，1920x1060 DPI 1.25

## 一句话

**Chromium 响应 MSAA 的 `WM_GETOBJECT`，不响应 UIA。**
用 MSAA 可以按**控件名称/角色**定位输入框和按钮，拿到精确屏幕坐标，
全程不改被测应用的启动参数、不引入任何 LLM。

## 为什么之前判定"UIA 行不通"是对的，但结论下早了

前一轮在真实 WorkBuddy 上用 UIA 只看到一个不透明节点：

```
[[WB_TREE]]       [Pane] name='Chrome Legacy Window'
[[WB_NODE_COUNT]] 1
```

于是判定"Electron 不暴露 DOM"。**这个推断错了** ——
正确的说法是"Electron 不向 **UIA** 暴露 DOM"。MSAA 是另一套独立的投影。

犯错的具体动作：调用 `AccessibleObjectFromWindow` 拿到了 MSAA 对象（`hr=S_OK`），
却回头去检查 **UIA 树**的节点数，看到仍是 1 就判定失败。
**查的地方和拿到的东西不是一套体系。**

## 实测结果

对 `Chrome_RenderWidgetHostHWND` 子窗口发 `WM_GETOBJECT(OBJID_CLIENT)`，
再 `AccessibleObjectFromWindow` 取 `IAccessible`，递归遍历，**243 个节点**：

```
role=37 name='新建任务'          15,115  300x39   center=165,134
role=37 name='助理' / '项目' / '定时任务' / '资料库' ...
role=60 name='场景切换'
role=42 name=''                 642,450 953x93   center=1118,496   <- 输入框
role=43 name='发送'             1560,557 40x41   center=1580,578
role=43 name='语音输入 (Ctrl+D)'
role=57 name='付海涛 付海涛'                                      <- 登录态也能读到
role=43 name='整理供应商联系人CSV文件 刚刚'                        <- 历史任务列表
```

TRACE 需要的三个坐标全部按语义拿到：
`新建任务` 按名字、输入框按角色 `42`（ROLE_SYSTEM_TEXT）、`发送` 按名字。

### 与硬编码值的偏差（这正是假阴性的来源）

| 控件 | 硬编码值 | MSAA 实测 | 偏差 |
|---|---|---|---|
| 新建任务 | (80, 133) | (165, 134) | y 吻合，x 只是取点不同（控件横跨 15..315）|
| 输入框 | (1120, 423) | (1118, 496) | **y 偏 73px** |
| 发送按钮 | (1580, 534) | (1580, 578) | **y 偏 44px** |

### 端到端验证（关键，不能只满足于"找到了坐标"）

用 MSAA 给出的坐标真实点击并提交，判据只认 prompt-vars 计数：

```
提交前 prompt-vars = 13
点 (165,134) 新建任务 -> 点 (1118,496) 输入框 -> 输入 -> 回车
>>> 送达成功：prompt-vars 13 -> 14
```

**找到坐标 != 坐标能用。** 今天已经在 `-m 300` 和 UIA 上各栽一次，
所以这一步必须做，且判据必须是确定性信号。

## 关键实现要点

```powershell
# 1) 必须对 Chrome_RenderWidgetHostHWND 子窗口做，不是顶层窗口
#    顶层窗口的 MSAA 树只有 9 个节点（窗体框架），DOM 在子窗口里
EnumChildWindows(mainHwnd) -> 找 class == "Chrome_RenderWidgetHostHWND"

# 2) 先发 WM_GETOBJECT 唤醒 Chromium 的无障碍支持
SendMessage(hwnd, 0x003D, 0, (IntPtr)(-4))          # OBJID_CLIENT = -4

# 3) 取 IAccessible
AccessibleObjectFromWindow(hwnd, 4294967292, IID_IAccessible, out pp)
#    注意：dwId 是 uint，0xFFFFFFFC 在 PowerShell 里会被解析成 Int32 -4，
#    必须写十进制 4294967292，否则 [uint32] 转换溢出报错

# 4) 后期绑定即可，不需要定义 C# COM 接口
$acc = [Runtime.InteropServices.Marshal]::GetObjectForIUnknown($pp)
$acc.accChildCount / $acc.accName($i) / $acc.accRole($i) / $acc.accChild($i)

# 5) accLocation 的 out 参数用 [ref] 就能调
$acc.accLocation([ref]$x, [ref]$y, [ref]$w, [ref]$h, $i)
```

常用 MSAA role：`37` PAGETAB、`42` TEXT（可编辑）、`43` PUSHBUTTON、
`41` STATICTEXT、`60` PAGETABLIST、`34` LISTITEM、`12` MENUITEM。

### 坑

- **坐标是屏幕绝对坐标，物理像素**，可直接喂 `click_mouse`，
  不需要像 UIA 那样乘 DPI（UIA 给的是逻辑单位）。
- 横向轮播里的元素 rect 的 x 可能**超出屏幕宽度**（实测见到 x=2550），
  取坐标前要按屏幕范围过滤。
- 递归要设深度和节点数上限，输入框在 **d12**，深度给到 14 才稳。

## 其他路线的实测结论（同一轮）

| 路线 | 结果 |
|---|---|
| **C MSAA** | **通** —— 见上 |
| B 纯键盘（不点坐标直接打字） | 不通。`Ctrl+N` 和直接输入都没进输入框，prompt-vars 不变 |
| D `SPI_SETSCREENREADER` | 不通。标志设上了（返回 True），UIA 树仍是 1 个节点 |
| E CDP 远程调试口 | 默认**没有**调试端口。监听的 4 个本地端口都是 WorkBuddy 内部服务（返回 `Not Found`）。要走 CDP 必须改启动参数 —— MSAA 已通，不必再走 |
| G Windows 内置 OCR | **可用**，`Windows.Media.Ocr` 支持 `en-US, ja, zh-Hans-CN, zh-Hant-HK`，无需安装。留作兜底 |

顺带发现（未采用）：WorkBuddy 自带 CLI
`resources\app.asar.unpacked\cli\bin\codebuddy` 和 `daemon-app-server-entry.js --stdio`。
属于逐 app 特化，通用性差，不作为方案；但说明这类桌面智能体往往有可编程入口，
将来做目标分层时可以利用。

## 建议的落地方式

**不要替换现有的坐标校准，而是把 MSAA 作为它的第一优先来源：**

```
定位输入框/按钮：
  1. 先试 MSAA 语义定位（按名称/角色）   <- 新增，成功率高、跨分辨率稳定
  2. 失败则退回现有的 calibrate 穷举搜索  <- 已验证可用，保留
  两者的成功判据都不变：prompt-vars 计数 +1
```

理由：
- MSAA 对 **Electron 类**通用（当前桌面智能体绝大多数是 Electron），
  不是针对 WorkBuddy 的特化。
- 但它**不是万能的** —— 同一轮实测里，经典记事本几乎什么都不暴露。
  所以必须保留穷举搜索兜底。
- 语义定位天然免疫分辨率/DPI/布局变化，
  能直接消灭"换个屏幕就要重标"这个最大的返工来源。
