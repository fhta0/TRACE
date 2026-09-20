"""Target 适配器接口：把'驱动某个桌面智能体'抽象出来，按 case['target'] 选择具体实现。
沙箱固定为 AgentBay（session 对象由 provider 提供）。接入新智能体 = 新增一个 Target 子类并在 get_target 注册。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class Target(ABC):
    """一个被测桌面智能体的驱动适配器。子类实现如何投放注入、如何下发任务。"""

    def __init__(self, session: Any):
        self.session = session

    def provision(self) -> None:
        """在会话内安装并启动被测智能体到'等登录/就绪'状态。
        默认假设已预装（no-op）。需要自动安装的适配器覆盖此方法。
        run_case 不调用此方法，provision 独立于测量环。"""
        return None

    @abstractmethod
    def plant_doc(self, filename: str, content: str) -> str:
        """把注入文档投放到智能体能读到的位置，返回完整路径。"""
        raise NotImplementedError

    @abstractmethod
    def dispatch(self, benign_task: str, wait_seconds: int) -> bytes:
        """向智能体下发一个正常任务，等待其执行，返回一张截图的 PNG bytes。"""
        raise NotImplementedError


def get_target(name: str, session: Any) -> Target:
    """按 case['target'] 选择适配器。懒加载具体实现，避免循环导入。"""
    if name == "workbuddy":
        from .target_workbuddy import WorkBuddyTarget
        return WorkBuddyTarget(session)
    if name == "deepseek-harness":
        from .target_deepseek_harness import DeepseekHarnessTarget
        return DeepseekHarnessTarget(session)
    raise NotImplementedError(f"未知的 target: {name!r}（当前支持: 'workbuddy', 'deepseek-harness'）")
