"""WorkBuddy 桌面目标驱动：投放文档、新建任务、发送任务、截图存证。

坐标来自 §5 实测：窗口已最大化的前提下的 (x, y)。
API 来自 §4：command / filesystem / computer 三类。
"""
from __future__ import annotations

import sys
import time
from typing import Any

from .target import Target

_DESKTOP = "C:\\Users\\administrator\\Desktop"

# 坐标标定环境：换分辨率或 DPI 会失准
_CALIBRATED_SCREEN = {"width": 1920, "height": 1080, "dpi": 1.0}

# §5 实测坐标（窗口已最大化）
_COORD_NEW_TASK = (77, 107)
_COORD_INPUT_BOX = (1075, 455)
# 回车发送失效时的后备坐标（默认路径已改用 press_keys(["Enter"])）
_COORD_SEND_BUTTON = (1524, 500)

# 屏幕稳定性轮询默认值（dispatch 用「最长等待 + 画面稳定」作为完成判据）
_POLL_INTERVAL = 5       # 每隔多少秒截一次图
_STABLE_POLLS = 3        # 连续多少次「画面无实质变化」才算完成


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

    # --- 任务交互 ---
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

    def _check_screen_calibration(self) -> None:
        """对比当前会话屏幕参数与 _CALIBRATED_SCREEN，不一致时往 stderr 打告警。

        只告警，不阻断——让 dispatch 继续跑，由投递校验兜底判定。
        """
        try:
            info = self.session.computer.get_screen_size()
        except Exception:
            # 取不到就不告警，避免阻断主流程
            return
        if not isinstance(info, dict):
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
                "\u5750\u6807\u53ef\u80fd\u5931\u51c6\uff0c\u82e5\u51fa\u73b0 ENVIRONMENT_INVALID "
                "\u8bf7\u91cd\u65b0\u6807\u5b9a\u5750\u6807\u3002\n"
            )

    # --- 存证 ---
    def screenshot(self) -> bytes:
        """截取当前屏幕，返回 PNG bytes。

        注意：使用 beta_take_screenshot()，screenshot() 在该镜像不可用（§6）。
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

    # --- 组合：完整任务派发流程 ---
    def dispatch(self, benign_task: str, wait_seconds: int) -> tuple[bytes, str]:
        """新建 -> 清空输入框 -> 输入 -> 发送 -> 轮询直到画面稳定或超时。

        返回 (after_png, status)，status 取值：
          - "OK"            任务已送达且智能体已完成（画面连续 _STABLE_POLLS 次无实质变化）
          - "NOT_DELIVERED" 发送前后画面几乎无变化，疑似任务未送达
          - "TIMEOUT"       任务已送达但在 wait_seconds 内画面始终未稳定（智能体仍在运行）
          - "ERROR_STATE"   画面稳定但智能体停在错误态（算力耗尽、服务端报错等）

        wait_seconds 的语义是「最长等待上限」，不再是固定等待。
        """
        # 屏幕参数与标定环境不一致时告警（只告警，不阻断）
        self._check_screen_calibration()

        # 发送前截图（before）—— 既用于投递校验，也是稳定性轮询的基准
        before = self.screenshot()

        self.new_task()
        self.focus_input()

        # ① 显式清空输入框：新建任务不保证草稿已清空，残留文字会污染新任务
        # （实测 §FIX-error-state：ctrl+a + Delete 在该 SDK 上全选生效但删除不生效，
        # 改用连续 BackSpace 清空；次数取一个足够大的上限）
        self.session.computer.press_keys(["ctrl", "a"])
        time.sleep(0.3)
        for _ in range(80):
            self.session.computer.press_keys(["BackSpace"])
        time.sleep(0.3)

        self.type_task(benign_task)
        self.send_task()

        # ② 等待一个 poll_interval 让发送动作在 UI 上落地，再开始稳定性判定
        time.sleep(_POLL_INTERVAL)
        last = self.screenshot()

        # 投递校验：发送前后画面几乎无变化 → 任务大概率没送达
        if not _screens_differ(before, last):
            return last, "NOT_DELIVERED"

        # ③ 屏幕稳定性轮询：连续 _STABLE_POLLS 次无实质变化 → 认为智能体已完成
        #    超过 wait_seconds 仍未稳定 → TIMEOUT（智能体还在跑，不能下安全结论）
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
                    # 画面稳定后，检查智能体是否停在错误态（算力耗尽、服务端报错等）
                    # §FIX-error-state：画面稳定 ≠ 任务成功，必须区分"成功完成"和"出错停止"
                    error_state = self._detect_error_state()
                    if error_state:
                        return cur, "ERROR_STATE"
                    return cur, "OK"
            last = cur

        # 超时仍未稳定：智能体还在跑，任何安全结论都没有意义
        return last, "TIMEOUT"
