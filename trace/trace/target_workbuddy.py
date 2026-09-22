"""WorkBuddy 桌面目标驱动：投放文档、新建任务、发送任务、截图存证。

坐标来自 §5 实测：1920×954 / DPI 1.25、窗口已最大化的前提下的 (x, y)。
API 来自 §4：command / filesystem / computer 三类。

★ 核心不变量（§FIX-verified-steps）：
    工具绝不能依赖使用者的判断力。
    一切校验必须是代码里写死的确定性断言，而不是「跑完让人/模型看截图判断对不对」。
    每一步操作都带后置校验；任何一步校验不通过 → 立刻停止并返回相应状态，
    绝不继续往下走、绝不最后报 PASS。
"""
from __future__ import annotations

import struct
import sys
import time
import zlib
from typing import Any

from .target import Target

_DESKTOP = "C:\\Users\\administrator\\Desktop"

# 坐标标定环境：换分辨率或 DPI 会失准
# §FIX-verified-steps：1920×954 / DPI 1.25（实测值）
_CALIBRATED_SCREEN = {"width": 1920, "height": 954, "dpi": 1.25}

# §FIX-verified-steps：1920×954 / DPI 1.25 实测坐标（窗口已最大化）
# ★ 这是「已关闭弹窗与右侧预览面板」的干净新任务态坐标；
# 若界面带预览面板，布局会整体左移/下移，坐标需重新标定。
_COORD_NEW_TASK    = (80, 133)
_COORD_INPUT_BOX   = (1120, 423)
_COORD_SEND_BUTTON = (1580, 534)   # 后备；默认仍走回车

# WorkBuddy 案例推荐弹窗有两层 ✕ 按钮，必须都点才能完全关闭：
_COORD_POPUP_CLOSE_PANEL = (1780, 110)   # 右侧面板的 ✕
_COORD_POPUP_CLOSE_CARD  = (351, 105)    # 内层卡片的 ✕

# §FIX-calibration-v2：弹窗关闭按钮位置按屏幕比例换算（不再写死像素）。
# 由 1920×954 下实测的两个 ✕ 位置反推：
#   (1780, 110) -> (0.927, 0.104)   右侧面板 ✕
#   (351,  105) -> (0.183, 0.099)   内层卡片 ✕
# 换算后任何分辨率都能命中，避免「要关弹窗得先有坐标，而坐标正是要校准的」死锁。
_POPUP_CLOSE_RATIOS = ((0.927, 0.104), (0.183, 0.099))

# WorkBuddy 每次提交任务就在该目录新建一个 <uuid>.prompt-vars.json。
# 这是确定性的「任务已提交」证据——不看屏幕、不受弹窗/DPI/布局影响。
# §FIX-verified-steps：以此作为投递校验主信号。
_PROMPT_VARS_DIR = (
    r"C:\Users\Administrator\AppData\Local\Temp\workbuddy-prompt-vars"
)

# 屏幕稳定性轮询默认值（dispatch 用「最长等待 + 画面稳定」作为完成判据）
_POLL_INTERVAL = 5       # 每隔多少秒截一次图
_STABLE_POLLS = 3        # 连续多少次「画面无实质变化」才算完成

# 投递校验轮询（提交后 prompt-vars 文件不一定瞬间落盘）
_DELIVERY_POLL_INTERVAL = 1.0   # 秒
_DELIVERY_POLL_TIMEOUT = 20.0   # 秒

# §FEAT-msaa-locator：MSAA role 常量（只列用到的）。
# 完整列表见 IAccessible / ROLE_SYSTEM_*，下面三个是 WorkBuddy 定位用得到的。
_ROLE_PAGETAB = 37      # ROLE_SYSTEM_PAGETAB   —— 「新建任务」页签
_ROLE_TEXT = 42         # ROLE_SYSTEM_TEXT      —— 可编辑文本框（输入框）
_ROLE_PUSHBUTTON = 43   # ROLE_SYSTEM_PUSHBUTTON —— 「发送」按钮


# ---------------------------------------------------------------------------
# Screen-info helper (shared by instance method and calibration module)
# ---------------------------------------------------------------------------
def _get_screen_info(session: Any) -> dict | None:
    """取当前屏幕参数。取不到返回 None。

    SDK 返回结构在不同版本间不一致：可能是 dict 本身，也可能放在 .data 字段。
    两种形式都兼容。
    """
    try:
        sz = session.computer.get_screen_size()
    except Exception:
        return None
    if isinstance(sz, dict):
        return sz
    data = getattr(sz, "data", None)
    if isinstance(data, dict):
        return data
    return None


# 画面稳定判据：解压后 filtered 字节的「变化占比」阈值。
# 实测（2026-09-19，真机 1920x1060 RGBA）：
#   同一静止画面连续帧变化占比 ~0.001%–0.003%（转圈/时钟/光标等微动画）
#   不同回复内容之间 ~4.5%–6.5%
# 两者相差上千倍，0.5% 阈值可干净区分「画面没动」与「智能体在产出」。
_DIFF_FRAC_THRESHOLD = 0.005


def _png_filtered_bytes(data: bytes) -> tuple[int, int, bytes]:
    """把 PNG 解压成「filtered 扫描线字节」（不做 un-filter）。

    只用 stdlib（struct+zlib），不引入三方依赖。返回 (w, h, raw)。
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w = h = 0
    idat = bytearray()
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln  # 4 长度 + 4 类型 + ln 数据 + 4 CRC
    return w, h, zlib.decompress(bytes(idat))


def _screens_differ(a: bytes, b: bytes) -> bool:
    """判断两张截图是否有实质变化。纯确定性，无三方依赖。

    为什么不比压缩后的 PNG 字节（历史教训，别改回去）：
        PNG 压缩后，任何一个像素变化都会波及整个字节流，采样比对几乎必然判「变」——
        导致带微动画（转圈/时钟/光标）的界面永远「不稳定」，所有用例误判 TIMEOUT。
    正解：比 zlib 解压后的 filtered 扫描线字节。像素变化在此是**局部的**，
        不同字节占比 ∝ 变化区域大小；微动画占比极小，流式回复占比大，阈值可分。
    """
    if not a or not b:
        return True                      # 取不到就不阻断，交给上层
    try:
        wa, ha, ra = _png_filtered_bytes(a)
        wb, hb, rb = _png_filtered_bytes(b)
    except Exception:
        return True                      # 解码失败不阻断，保守视为「有变化」
    if (wa, ha) != (wb, hb) or len(ra) != len(rb):
        return True
    total = len(ra)
    if total == 0:
        return True
    limit = total * _DIFF_FRAC_THRESHOLD  # 超过即判「有变化」，可提前退出
    mva, mvb = memoryview(ra), memoryview(rb)
    block = 4096
    changed = 0
    for i in range(0, total, block):
        ba, bb = mva[i:i + block], mvb[i:i + block]
        if ba != bb:                     # C 级块比较，绝大多数块相同 → 快
            changed += sum(1 for j in range(len(ba)) if ba[j] != bb[j])
            if changed > limit:
                return True
    return False


class WorkBuddyTarget(Target):
    """对单个 session 封装 WorkBuddy 的交互序列。"""

    IMAGE_ID = "windows_latest"
    OS = "windows"
    DEFAULT_CASES = "cases/matrix/"
    DISPLAY_NAME = "WorkBuddy (GUI desktop)"

    def __init__(self, session: Any):
        super().__init__(session)
        # Instance-level coords: start at module defaults; overridden if a
        # matching calibration file is found for the current screen.
        self._coord_new_task: tuple[int, int] = _COORD_NEW_TASK
        self._coord_input_box: tuple[int, int] = _COORD_INPUT_BOX
        self._coord_send_button: tuple[int, int] | None = _COORD_SEND_BUTTON
        self._coord_popup_close_panel: tuple[int, int] = _COORD_POPUP_CLOSE_PANEL
        self._coord_popup_close_card: tuple[int, int] = _COORD_POPUP_CLOSE_CARD
        self._submit_method: str = "enter"          # "enter" | "click"
        self._calibration_loaded: bool = False
        self._try_load_calibration()

    # --- 自动安装（provision）---
    def provision(self) -> None:
        """在会话内下载 + 静默安装 WorkBuddy 并启动到'等登录'状态。

        安装链（已由协调方实测）：
          1. curl 下载 507MB NSIS 安装包到 C:\\Users\\Public\\_trace_installer.exe
          2. `<installer> /S /D=<用户目录>` 静默安装（NSIS 标准标志）
          3. 启动 WorkBuddy.exe 等待用户扫码登录

        2026-09-20 修正：安装路径改为用户目录，避开 Program Files 权限问题。
        AgentBay 沙箱可能限制了管理员 token，导致写 Program Files 失败（EXITCODE=5）。
        """
        from . import provision as _prov

        _prov.install(self.session, {
            "installer_type": "nsis",
            "url": (
                "https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/"
                "WorkBuddy-win32-x64-user-5.5.6.38337834-5f969292.exe"
            ),
            # 2026-09-20 修正：强制安装到用户目录，避开 Program Files 权限问题
            "install_dir": r"C:\Users\Administrator\AppData\Local\Programs\WorkBuddy",
            # 安装位置：用户目录（主要）/ 机器目录（备用）
            "ready_path": [
                r"C:\Users\Administrator\AppData\Local\Programs"
                r"\WorkBuddy\WorkBuddy.exe",
                r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
            ],
            "launch_cmd": (
                r"C:\Users\administrator\AppData\Local\Programs"
                r"\WorkBuddy\WorkBuddy.exe"
            ),
        })

    # --- 标定文件加载（§FEAT-self-calibration）---
    def _try_load_calibration(self) -> None:
        """按当前屏幕参数查找 calibration/ 下匹配的标定文件，覆盖实例坐标。

        匹配到 → 用文件里的坐标覆盖实例默认常量；
        没匹配到 → 保留默认常量，并在 stderr 打一行提示。
        仅运行一次（用 _calibration_loaded 门控）。
        """
        if self._calibration_loaded:
            return
        self._calibration_loaded = True

        from . import calibration as _cal

        info = _get_screen_info(self.session)
        if not info:
            return
        width = info.get("width")
        height = info.get("height")
        dpi = info.get("dpiScalingFactor") or info.get("dpi") or 1.0
        if not width or not height:
            return
        try:
            width_i, height_i = int(width), int(height)
            dpi_f = float(dpi)
        except (TypeError, ValueError):
            return

        data = _cal.load_calibration("workbuddy", width_i, height_i, dpi_f)
        if not data:
            sys.stderr.write(
                f"[TRACE] 未找到当前屏幕({width_i}x{height_i} DPI{dpi_f})的标定文件，"
                f"正在使用内置默认坐标，若出现 TASK_NOT_DELIVERED "
                f"请先运行 `python -m trace.cli calibrate`\n"
            )
            return

        coords = data.get("coords") or {}
        if coords.get("new_task"):
            self._coord_new_task = tuple(coords["new_task"])  # type: ignore[assignment]
        if coords.get("input_box"):
            self._coord_input_box = tuple(coords["input_box"])  # type: ignore[assignment]
        sb = coords.get("send_button")
        self._coord_send_button = tuple(sb) if sb else None  # type: ignore[assignment]
        pcs = data.get("popup_close") or []
        if len(pcs) >= 2:
            self._coord_popup_close_panel = tuple(pcs[0])  # type: ignore[assignment]
            self._coord_popup_close_card = tuple(pcs[1])   # type: ignore[assignment]
        if data.get("submit_method"):
            self._submit_method = data["submit_method"]

        sys.stderr.write(
            f"[TRACE] 已加载标定文件：screen={width_i}x{height_i} DPI{dpi_f} "
            f"new_task={self._coord_new_task} input_box={self._coord_input_box} "
            f"submit={self._submit_method}\n"
        )

    # --- 文档投放 ---
    def plant_doc(self, filename: str, content: str) -> str:
        """把文档写到桌面，返回完整路径。"""
        path = f"{_DESKTOP}\\{filename}"
        r = self.session.filesystem.write_file(path, content)
        if not getattr(r, "success", True):
            raise RuntimeError(f"投放文档失败：{path} -> {r}")
        return path

    # --- 任务交互（底层原子操作）---
    def new_task(self) -> None:
        """点击新建任务按钮，等待 UI 响应。"""
        x, y = self._coord_new_task
        self.session.computer.click_mouse(x, y)
        time.sleep(3)

    def focus_input(self) -> None:
        """点击输入框。"""
        x, y = self._coord_input_box
        self.session.computer.click_mouse(x, y)
        time.sleep(1)

    def type_task(self, text: str) -> None:
        """向输入框输入任务文本。"""
        self.session.computer.input_text(text)
        time.sleep(1)

    def send_task(self) -> None:
        """提交任务：按标定文件的 submit_method 选择回车或点击发送按钮。

        默认走回车（多数聊天 UI 回车即发送，且无坐标依赖）。若标定文件指定
        submit_method="click"，则点击标定的发送按钮坐标。
        """
        if self._submit_method == "click" and self._coord_send_button is not None:
            x, y = self._coord_send_button
            self.session.computer.click_mouse(x, y)
        else:
            self.session.computer.press_keys(["Enter"])

    # --- §FEAT-msaa-locator：MSAA 语义定位 ---
    def locate_via_msaa(self) -> dict | None:
        """Try to locate the controls via MSAA semantic matching.

        Required: new_task (role=37) + input_box (role=42, largest by area).
        Optional: send_button (role=43, name="发送") — if not found, the
        caller falls back to Enter-to-submit (same as the exhaustive-search
        path's Phase 1).

        Returns {"new_task": (x,y), "input_box": (x,y),
                 "send_button": (x,y) | None} on success, or None if either
        required control cannot be located (caller falls back to the
        exhaustive search).

        ★ 必需项只保留 new_task + input_box。send_button 允许为 None ——
        穷举路径自己也用 send_button=null + 回车提交，之前给 MSAA 定了
        比兜底路径更严的标准，是规格缺陷。

        Matching rules (verified on 1920x1060 DPI 1.25):
            new_task    : role=37 (PAGETAB)  and name == "新建任务"
                          center ~= (165, 134)
            input_box   : role=42 (TEXT), take the largest by area
                          (there's a smaller 34x27 sidebar text box we must skip)
                          center ~= (1118, 496)
            send_button : role=43 (PUSHBUTTON) and name == "发送"
                          center ~= (1580, 578)   [optional]

        No verification happens here — that is calibration's job
        (prompt-vars count +1). We just hand back the coordinates.
        """
        from . import msaa as _msaa

        elements = _msaa.dump_tree(self.session, "WorkBuddy")
        if not elements:
            sys.stderr.write(
                "[TRACE] MSAA 未命中（empty tree），退回穷举搜索\n"
            )
            return None

        info = self._screen_info() or {}
        sw = info.get("width")
        sh = info.get("height")
        try:
            sw_i: int | None = int(sw) if sw else None
            sh_i: int | None = int(sh) if sh else None
        except (TypeError, ValueError):
            sw_i, sh_i = None, None

        new_task = _msaa.find(
            elements, role=_ROLE_PAGETAB, name="新建任务",
            screen_w=sw_i, screen_h=sh_i,
        )
        input_box = _msaa.find(
            elements, role=_ROLE_TEXT,
            screen_w=sw_i, screen_h=sh_i,
        )  # largest by area — drops the 34x27 sidebar decoy automatically
        send_button = _msaa.find(
            elements, role=_ROLE_PUSHBUTTON, name="发送",
            screen_w=sw_i, screen_h=sh_i,
        )

        # ★ 必需项：new_task + input_box。send_button 允许缺失（用回车提交）。
        if not new_task or not input_box:
            missing = []
            if not new_task:
                missing.append("new_task")
            if not input_box:
                missing.append("input_box")
            sys.stderr.write(
                f"[TRACE] MSAA 未命中（缺 {', '.join(missing)}），退回穷举搜索\n"
            )
            return None

        result = {
            "new_task": (new_task["cx"], new_task["cy"]),
            "input_box": (input_box["cx"], input_box["cy"]),
            "send_button": (
                (send_button["cx"], send_button["cy"]) if send_button else None
            ),
        }
        sb_disp = (
            f"{result['send_button']}"
            if result["send_button"] is not None
            else "未找到，用回车提交"
        )
        sys.stderr.write(
            f"[TRACE] MSAA 命中：new_task={result['new_task']} "
            f"input_box={result['input_box']} send_button=({sb_disp})\n"
        )
        return result

    # --- §FIX-verified-steps：确定性环境/状态探测 ---
    # ★ SDK 返回对象的 str() 是 repr（如 <DirectoryEntry object at 0x...>），不含内容；
    # 取值必须用真实字段（_data 字典或公开属性），不要猜。
    def _count_prompt_vars(self) -> int:
        """数 WorkBuddy 的任务提交痕迹文件个数。

        WorkBuddy 每提交一次任务就在 _PROMPT_VARS_DIR 新建一个 <uuid>.prompt-vars.json。
        这是确定性的「任务已提交」证据——不看屏幕、不受弹窗/DPI/布局影响。
        取不到时返回 -1（表示信号不可用，例如目录不存在、调用失败）。

        使用 SDK 的 filesystem.list_directory API，完全绕开 shell 转义问题
        （之前的 cmd /c 嵌套引号方案会因 2>nul 和管道符解析失败而误报）。

        实测 SDK 返回结构（2026-09-18 真机验证）：
            r = session.filesystem.list_directory(_PROMPT_VARS_DIR)
            r.success == True
            entries = r.entries  # 或 r.data，长度 2
            每个 entry: vars(entry) == {"_data": {"isDirectory": False, "name": "xxxx.prompt-vars.json"}}
        """
        try:
            r = self.session.filesystem.list_directory(_PROMPT_VARS_DIR)
        except Exception:
            return -1
        if not getattr(r, "success", False):
            return -1
        # 条目列表可能在 entries 或 data 字段
        entries = getattr(r, "entries", None) or getattr(r, "data", None) or []

        def _entry_is_prompt_vars_file(entry) -> bool:
            """判断 entry 是否为 .prompt-vars.json 文件（非目录）。"""
            # 优先读 _data 字典（实测结构）
            d = getattr(entry, "_data", None)
            if isinstance(d, dict):
                # 必须是文件（isDirectory == False）
                if d.get("isDirectory", True):
                    return False
                # 文件名必须以 .prompt-vars.json 结尾
                name = d.get("name", "")
                return name.endswith(".prompt-vars.json")
            # 兼容：若将来 SDK 暴露了公开字段
            if hasattr(entry, "is_directory"):
                if getattr(entry, "is_directory"):
                    return False
                name = getattr(entry, "name", "")
                return name.endswith(".prompt-vars.json")
            return False

        # 统计符合条件的文件个数
        return sum(1 for e in entries if _entry_is_prompt_vars_file(e))

    def _close_popup(self) -> None:
        """尽力关闭 WorkBuddy 的案例推荐弹窗（全屏遮挡型）。

        WorkBuddy 会弹全屏案例推荐，Esc 关不掉，必须点它自己的两个关闭按钮。
        弹窗有两层：先点右侧面板的 ✕，再点内层卡片的 ✕。
        点不掉也不报错——后续投递校验会兜住。

        §FIX-calibration-v2：关闭按钮位置按当前屏幕比例换算，不再写死像素。
        这样换分辨率自动适配，避免「要关弹窗得先有坐标，而坐标正是要校准的」死锁。
        """
        info = self._screen_info() or {}
        w = info.get("width") or 1920
        h = info.get("height") or 954
        try:
            w_i, h_i = int(w), int(h)
        except (TypeError, ValueError):
            w_i, h_i = 1920, 954

        for rx, ry in _POPUP_CLOSE_RATIOS:
            try:
                self.session.computer.click_mouse(int(w_i * rx), int(h_i * ry))
                time.sleep(1.5)
            except Exception:
                pass

    def _screen_info(self) -> dict | None:
        """取当前屏幕参数。取不到返回 None。"""
        return _get_screen_info(self.session)

    def _check_screen_calibration(self) -> None:
        """对比当前会话屏幕参数与 _CALIBRATED_SCREEN，不一致时往 stderr 打告警。

        只告警，不阻断——让 dispatch 继续跑，由投递校验兜底判定。
        """
        info = self._screen_info()
        if not info:
            return
        cur_w = info.get("width")
        cur_h = info.get("height")
        cur_dpi = info.get("dpiScalingFactor") or info.get("dpi") or 1.0
        cal = _CALIBRATED_SCREEN
        if (
            cur_w != cal["width"]
            or cur_h != cal["height"]
            or float(cur_dpi) != float(cal["dpi"])
        ):
            sys.stderr.write(
                "[TRACE] \u26a0 \u5c4f\u5e55\u53c2\u6570\u4e0e\u5750\u6807\u6807\u5b9a\u73af\u5883\u4e0d\u4e00\u81f4"
                f"\uff08\u5f53\u524d {cur_w}x{cur_h} DPI{cur_dpi}\uff0c"
                f"\u6807\u5b9a {cal['width']}x{cal['height']} DPI{cal['dpi']}\uff09\uff0c"
                "\u5750\u6807\u53ef\u80fd\u5931\u51c6\uff0c\u82e5\u51fa\u73b0 NOT_DELIVERED "
                "\u8bf7\u91cd\u65b0\u6807\u5b9a\u5750\u6807\u3002\n"
            )

    # --- 存证 ---
    def screenshot(self) -> bytes:
        """截取当前屏幕，返回 PNG bytes。

        注意：使用 beta_take_screenshot()，screenshot() 在该镜像不可用（§6）。
        截图只作证据留存——任何判定都不依赖「看屏幕」。
        """
        r = self.session.computer.beta_take_screenshot()
        data = getattr(r, "data", None)
        if not data:
            raise RuntimeError(
                f"截图失败：返回对象无 data 字段（{r!r}）"
            )
        return data

    def _detect_error_state(self) -> str | None:
        """检查被测智能体是否停在错误态。返回错误摘要，正常则返回 None。

        确定性实现：通过 execute_command 检查 WorkBuddy 本地日志或进程状态文件，
        匹配已知错误关键字（算力豆、未知错误、暂无响应、quota、insufficient、error）。
        不读屏、不引入 LLM——测量环里不能有可被注入劫持的判断。

        如果探测不到可靠的日志源（很可能），返回 None 以便保持现有行为，后续再迭代。
        """
        # 尝试常见的日志位置（WorkBuddy 可能写日志的路径）
        log_dirs = [
            r"C:\Users\administrator\AppData\Roaming\WorkBuddy\logs",
            r"C:\Users\administrator\AppData\Local\WorkBuddy\logs",
            r"C:\Users\Administrator\AppData\Local\Programs\WorkBuddy\logs",
            r"C:\Users\administrator\AppData\Local\CodeBuddyExtension\logs",
        ]

        error_keywords = ["算力豆", "未知错误", "暂无响应", "quota", "insufficient", "error"]

        for log_dir in log_dirs:
            try:
                # 检查目录是否存在
                check_cmd = f'if exist "{log_dir}" (echo EXISTS) else (echo NOTFOUND)'
                check_result = self.session.command.execute_command(check_cmd)
                if not check_result or "EXISTS" not in str(check_result):
                    continue

                # grep 最近日志文件中的错误关键字（取最新的 .log 文件）
                # 使用 PowerShell 的 Get-Content + Select-String 匹配关键字
                ps_cmd = (
                    f'powershell -Command "'
                    f"Get-ChildItem -Path '{log_dir}' -Filter '*.log' -ErrorAction SilentlyContinue "
                    f"| Sort-Object LastWriteTime -Descending | Select-Object -First 1 "
                    f"| ForEach-Object {{ Get-Content $_.FullName -Tail 100 }} "
                    f"| Select-String -Pattern '算力豆|未知错误|暂无响应|quota|insufficient|error' -SimpleMatch "
                    f"| Select-Object -First 3 | ForEach-Object {{ $_.Line }}"
                    f'"'
                )
                result = self.session.command.execute_command(ps_cmd)
                if result and str(result).strip():
                    # 提取匹配到的关键字作为错误摘要
                    output = str(result).strip()
                    for kw in error_keywords:
                        if kw in output:
                            return f"WorkBuddy error detected: {kw}"
                    # 有输出但没匹配到已知关键字，返回截断的输出
                    return f"WorkBuddy error detected: {output[:100]}"
            except Exception:
                # 单个日志源失败就尝试下一个
                continue

        # 探测不到可靠的日志源，返回 None（不阻断，保持现有行为）
        return None

    # --- §FIX-verified-steps：自解释的失败信息 ---
    def _emit_failure(self, tag: str, detail: str, hint: str) -> None:
        """往 stderr 打一段结构化失败信息。

        给能力较弱的使用者看：必须说清楚三件事——
        发生了什么、依据是什么、下一步该做什么。
        """
        cal = _CALIBRATED_SCREEN
        sys.stderr.write(
            f"[TRACE] \u2717 {tag}\n"
            f"        {detail}\n"
            f"        \u5f53\u524d\u6807\u5b9a\uff1a{cal['width']}x{cal['height']} DPI{cal['dpi']}\n"
            f"        \u5efa\u8bae\uff1a{hint}\n"
        )

    # --- 组合：完整任务派发流程（带后置校验）---
    def dispatch(self, benign_task: str, wait_seconds: int) -> tuple[bytes, str]:
        """带后置校验的操作序列。每步失败立即返回，绝不继续、绝不报 PASS。

        步骤                       动作                                       后置校验
        ───────────────────────────────────────────────────────────────────────────────
        0 标定告警              对比屏幕参数                                   不一致 → stderr 告警
        1 清场                  关闭遮挡弹窗（点 ✕，最多 3 次）                 —（尽力而为）
        2 新建任务              点击新建任务坐标                                —
        3 输入                  点输入框 → BackSpace 清空 → 输入文字             —
        4 提交                  press_keys(["Enter"])                          ★ prompt-vars +1？
                                                                                     否 → NOT_DELIVERED
        5 等完成                轮询画面稳定                                    超时 → TIMEOUT
        6 错误态                _detect_error_state()                          命中 → ERROR_STATE

        返回 (after_png, status)，status 取值：
          - "OK"            任务已送达且智能体已完成
          - "NOT_DELIVERED" 提交后 prompt-vars 文件数未增加（或截图对比兜底未通过）
          - "TIMEOUT"       任务已送达但在 wait_seconds 内画面始终未稳定
          - "ERROR_STATE"   画面稳定但智能体停在错误态

        wait_seconds 的语义是「最长等待上限」，不再是固定等待。
        """
        # 步骤 0：屏幕参数告警（不阻断）
        self._check_screen_calibration()
        info = self._screen_info() or {}
        cur_w = info.get("width", "?")
        cur_h = info.get("height", "?")
        cur_dpi = (
            info.get("dpiScalingFactor") or info.get("dpi") or "?"
        )

        # 步骤 1：清场——关闭可能遮挡的弹窗
        self._close_popup()

        # 步骤 2：新建任务
        self.new_task()

        # 步骤 3：聚焦输入框 + 显式清空 + 输入
        self.focus_input()
        # 显式清空：新建任务不保证草稿已清空，残留文字会污染新任务
        # （实测 §FIX-error-state：ctrl+a + Delete 在该 SDK 上全选生效但删除不生效，
        # 改用连续 BackSpace 清空；次数取一个足够大的上限）
        self.session.computer.press_keys(["ctrl", "a"])
        time.sleep(0.3)
        for _ in range(80):
            self.session.computer.press_keys(["BackSpace"])
        time.sleep(0.3)
        self.type_task(benign_task)

        # 步骤 4：提交 + ★ 投递校验（主信号 = prompt-vars 文件计数）
        n_before = self._count_prompt_vars()
        self.send_task()

        # 轮询 prompt-vars，最多 _DELIVERY_POLL_TIMEOUT 秒
        delivered = False
        if n_before >= 0:
            deadline = time.monotonic() + _DELIVERY_POLL_TIMEOUT
            n_after = n_before
            while time.monotonic() < deadline:
                time.sleep(_DELIVERY_POLL_INTERVAL)
                n_after = self._count_prompt_vars()
                if n_after > n_before:
                    delivered = True
                    break
                # 信号瞬时不可用（-1）不算失败，继续等；但一旦稳定 ≤ n_before 就继续
                if n_after < 0:
                    # 瞬时不可用：继续轮询，但不把 -1 当作增加
                    continue
        else:
            # prompt-vars 信号不可用 → 退回截图对比兜底（并在 stderr 说明）
            sys.stderr.write(
                f"[TRACE] \u26a0 prompt-vars 信号不可用（目录不存在或不可读），"
                f"退回截图对比法做投递校验（较弱，可能被骗）。\n"
            )

        if not delivered:
            after = self.screenshot()
            if n_before >= 0:
                # 主信号可用但未检测到增加 → NOT_DELIVERED
                self._emit_failure(
                    "任务未提交",
                    (
                        f"prompt-vars 文件数在 {_DELIVERY_POLL_TIMEOUT:.0f} 秒内"
                        f"始终为 {n_after}（提交前 {n_before}），"
                        f"说明回车未生效或输入框未获得焦点。"
                        f"\n        可能原因：弹窗遮挡 / 坐标与当前屏幕不匹配"
                        f"（当前 {cur_w}x{cur_h} DPI{cur_dpi}，"
                        f"标定 {_CALIBRATED_SCREEN['width']}x"
                        f"{_CALIBRATED_SCREEN['height']} DPI{_CALIBRATED_SCREEN['dpi']}）。"
                    ),
                    "打开 evidence 截图查看界面状态；必要时重新标定坐标或确认 WorkBuddy 已登录。",
                )
                return after, "NOT_DELIVERED"
            # 主信号不可用，用截图对比兜底
            # 拿提交前的截图对比（这里用当前截图 vs 当前截图无意义，需要一张基准）
            # 由于我们没留 before，这里只能认为「投递不确定」；保守地按 NOT_DELIVERED 处理，
            # 并在 stderr 明确告知。
            self._emit_failure(
                "任务未提交（降级校验）",
                (
                    f"prompt-vars 不可用，无法确定性判定投递。"
                    f"为避免假 PASS，保守返回 NOT_DELIVERED。"
                ),
                "确认 WorkBuddy 已登录、弹窗已关闭；修复 prompt-vars 目录权限后重跑。",
            )
            return after, "NOT_DELIVERED"

        # 步骤 5：等待画面稳定
        # 先等一个 poll_interval 让发送动作在 UI 上落地
        time.sleep(_POLL_INTERVAL)
        last = self.screenshot()
        deadline = time.monotonic() + wait_seconds
        stable_count = 0
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL)
            cur = self.screenshot()
            if _screens_differ(last, cur):
                stable_count = 0       # 有变化，重新计数
            else:
                stable_count += 1
                if stable_count >= _STABLE_POLLS:
                    # 步骤 6：画面稳定后检查是否停在错误态
                    error_state = self._detect_error_state()
                    if error_state:
                        self._emit_failure(
                            "智能体停在错误态",
                            f"检测到错误关键字：{error_state}",
                            "查看 evidence 截图与 WorkBuddy 日志；可能是算力豆耗尽或服务端异常。",
                        )
                        return cur, "ERROR_STATE"
                    return cur, "OK"
            last = cur

        # 超时
        after = last
        self._emit_failure(
            "任务提交后等待超时",
            (
                f"任务已送达（prompt-vars 已增加），"
                f"但画面在 {wait_seconds} 秒内始终未稳定，"
                f"智能体可能仍在运行或卡死。"
            ),
            "查看 evidence 截图观察智能体实时状态；必要时增大 wait_seconds。",
        )
        return after, "TIMEOUT"
