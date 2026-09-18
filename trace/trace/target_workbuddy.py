"""WorkBuddy 桌面目标驱动：投放文档、新建任务、发送任务、截图存证。

坐标来自 §5 实测：窗口已最大化的前提下的 (x, y)。
API 来自 §4：command / filesystem / computer 三类。
"""
from __future__ import annotations

import time
from typing import Any

from .target import Target

_DESKTOP = "C:\\Users\\administrator\\Desktop"

# §5 实测坐标（窗口已最大化）
_COORD_NEW_TASK = (77, 107)
_COORD_INPUT_BOX = (1075, 455)
_COORD_SEND_BUTTON = (1524, 500)


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
            "ready_path": (
                r"C:\Users\administrator\AppData\Local\Programs"
                r"\WorkBuddy\WorkBuddy.exe"
            ),
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
        """点击发送按钮。"""
        x, y = _COORD_SEND_BUTTON
        self.session.computer.click_mouse(x, y)

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

    # --- 组合：完整任务派发流程 ---
    def dispatch(self, benign_task: str, wait_seconds: int) -> bytes:
        """新建 -> 输入 -> 发送 -> 等待 -> 截图，返回 PNG bytes。"""
        self.new_task()
        self.focus_input()
        self.type_task(benign_task)
        self.send_task()
        time.sleep(wait_seconds)
        return self.screenshot()
