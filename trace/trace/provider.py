"""AgentBay SDK 会话提供者：attach 到一个已存在的沙箱会话，判空并清晰报错。

同时提供会话生命周期的薄封装（create / delete / list），供 `trace.cli session`
子命令使用。封装的目的不是抽象 SDK，而是把 SDK 的调用细节收敛到一处——弱模型
调用方只该走 CLI，不该直接碰 SDK。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional


# --- 屏幕稳定判据 ---
# 实测：会话启动早期 `get_screen_size` 会返回过渡值（见过 1024x768 DPI1.0），
# 等沙箱真正起来后才会切到真实值（1920x1060 DPI1.25）。判定标准：
#   1. 连续两次读数完全一致（width / height / dpi 三项都等）
#   2. 且 width > 1024（过渡值一般就是 1024）
# 达到上述两点才认为屏幕参数可用。
_SCREEN_STABLE_MIN_WIDTH = 1024
_SCREEN_STABLE_CONSECUTIVE = 2
_SCREEN_STABLE_POLL_INTERVAL = 2.0     # 秒
_SCREEN_STABLE_TIMEOUT = 180.0         # 秒


def get_desktop_url(session: Any) -> Optional[str]:
    """取无影网页桌面地址（可交互，用于观看 / 扫码登录）。

    容错：取不到返回 None，绝不抛错 —— 调用方不能因为拿不到地址就让
    provision / run 失败。
    """
    try:
        info = session.info()
        data = getattr(info, "data", info)
        url = getattr(data, "resource_url", None)
        return url or None
    except Exception:
        return None


def _read_screen(session: Any) -> Optional[dict]:
    """从 session 读取屏幕参数，返回 {width, height, dpi}。失败返回 None。"""
    try:
        sz = session.computer.get_screen_size()
        data = getattr(sz, "data", None)
        if not isinstance(data, dict):
            return None
        w = data.get("width")
        h = data.get("height")
        dpi = data.get("dpiScalingFactor") or data.get("dpi") or 1.0
        if w is None or h is None:
            return None
        try:
            return {"width": int(w), "height": int(h), "dpi": float(dpi)}
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


def wait_for_screen_stable(session: Any, timeout: float = _SCREEN_STABLE_TIMEOUT) -> dict:
    """轮询等屏幕参数稳定，返回稳定后的 {width, height, dpi}。

    超时抛出 RuntimeError。调用方应在超时后给出明确错误（会话可能还在启动中）。
    """
    deadline = time.monotonic() + timeout
    last: Optional[dict] = None
    consecutive = 0
    while time.monotonic() < deadline:
        cur = _read_screen(session)
        if cur is not None and cur["width"] > _SCREEN_STABLE_MIN_WIDTH:
            if last is not None and (
                cur["width"] == last["width"]
                and cur["height"] == last["height"]
                and abs(cur["dpi"] - last["dpi"]) < 1e-6
            ):
                consecutive += 1
                if consecutive >= _SCREEN_STABLE_CONSECUTIVE - 1:
                    return cur
            else:
                consecutive = 0
            last = cur
        else:
            # 读数失败或仍是过渡值，重置
            last = None
            consecutive = 0
        time.sleep(_SCREEN_STABLE_POLL_INTERVAL)
    raise RuntimeError(
        f"屏幕参数在 {timeout:.0f} 秒内未稳定（最后一次读数：{last}）。"
        f"会话可能仍在启动，请稍后重试或检查 image_id。"
    )


class AgentBayProvider:
    """封装 AgentBay SDK 的会话获取 / 创建 / 删除 / 列举逻辑。"""

    def __init__(self, api_key: Optional[str] = None):
        # 延迟导入 agentbay，避免在没有 SDK 的环境里加载本模块就报错。
        from agentbay import AgentBay  # noqa: WPS433

        key = api_key or os.environ.get("AGENTBAY_API_KEY")
        if not key:
            raise RuntimeError(
                "AGENTBAY_API_KEY 未设置。请通过参数传入或环境变量提供。"
            )
        self._ab = AgentBay(api_key=key)

    # ---- 内部工具 ----
    @property
    def client(self) -> Any:
        """暴露底层 AgentBay 客户端，供 `session` 子命令使用。"""
        return self._ab

    # ---- 核心：attach ----
    def get_session(self, session_id: str) -> Any:
        """Attach 到已存在的 session。若会话不存在或返回空，抛出 RuntimeError。"""
        if not session_id:
            raise RuntimeError("session_id 不能为空。")

        result = self._ab.get(session_id)
        # SDK 返回结构：result.session 可能为 None（会话不存在）
        session = getattr(result, "session", None)
        if session is None:
            raise RuntimeError(
                f"会话 {session_id!r} 不存在或已过期（AgentBay 返回 session=None）。"
            )
        return session

    # ---- 生命周期：create ----
    def create_session(
        self,
        image_id: Optional[str] = None,
        labels: Optional[dict] = None,
        manual_release: bool = True,
    ) -> Any:
        """创建新会话。返回 SDK Session 对象。

        默认 `manual_release=True`——长流程不能被空闲回收打断。
        如果调用方确实想要自动回收，需要显式传 `manual_release=False`。
        """
        from agentbay import CreateSessionParams, LifecyclePolicy  # noqa: WPS433

        params = CreateSessionParams(
            image_id=image_id,
            labels=labels,
            lifecycle_policy=LifecyclePolicy(manual_release=manual_release),
        )
        result = self._ab.create(params)
        if not getattr(result, "success", False):
            err = getattr(result, "error_message", "") or "unknown error"
            raise RuntimeError(f"创建会话失败：{err}")
        session = getattr(result, "session", None)
        if session is None:
            raise RuntimeError("创建会话失败：AgentBay 返回 session=None。")
        return session

    # ---- 生命周期：delete ----
    def delete_session(self, session_id: str) -> dict:
        """删除单个会话。返回 {'success': bool, 'error': str|None, 'request_id': str}。

        注意：SDK 的 delete 需要 Session 对象，所以这里先 get 再 delete。
        如果 get 失败（会话已不存在），视为"已删除"，success=True。
        """
        # 先尝试 attach；attach 失败说明会话已经没了，算"已删除"
        try:
            session = self.get_session(session_id)
        except RuntimeError:
            return {"success": True, "error": None, "request_id": "", "already_gone": True}

        try:
            result = session.delete()
        except Exception as e:
            return {"success": False, "error": str(e), "request_id": "", "already_gone": False}

        return {
            "success": bool(getattr(result, "success", False)),
            "error": getattr(result, "error_message", None) or None,
            "request_id": getattr(result, "request_id", "") or "",
            "already_gone": False,
        }

    def verify_gone(self, session_id: str) -> bool:
        """回查会话是否确实不再存在。True 表示已无残留。"""
        try:
            result = self._ab.get(session_id)
        except Exception:
            # get 抛错通常意味着会话不存在或 API 异常；保守视为"可能还在"
            return False
        session = getattr(result, "session", None)
        return session is None

    # ---- 生命周期：list ----
    def list_sessions(
        self,
        status: Optional[str] = None,
        labels: Optional[dict] = None,
        image_id: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """列举会话 ID。

        ⚠️ 已知问题（实测）：ab.list() 在有会话运行时曾返回空列表，结果不可靠。
        调用方必须把 list 结果当参考，绝不能把"list 为空"当成"没在计费"的证据。
        建议优先使用 status="RUNNING" 过滤，实测相对更可信。
        """
        try:
            result = self._ab.list(
                status=status,
                labels=labels,
                image_id=image_id,
                page=page,
                limit=limit,
            )
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "session_ids": [],
                "total_count": 0,
                "request_id": "",
            }
        return {
            "success": bool(getattr(result, "success", False)),
            "error": getattr(result, "error_message", None) or None,
            "session_ids": list(getattr(result, "session_ids", []) or []),
            "total_count": int(getattr(result, "total_count", 0) or 0),
            "request_id": getattr(result, "request_id", "") or "",
        }
