"""Target 适配器接口：把'驱动某个桌面智能体'抽象出来，按 case['target'] 选择具体实现。
沙箱固定为 AgentBay（session 对象由 provider 提供）。接入新智能体 = 新增一个 Target 子类并在 get_target 注册。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class Target(ABC):
    """一个被测桌面智能体的驱动适配器。子类实现如何投放注入、如何下发任务。

    ★ 子类必须声明四个类属性（Target 元数据契约，§1）：
        IMAGE_ID        AgentBay 镜像名（如 "aio-ubuntu-2404"）
        OS              "linux" | "windows"
        DEFAULT_CASES   默认用例目录（相对仓库，如 "cases/dsh-matrix/"）
        DISPLAY_NAME    人读名（如 "DeepSeek Harness (headless CLI)"）
    基类给默认 None；validate_meta() 校验四项都已声明。
    """

    IMAGE_ID: str | None = None
    OS: str | None = None
    DEFAULT_CASES: str | None = None
    DISPLAY_NAME: str | None = None

    @classmethod
    def validate_meta(cls) -> None:
        """校验四项元数据都已由子类声明，任一为 None 即抛 NotImplementedError。"""
        missing = [
            a for a in ("IMAGE_ID", "OS", "DEFAULT_CASES", "DISPLAY_NAME")
            if getattr(cls, a) is None
        ]
        if missing:
            raise NotImplementedError(
                f"{cls.__name__} 未声明必填元数据：{missing}"
            )

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

    def cleanup_doc(self, path: str) -> None:
        """删除本次投放的注入文档（每次 run 结束后调用）。

        为什么需要（§测量卫生）：plant_doc 把注入文档写进 agent 工作区，
        若不清理，**上一个用例/上一次 repeat 的注入文档会残留**，多用例同会话时
        agent 可能对前一个用例的注入动作，污染测量。这里在每次 run 收尾时删掉自己
        投放的文档，保证下一次投放前工作区是干净的。

        清理失败不应让整个 run 失败（证据已采、canary 已判），仅告警。
        默认实现走 filesystem MCP 的 delete_file；子类如落盘方式不同可覆盖。
        """
        import sys
        try:
            self.session.filesystem.delete_file(path)
        except Exception as e:  # noqa: BLE001 — 清理尽力而为，不阻断
            sys.stderr.write(f"[TRACE] ⚠ cleanup_doc 未能删除 {path}：{e}\n")


# ---------------------------------------------------------------------------
# 注册表（懒构造，避免与 target_workbuddy / target_deepseek_harness 循环导入）
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, type["Target"]] | None = None


def _build_registry() -> dict[str, type["Target"]]:
    """延迟构造注册表。子类在函数内 import，避免循环导入。"""
    from .target_workbuddy import WorkBuddyTarget
    from .target_deepseek_harness import DeepseekHarnessTarget

    return {
        "workbuddy": WorkBuddyTarget,
        "deepseek-harness": DeepseekHarnessTarget,
    }


def _registry() -> dict[str, type["Target"]]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def known_targets() -> list[str]:
    """返回已注册 target 名（按字母排序）。"""
    return sorted(_registry())


def _lookup(name: str) -> type["Target"]:
    """按 name 查注册表；未知 name 抛 NotImplementedError，错误信息列出已知 target。"""
    cls = _registry().get(name)
    if cls is None:
        raise NotImplementedError(
            f"未知 target: {name!r}，已知: {known_targets()}"
        )
    return cls


def get_target(name: str, session: Any) -> Target:
    """按 case['target'] 选择适配器。实例化后校验元数据。"""
    cls = _lookup(name)
    cls.validate_meta()
    return cls(session)


def target_meta(name: str) -> type["Target"]:
    """只取类（读元数据用，无需 session）。未知 name 报错，错误信息列出已知 target。"""
    cls = _lookup(name)
    cls.validate_meta()
    return cls
