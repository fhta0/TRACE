# 新功能：MSAA 语义定位（作为坐标搜索的第一优先来源）

背景与实测证据见 `FINDING-msaa-route.md`。一句话：
**Chromium 响应 MSAA 的 `WM_GETOBJECT`，不响应 UIA**，
所以能按控件名称/角色拿到 Electron 智能体的输入框和按钮的精确屏幕坐标。

## 设计原则（先读这条，它决定了整个改动的形状）

**MSAA 不替换现有的 `calibrate` 穷举搜索，只是给它一个更好的候选来源。**

```
定位输入框/按钮：
  1. 先试 MSAA 语义定位（按名称/角色）    <- 本次新增
  2. 失败则退回现有穷举搜索                <- 保留，一行不改
  两条路径的成功判据完全一致：prompt-vars 计数 +1
```

理由：
- MSAA 对 Electron 类通用，但**不是万能的** —— 同轮实测里经典记事本几乎什么都不暴露。
- 判据不能变。MSAA 只是"猜得更准的候选"，**猜得准不等于点得中**，
  仍然必须用确定性信号验证过才能用。今天已经在 `-m 300` 和 UIA 上各栽一次
  "在有利条件下验证就当成立"，这条红线不能松。

## 一、新建 `trace/msaa.py`（target 无关）

这个模块**不认识 WorkBuddy**，它只做一件事：把某个窗口的 MSAA 控件树抓回来。

```python
def dump_tree(session, window_title_contains: str,
              max_depth: int = 14, max_nodes: int = 400) -> list[dict]:
    """抓取标题匹配的窗口的 MSAA 控件树。

    返回 [{"depth":int, "role":int, "name":str,
           "x":int, "y":int, "w":int, "h":int, "cx":int, "cy":int}, ...]
    坐标是**屏幕绝对坐标、物理像素**，可直接喂 click_mouse。
    抓不到返回 []。
    """

def find(elements, *, role=None, name=None, name_contains=None,
         screen_w=None, screen_h=None) -> dict | None:
    """按条件挑一个元素。多个命中时取面积最大的那个。

    screen_w/screen_h 给了就过滤掉落在屏幕外的元素
    （实测横向轮播里的元素 x 可能到 2550，远超屏宽）。
    """
```

### PowerShell 脚本（下面这段的遍历逻辑已真机验证，请勿改动其中的调用方式）

写进 `_PS_SCRIPT` 常量。**必须纯 ASCII** —— 脚本里出现中文会触发 ParserError
（UTF-8 无 BOM 写入，PowerShell 5.1 按 ANSI 读，多字节被拆乱），今天踩过两次。
注释一律用英文。

```powershell
$OUT = "C:\Users\Administrator\_trace_msaa.txt"
Set-Content -Path $OUT -Value "" -Encoding UTF8

# P/Invoke. Build the C# source by joining lines - do NOT use a here-string
# (here-strings in .ps1 have triggered ParserError in this environment).
$src = @(
'using System;','using System.Text;','using System.Collections.Generic;',
'using System.Runtime.InteropServices;',
'public class TraceMsaa {',
'  public delegate bool EnumProc(IntPtr h, IntPtr p);',
'  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);',
'  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);',
'  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);',
'  [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr h, uint id, ref Guid iid, out IntPtr pp);',
'  public static List<IntPtr> Kids(IntPtr parent) {',
'    List<IntPtr> r = new List<IntPtr>();',
'    EnumChildWindows(parent, delegate(IntPtr h, IntPtr p) { r.Add(h); return true; }, IntPtr.Zero);',
'    return r; }',
'  public static string Cls(IntPtr h) { StringBuilder sb=new StringBuilder(256); GetClassName(h,sb,256); return sb.ToString(); }',
'}') -join "`n"
Add-Type -TypeDefinition $src -ErrorAction SilentlyContinue
```

遍历部分的**关键点**（每一条都是实测踩出来的，不要"优化"掉）：

1. **必须对 `Chrome_RenderWidgetHostHWND` 子窗口做，不是顶层窗口。**
   顶层窗口的 MSAA 树只有 9 个节点（窗体框架），DOM 全在这个子窗口里。
2. **先发 `WM_GETOBJECT` 唤醒 Chromium 的无障碍支持**：
   `SendMessage($hwnd, 0x003D, [IntPtr]0, [IntPtr](-4))`　（`OBJID_CLIENT = -4`）
3. **取 IAccessible 时 dwId 要写十进制 `4294967292`**。
   写 `0xFFFFFFFC` 会被 PowerShell 解析成 Int32 `-4`，再转 `[uint32]` 就溢出报错。
4. **不需要定义 C# COM 接口**，后期绑定就够：
   `$acc = [Runtime.InteropServices.Marshal]::GetObjectForIUnknown($pp)`
   然后 `$acc.accChildCount` / `accName($i)` / `accRole($i)` / `accChild($i)`。
5. **`accLocation` 的 out 参数用 `[ref]` 就能调**：
   `$acc.accLocation([ref]$x, [ref]$y, [ref]$w, [ref]$h, $i)`
6. **深度要给够**：实测输入框在 **第 12 层**，`max_depth` 默认 14。
   同时要有节点数上限防止失控。

输出每行一条，**用制表符分隔**便于 Python 解析（这一点与实测脚本不同，是本次唯一的格式改动，
首次真机使用时请确认解析正常）：

```
NODE<TAB>depth<TAB>role<TAB>x<TAB>y<TAB>w<TAB>h<TAB>name
```

`name` 放最后，因为它可能含空格。`accLocation` 取不到时整行跳过。

Python 侧用 `session.filesystem.write_file` 写脚本、
`execute_command` 执行、**`session.filesystem.read_file` 回读结果文件**
—— 不要依赖 `execute_command` 的 stdout，实测会丢。
中文名称经 `read_file` 回来是正常的（已验证）。

## 二、`target_workbuddy.py` 增加 MSAA 定位

```python
# MSAA role 常量（只列用到的）
_ROLE_TEXT = 42        # ROLE_SYSTEM_TEXT，可编辑文本框
_ROLE_PUSHBUTTON = 43
_ROLE_PAGETAB = 37

def locate_via_msaa(self) -> dict | None:
    """用 MSAA 语义定位三个控件。任一个找不到就返回 None（交给穷举搜索兜底）。

    实测值（1920x1060 DPI1.25）：
        新建任务  role=37 name='新建任务'  center=(165, 134)
        输入框    role=42 name=''          center=(1118, 496)
        发送      role=43 name='发送'      center=(1580, 578)
    """
```

匹配方式：
- `new_task`：`role=37` 且 `name == "新建任务"`
- `input_box`：`role=42`，**取面积最大的那个**（实测还有个 34x27 的小文本框在侧边栏活动区）
- `send_button`：`role=43` 且 `name == "发送"`

返回 `{"new_task": (x,y), "input_box": (x,y), "send_button": (x,y)}`。
**不要在这里做验证** —— 验证是 calibration 的职责。

## 三、`calibration.py` 把 MSAA 作为第一候选

现在的流程是直接进穷举搜索。改成：

```
1. 归一化（点新建任务 + 关弹窗）—— 保留
2. 【新增】尝试 locate_via_msaa()
     拿到坐标 -> 用现有的探针验证一次（prompt-vars 计数 +1）
       通过 -> 直接返回这组坐标，submit_method="enter"，结束
       不通过 -> 记一行 stderr，落到第 3 步
     拿不到 -> 直接落到第 3 步
3. 现有的穷举搜索 —— 一行不改
```

标定结果 JSON 增加一个字段，记录坐标是怎么来的：

```json
{ "coords_source": "msaa" }      // 或 "search"
```

这对排查很重要 —— 出问题时要能一眼看出这次用的是哪条路径。

stderr 要给出清楚的提示，例如：

```
[TRACE] MSAA 语义定位命中：new_task=(165,134) input_box=(1118,496) send=(1580,578)
[TRACE] MSAA 坐标验证通过（prompt-vars 13 -> 14），跳过穷举搜索
```

或

```
[TRACE] MSAA 未命中（缺 input_box），退回穷举搜索
```

## 四、`doctor` 增加一项（可选，不计入 failed）

加一项检查：MSAA 树能否抓到（抓到几个节点）。
抓不到只给 ⚠️，因为穷举搜索仍然可用。

## 约束

- 新建 `trace/msaa.py`；改 `trace/target_workbuddy.py`、`trace/calibration.py`，
  必要时 `trace/cli.py`（doctor 那项）。
- **不改判定逻辑**：`runner.py` / `oracle.py` / `report.py` 一行不动。
  MSAA 只影响"怎么找到控件"，不影响"结论怎么下"。
- 不引入三方依赖。不运行工具、不 pip install、不发网络（协调方负责真机验证）。
- PowerShell 脚本**必须纯 ASCII**，不得使用 here-string。
- 改完打印：`msaa.py` 的 `_PS_SCRIPT` 全文、`dump_tree` / `find` 全文、
  `locate_via_msaa` 全文、`calibration.py` 里新增的 MSAA 分支，
  以及一段成功命中和一段未命中的 stderr 示例文案。
