"""通用 provision（自动安装）器：在 AgentBay 会话内按 spec 下载 + 静默安装 + 轮询到就绪。

验证状态（诚实标注）：
  - ``nsis`` 路径已由协调方在 WorkBuddy 上实测（507MB NSIS 包 /S 静默安装）。
  - ``inno`` / ``msi`` / ``zip`` / ``winget`` 走业界标准安装器模式，
    首次实际使用时再验证。

provision 是一次性步骤，不进测量环（run_case 不调用 install()）。
"""
from __future__ import annotations

import time
from typing import Any

# --- 会话内固定路径 ---
# 安装 bat 与轮询 bat 必须分开：后台安装 bat 由 cmd.exe 逐行读取，
# 若轮询期间覆写同一文件，会冲掉正在跑的安装命令导致 flag 永不出现。
_INSTALL_BAT = r"C:\Users\Public\_trace_install.bat"   # 写一次、后台跑，轮询期间不得改动
_POLL_BAT = r"C:\Users\Public\_trace_poll.bat"         # 轮询/查询用，可反复覆写
_FLAG_PATH = r"C:\Users\Public\_trace_provision.flag"
_TMP_DIR = r"C:\Users\Public"

# --- 各类型默认静默参数（业界标准安装器标志） ---
_DEFAULT_SILENT: dict[str, str | None] = {
    "nsis": "/S",
    "inno": "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART",
    "msi": None,      # msi 走 msiexec，默认参数写死在命令模板里
    "zip": None,      # zip 走 PowerShell Expand-Archive
    "winget": "--silent --accept-package-agreements --accept-source-agreements",
}

# --- 各类型安装包扩展名（用于临时文件名） ---
_EXT_BY_TYPE: dict[str, str | None] = {
    "nsis": ".exe",
    "inno": ".exe",
    "msi": ".msi",
    "zip": ".zip",
    "winget": None,
}

_POLL_INTERVAL_S = 20
_DEFAULT_TIMEOUT_S = 1800  # 30 分钟（curl 已带 stall 检测/重试，这里再给极慢但仍在下载的网络留余量）


# ---------------------------------------------------------------------------
# spec 校验
# ---------------------------------------------------------------------------
def _validate_spec(spec: dict) -> None:
    """校验 spec 字段合法性。非法直接抛 ValueError，早失败早好。"""
    itype = spec.get("installer_type")
    if itype not in _EXT_BY_TYPE:
        raise ValueError(
            f"installer_type 非法：{itype!r}，"
            f"允许值：nsis | inno | msi | zip | winget"
        )
    if itype == "winget":
        if not spec.get("winget_id"):
            raise ValueError("winget 类型必须提供 winget_id")
        if spec.get("url"):
            raise ValueError("winget 类型不应提供 url（由 winget 拉取）")
    else:
        if not spec.get("url"):
            raise ValueError(f"{itype} 类型必须提供 url")
    if itype == "zip" and not spec.get("install_dir"):
        raise ValueError("zip 类型必须提供 install_dir")
    if not spec.get("ready_path"):
        raise ValueError("必须提供 ready_path 用于轮询就绪状态")


# ---------------------------------------------------------------------------
# bat 内容构造
# ---------------------------------------------------------------------------
def _tmp_path_for(spec: dict) -> str:
    """根据 installer_type 决定临时安装包路径。winget 不使用，返回空串。"""
    ext = _EXT_BY_TYPE[spec["installer_type"]]
    if ext is None:
        return ""
    return f"{_TMP_DIR}\\_trace_installer{ext}"


def _build_bat_body(spec: dict, tmp_path: str) -> str:
    """按 installer_type 拼接 bat 命令主体（清旧 flag + 下载 + 安装 + 打新 flag）。"""
    itype = spec["installer_type"]
    silent_args = spec.get("silent_args") or _DEFAULT_SILENT.get(itype)
    url = spec.get("url")

    lines: list[str] = []

    # 1) 清旧 flag，避免读到上一轮结果（写在 bat 首行，保证 start 后立刻执行）
    lines.append(f'if exist "{_FLAG_PATH}" del /f /q "{_FLAG_PATH}"')

    if itype == "winget":
        # winget 直接 install，无下载步
        effective = silent_args or _DEFAULT_SILENT["winget"]
        lines.append(f'winget install --id {spec["winget_id"]} {effective}')
    else:
        # 1.5) 清理可能损坏的旧文件（避免 -C - 续传导致问题）
        lines.append(f'if exist "{tmp_path}" del /f /q "{tmp_path}"')

        # 2) 下载（带 stall 检测/重试，移除 -C - 避免损坏文件续传）
        #    --connect-timeout 30          ：30 秒连不上就失败（触发重试）
        #    --speed-limit 10000           ：
        #    --speed-time 30               ：下载速度低于 10KB/s 持续 30 秒即判 stall、中断
        #    --retry 5 --retry-delay 5     ：失败最多重试 5 次，每次隔 5 秒
        #    --retry-all-errors            ：所有错误都重试（不只是瞬态）
        lines.append(
            f'curl -L --retry 5 --retry-delay 5 --retry-all-errors '
            f'--connect-timeout 30 --speed-limit 10000 --speed-time 30 '
            f'-o "{tmp_path}" "{url}"'
        )
        # 3) 安装（按类型分支）
        if itype in ("nsis", "inno"):
            lines.append(f'"{tmp_path}" {silent_args}')
        elif itype == "msi":
            args = silent_args or "/quiet /norestart"
            lines.append(f'msiexec /i "{tmp_path}" {args}')
        elif itype == "zip":
            install_dir = spec["install_dir"]
            lines.append(
                f'powershell -NoProfile -Command '
                f'"Expand-Archive -Force \'{tmp_path}\' \'{install_dir}\'"'
            )
        else:  # pragma: no cover - _validate_spec guards this
            raise ValueError(f"未处理：{itype}")

    # 结尾打 flag
    lines.append(f'echo DONE> "{_FLAG_PATH}"')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 会话内 bat 写/执行/轮询原语
# ---------------------------------------------------------------------------
def _write_bat(session: Any, body: str, bat_path: str = _POLL_BAT) -> None:
    """把 bat 内容（含 @echo off / chcp 65001）写到会话内指定 bat_path。"""
    content = f"@echo off\nchcp 65001 >nul\n{body}\n"
    w = session.filesystem.write_file(bat_path, content)
    if not getattr(w, "success", True):
        raise RuntimeError(f"写入 bat 失败：path={bat_path!r} result={w!r}")


def _run_bat_once(session: Any, body: str, bat_path: str = _POLL_BAT,
                  timeout_ms: int = 30000) -> str:
    """写一个 bat 并同步执行，返回 stdout（已 strip）。用于轻量查询。"""
    _write_bat(session, body, bat_path=bat_path)
    r = session.command.execute_command(
        f'cmd /c "{bat_path}"', timeout_ms=timeout_ms
    )
    if not getattr(r, "success", True):
        raise RuntimeError(
            f"执行 bat 失败：path={bat_path!r} "
            f"success=False, output={getattr(r, 'output', '')!r}"
        )
    return (getattr(r, "output", "") or "").strip()


def _start_bat_background(session: Any) -> None:
    """后台启动 _INSTALL_BAT，立即返回。

    用 ``start /B`` 把 bat 分离到独立 cmd 进程。
    注意：``start /B`` 返回值 ``success=False`` 是常态（§6），
    安装成败由 install() 里的 flag 轮询判定，不依赖此处的返回值。
    """
    cmd = f'start "" /B cmd /c "{_INSTALL_BAT}"'
    session.command.execute_command(cmd, timeout_ms=30000)


def _check_flag(session: Any) -> bool:
    """检查 flag 文件是否已生成。"""
    body = f'if exist "{_FLAG_PATH}" (echo READY) else (echo PENDING)'
    try:
        out = _run_bat_once(session, body)
    except RuntimeError:
        # 单次轮询失败不算安装失败，仅当作未就绪
        return False
    return "READY" in out


def _check_ready_path(session: Any, ready_path: str) -> bool:
    """检查 ready_path 是否存在。"""
    body = f'if exist "{ready_path}" (echo READY) else (echo MISSING)'
    try:
        out = _run_bat_once(session, body)
    except RuntimeError:
        return False
    return "READY" in out


def _launch(session: Any, launch_cmd: str) -> None:
    """启动 launch_cmd。用 ``start ""`` 让 exe 独立进程跑，不阻塞。"""
    cmd = f'start "" "{launch_cmd}"'
    r = session.command.execute_command(cmd, timeout_ms=30000)
    if not getattr(r, "success", True):
        raise RuntimeError(
            f"启动 launch_cmd 失败：{launch_cmd!r} -> {r!r}"
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def install(
    session: Any,
    spec: dict,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    poll_interval_s: int = _POLL_INTERVAL_S,
) -> None:
    """按 spec 在会话内下载 + 静默安装 + 轮询到就绪。

    spec 字段：
      - ``installer_type``: ``"nsis" | "inno" | "msi" | "zip" | "winget"``
      - ``url``:            下载直链（winget 为 None）
      - ``winget_id``:      winget 包 ID（仅 winget 用）
      - ``silent_args``:    可选，覆盖各类型默认静默参数
      - ``install_dir``:    zip 用，解压目标目录
      - ``ready_path``:     判定"装好了"的文件绝对路径（如目标 exe）
      - ``launch_cmd``:     可选，安装后启动 agent 的命令

    流程：
      0. 清理残留进程和损坏文件（避免锁文件）
      1. 写一个 .bat 到会话（首行清旧 flag，中间 curl+静默安装，末尾打新 flag）。
      2. 后台启动该 bat（PowerShell Start-Process）。
      3. Python 侧每 ``poll_interval_s`` 秒轮询 flag 是否出现，最多 ``timeout_s`` 秒。
      4. flag 出现后，再用 ``ready_path`` 二次确认目标文件存在。
      5. 若给了 ``launch_cmd``，启动它。

    验证状态：
      - nsis 路径已由协调方在 WorkBuddy 上实测。
      - inno / msi / zip / winget 走标准安装器模式，首次实际使用时再验证。

    失败/超时抛 ``RuntimeError``，消息含 flag 状态与 ready_path 检查结果。
    """
    _validate_spec(spec)

    tmp_path = _tmp_path_for(spec)

    # 0) 预清理：终止残留进程 + 删除损坏文件
    _pre_cleanup(session, tmp_path)

    body = _build_bat_body(spec, tmp_path)

    # 1) 写安装 bat 到 _INSTALL_BAT（含清旧 flag 行），然后后台启动
    _write_bat(session, body, bat_path=_INSTALL_BAT)

    # 2) 后台启动
    _start_bat_background(session)

    # 3) 轮询 flag
    deadline = time.monotonic() + timeout_s
    last_flag_state = "PENDING"
    while time.monotonic() < deadline:
        time.sleep(poll_interval_s)
        if _check_flag(session):
            last_flag_state = "READY"
            break
    else:
        ready_ok = _check_ready_path(session, spec["ready_path"])
        raise RuntimeError(
            f"provision 超时（{timeout_s}s）。"
            f"flag={last_flag_state} ready_path={spec['ready_path']} "
            f"ready_exists={ready_ok}"
        )

    # 4) 二次确认 ready_path
    ready_ok = _check_ready_path(session, spec["ready_path"])
    if not ready_ok:
        raise RuntimeError(
            f"provision flag 已出现但 ready_path 不存在："
            f"{spec['ready_path']}（安装疑似失败）"
        )

    # 5) 启动 launch_cmd（若提供）
    launch_cmd = spec.get("launch_cmd")
    if launch_cmd:
        _launch(session, launch_cmd)

    return None


def _pre_cleanup(session: Any, tmp_path: str) -> None:
    """预清理：终止残留安装进程 + 删除损坏的安装包文件。

    避免锁文件导致的"另一个程序正在使用此文件"错误。
    """
    if not tmp_path:  # winget 不使用临时文件
        return

    cleanup_script = f"""@echo off
taskkill /f /im WorkBuddy*.exe 2>nul
taskkill /f /im {tmp_path.split('\\')[-1]} 2>nul
if exist "{tmp_path}" del /f /q "{tmp_path}"
if exist "{_FLAG_PATH}" del /f /q "{_FLAG_PATH}"
"""
    _write_bat(session, cleanup_script, bat_path=_POLL_BAT)
    try:
        session.command.execute_command(
            f'cmd /c "{_POLL_BAT}"', timeout_ms=10000
        )
    except Exception:
        # 清理失败不算致命错误，继续执行
        pass
