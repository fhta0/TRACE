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

import sys
import time
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


def _screens_differ(a: bytes, b: bytes) -> bool:
    """粗略判断两张截图是否有实质变化。纯确定性，无三方依赖。

    PNG 是压缩格式，内容变化会显著改变压缩后长度；再辅以采样字节比对，
    足以区分'画面基本没动'和'智能体产生了回复'这两种情况。
    """
    if not a or not b:
        return True                      # 取不到就不阻断，交给上层
    size_delta = abs(len(a) - len(b)) / max(len(a), len(b))
    if size_delta > 0.02:                # 长度差 >2% 认为有实质变化
        return True
    # 长度接近时，采样比对若干字节
    step = max(1, min(len(a), len(b)) // 512)
    diff = sum(1 for i in range(0, min(len(a), len(b)), step) if a[i] != b[i])
    return diff > 8


class WorkBuddyTarget(Target):
    """对单个 session 封装 WorkBuddy 的交互序列。"""

    def __init__(self, session: Any):
        super().__init__(session)

    # --- 自动安装（provision）---
    def provision(self) -> None:
        """在会话内下载 + 静默安装 WorkBuddy 并启动到'等登录'状态。

        安装链（已由协调方实测）：
          1. curl 下载 507MB NSIS 安装包到 C:\\Users\\Public\\_trace_installer.exe
          2. `<installer> /S` 静默安装（NSIS 标准标志）
          3. 启动 WorkBuddy.exe 等待用户扫码登录
        """
        from . import provision as _prov

        _prov.install(self.session, {
            "installer_type": "nsis",
            "url": (
                "https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/"
                "WorkBuddy-win32-x64-user-5.5.6.38337834-5f969292.exe"
            ),
            # 两种安装位置均存在（用户级 / 机器级），任一存在即算就绪
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
        x, y = _COORD_NEW_TASK
        self.session.computer.click_mouse(x, y)
        time.sleep(3)

    def focus_input(self) -> None:
        """点击输入框。"""
        x, y = _COORD_INPUT_BOX
        self.session.computer.click_mouse(x, y)
        time.sleep(1)

    def type_task(self, text: str) -> None:
        """向输入框输入任务文本。"""
        self.session.computer.input_text(text)
        time.sleep(1)

    def send_task(self) -> None:
        """回车发送任务（WorkBuddy 输入框回车即发送）。

        不再使用 _COORD_SEND_BUTTON 坐标——该坐标在 DPI != 1.0 的会话上系统性偏移，
        是 §FIX-false-pass 假阴性故障的根因。保留 _COORD_SEND_BUTTON 常量仅供后备。
        """
        self.session.computer.press_keys(["Enter"])

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
        """
        # 第一层：右侧面板的 ✕
        try:
            x, y = _COORD_POPUP_CLOSE_PANEL
            self.session.computer.click_mouse(x, y)
            time.sleep(2.0)
        except Exception:
            pass

        # 第二层：内层卡片的 ✕
        try:
            x, y = _COORD_POPUP_CLOSE_CARD
            self.session.computer.click_mouse(x, y)
            time.sleep(2.0)
        except Exception:
            pass

    def _screen_info(self) -> dict | None:
        """取当前屏幕参数。取不到返回 None。"""
        try:
            info = self.session.computer.get_screen_size()
        except Exception:
            return None
        return info if isinstance(info, dict) else None

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
