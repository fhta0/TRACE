"""AgentBay SDK 会话提供者：attach 到一个已存在的沙箱会话，判空并清晰报错。"""
from __future__ import annotations

import os
from typing import Any


def get_desktop_url(session: Any) -> str | None:
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


class AgentBayProvider:
    """封装 AgentBay SDK 的会话获取逻辑。"""

    def __init__(self, api_key: str | None = None):
        # 延迟导入 agentbay，避免在没有 SDK 的环境里加载本模块就报错。
        from agentbay import AgentBay  # noqa: WPS433

        key = api_key or os.environ.get("AGENTBAY_API_KEY")
        if not key:
            raise RuntimeError(
                "AGENTBAY_API_KEY 未设置。请通过参数传入或环境变量提供。"
            )
        self._ab = AgentBay(api_key=key)

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
