"""通用 provision（自动安装）器：在 AgentBay 会话内按 spec 下载 + 静默安装 + 轮询到就绪。

验证状态（诚实标注）：
  - ``nsis`` 路径已由协调方在 WorkBuddy 上实测（507MB NSIS 包 /S 静默安装）。
  - ``inno`` / ``msi`` / ``zip`` / ``winget`` 走业界标准安装器模式，
    首次实际使用时再验证。

provision 是一次性步骤，不进测量环（run_case 不调用 install()）。

下载看门狗（2026-09-18 修正）：
  curl 的超时参数（-m / --speed-limit / --speed-time / --retry）在这个环境的卡死状态下
  **全部无效**。真机实测：-m 300 在卡死 42 分钟里没开火，socket 读操作被楔死，
  没有回到 curl 的事件循环，计时器根本没机会被求值。

  **教训：验证一个修复，必须在它要修的那个故障状态下验证，而不是在正常状态下验证。**
  前两次分别加了 --speed-limit/--speed-time/--retry 和 -m 300，都以为解决了，
  但只在健康网络下测试，完全没有证明它们在卡死时会生效。

  已验证有效的解法：Python 侧外部看门狗盯文件大小增长，卡死就杀 curl 用 -C - 续传重启，
  完成判据是文件大小 == Content-Length（确定性信号，不信 curl 退出码）。
"""
from __future__ import annotations

import time
from typing import Any, Union

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

# --- 下载看门狗参数（2026-09-18 修正：不信 curl 超时，只信文件大小增长） ---
_STALL_S = 90        # 连续 90 秒无增长即判定卡死
_POLL_S = 15         # 每 15 秒检查一次文件大小
_DEADLINE_S = 3000   # 总时限 50 分钟（给极慢网络留余量）


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
# 下载看门狗辅助函数（2026-09-18 修正：不信 curl 超时，只信文件大小增长）
# ---------------------------------------------------------------------------
def _get_content_length(session: Any, url: str) -> int:
    """通过 HEAD 请求获取 Content-Length。取不到抛 RuntimeError。"""
    cmd = (
        f'powershell -NoProfile -Command "'
        f"(curl.exe -sIL '{url}' | Select-String -Pattern '^Content-Length' | "
        f'Select-Object -Last 1).ToString()"'
    )
    try:
        out = session.command.execute_command(cmd, timeout_ms=90000)
        hdr = (getattr(out, "output", "") or "").strip()
    except Exception as e:
        raise RuntimeError(f"获取 Content-Length 失败：{e}")

    # 解析最后一个数字 token
    total = 0
    for tok in hdr.replace(":", " ").split():
        if tok.isdigit():
            total = max(total, int(tok))

    if total <= 0:
        raise RuntimeError(
            f"无法获取 Content-Length（响应：{hdr!r}）。"
            f"没有完成判据就不要开始下载，否则又回到'猜'。"
        )
    return total


def _get_file_size(session: Any, path: str) -> int:
    """获取会话内文件大小（字节）。文件不存在返回 0，出错返回 -1。"""
    cmd = (
        f'powershell -NoProfile -Command "'
        f"if (Test-Path '{path}') {{ (Get-Item '{path}').Length }} else {{ 0 }}"
        f'"'
    )
    try:
        out = session.command.execute_command(cmd, timeout_ms=30000)
        return int((getattr(out, "output", "") or "").strip().split()[-1])
    except Exception:
        return -1


def _count_curl_processes(session: Any) -> int:
    """统计会话内 curl 进程数。出错返回 -1。"""
    cmd = (
        'powershell -NoProfile -Command '
        '"(Get-Process curl -ErrorAction SilentlyContinue | Measure-Object).Count"'
    )
    try:
        out = session.command.execute_command(cmd, timeout_ms=30000)
        return int((getattr(out, "output", "") or "").strip().split()[-1])
    except Exception:
        return -1


def _kill_curl(session: Any) -> None:
    """杀掉会话内所有 curl 进程。"""
    cmd = (
        'powershell -NoProfile -Command '
        '"Stop-Process -Name curl -Force -ErrorAction SilentlyContinue"'
    )
    try:
        session.command.execute_command(cmd, timeout_ms=30000)
    except Exception:
        pass  # 杀失败不算致命，下一轮会重试


def _start_download(session: Any, tmp_path: str, url: str) -> None:
    """后台启动 curl 下载（-C - 续传，--connect-timeout 20）。"""
    cmd = f'start "" /B curl -L -C - --connect-timeout 20 -o "{tmp_path}" "{url}"'
    # start /B 的 success=False 是常态，不判成败
    session.command.execute_command(cmd, timeout_ms=30000)


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
    """按 installer_type 拼接 bat 命令主体。

    Args:
        spec: 安装规格
        tmp_path: 临时安装包路径

    bat 只做：清旧 flag + 安装 + 打新 flag。
    下载由 Python 侧看门狗驱动（2026-09-18 修正）。
    """
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


def _check_ready_path(session: Any, ready_path: Union[str, list[str]]) -> bool:
    """检查 ready_path 是否存在。

    ready_path 既接受单个字符串、也接受字符串列表：
    列表中任一存在即算就绪（同一目标多种安装位置时避免假失败）。
    """
    paths = [ready_path] if isinstance(ready_path, str) else list(ready_path)
    if not paths:
        return False
    for p in paths:
        body = f'if exist "{p}" (echo READY) else (echo MISSING)'
        try:
            out = _run_bat_once(session, body)
        except RuntimeError:
            continue
        if "READY" in out:
            return True
    return False


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
      - ``ready_path``:     判定"装好了"的文件绝对路径（str 或 list[str]，任一存在即算就绪）
      - ``launch_cmd``:     可选，安装后启动 agent 的命令

    流程（2026-09-18 修正）：
      0. 清理残留进程和损坏文件
      1. winget 类型：直接写 bat（安装 + 打 flag），后台启动，轮询 flag
      2. 其他类型：
         a. 取 Content-Length（取不到抛异常，没有完成判据就不开始）
         b. Python 侧看门狗驱动下载：监控文件大小，卡死就杀 curl 用 -C - 续传
         c. 下载完成判据：文件大小 == Content-Length（不信 curl 退出码）
         d. 写安装 bat（清 flag + 安装 + 打 flag），后台启动，轮询 flag
      3. flag 出现后，用 ready_path 二次确认
      4. 若给了 launch_cmd，启动它

    验证状态：
      - nsis 路径已由协调方在 WorkBuddy 上实测。
      - inno / msi / zip / winget 走标准安装器模式，首次实际使用时再验证。

    失败/超时抛 ``RuntimeError``，消息含详细诊断信息。
    """
    import sys

    _validate_spec(spec)

    tmp_path = _tmp_path_for(spec)

    # 0) 预清理：终止残留进程 + 删除损坏文件
    _pre_cleanup(session, tmp_path)

    if spec["installer_type"] == "winget":
        # winget 无下载步，直接走 bat 安装流程
        body = _build_bat_body(spec, tmp_path)
        _write_bat(session, body, bat_path=_INSTALL_BAT)
        _start_bat_background(session)
        _poll_flag_and_ready(session, spec, timeout_s, poll_interval_s)
        launch_cmd = spec.get("launch_cmd")
        if launch_cmd:
            _launch(session, launch_cmd)
        return None

    # 非 winget 类型：Python 看门狗驱动下载
    url = spec["url"]
    print(f"[TRACE] 获取 Content-Length ...", file=sys.stderr, flush=True)
    total_size = _get_content_length(session, url)
    print(
        f"[TRACE] 期望总大小 = {total_size:,} 字节 ({total_size / 1048576:.1f} MB)",
        file=sys.stderr, flush=True
    )

    # 删除旧 tmp 文件（避免续传损坏文件）
    if tmp_path:
        body = f'if exist "{tmp_path}" del /f /q "{tmp_path}"'
        _write_bat(session, body, bat_path=_POLL_BAT)
        try:
            session.command.execute_command(f'cmd /c "{_POLL_BAT}"', timeout_ms=10000)
        except Exception:
            pass

    # 杀残留 curl
    _kill_curl(session)
    time.sleep(2)

    # 下载看门狗主循环
    t_start = time.monotonic()
    attempt = 0
    last_size = _get_file_size(session, tmp_path)
    last_change = time.monotonic()
    print(f"[TRACE] 断点位置 = {last_size:,} 字节", file=sys.stderr, flush=True)

    while time.monotonic() - t_start < _DEADLINE_S:
        cur = _get_file_size(session, tmp_path)

        # 完成判据：文件大小 >= Content-Length
        if cur >= total_size:
            print(f"[TRACE] 下载完成 {cur:,} 字节", file=sys.stderr, flush=True)
            break

        # 检查 curl 进程
        curl_count = _count_curl_processes(session)
        if curl_count == 0:
            # curl 不在跑，启动（-C - 续传）
            attempt += 1
            print(
                f"[TRACE] [第 {attempt} 次] 启动 curl（-C - 续传，从 {cur:,} 开始）",
                file=sys.stderr, flush=True
            )
            _start_download(session, tmp_path, url)
            time.sleep(5)
            last_change = time.monotonic()
            last_size = _get_file_size(session, tmp_path)
            continue

        # 检查文件大小增长
        if cur > last_size:
            # 有增长，刷新状态
            rate = (cur - last_size) / max(time.monotonic() - last_change, 1) / 1024
            pct = cur * 100.0 / total_size
            print(
                f"[TRACE] 下载 {cur:,} / {total_size:,} ({pct:.1f}%)  {rate:.0f} KB/s",
                file=sys.stderr, flush=True
            )
            last_size = cur
            last_change = time.monotonic()
        elif time.monotonic() - last_change > _STALL_S:
            # 卡死：连续 STALL_S 秒无增长
            print(
                f"[TRACE] !!! 卡死 {_STALL_S}s（停在 {cur:,}），杀掉 curl 重启续传",
                file=sys.stderr, flush=True
            )
            _kill_curl(session)
            time.sleep(3)
            last_change = time.monotonic()

        time.sleep(_POLL_S)

    # 最终校验
    final_size = _get_file_size(session, tmp_path)
    if final_size < total_size:
        elapsed = time.monotonic() - t_start
        since_last_growth = time.monotonic() - last_change
        raise RuntimeError(
            f"下载未完成：实际 {final_size:,} 字节 / 期望 {total_size:,} 字节。"
            f"已运行 {elapsed:.0f}s，重启 {attempt} 次，"
            f"最后一次增长在 {since_last_growth:.0f}s 前。"
            f"建议：换时间重试 / 检查 CDN 可达性。"
        )

    print(f"[TRACE] 完整性校验通过：{final_size:,} == {total_size:,}", file=sys.stderr, flush=True)

    # 下载完成，写安装 bat 并启动
    body = _build_bat_body(spec, tmp_path)
    _write_bat(session, body, bat_path=_INSTALL_BAT)
    _start_bat_background(session)

    # 轮询 flag + ready_path
    _poll_flag_and_ready(session, spec, timeout_s, poll_interval_s)

    # 启动 launch_cmd（若提供）
    launch_cmd = spec.get("launch_cmd")
    if launch_cmd:
        _launch(session, launch_cmd)

    return None


def _poll_flag_and_ready(
    session: Any,
    spec: dict,
    timeout_s: int,
    poll_interval_s: int,
) -> None:
    """轮询 flag 出现，然后二次确认 ready_path。超时或 ready_path 不存在抛 RuntimeError。"""
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

    # 二次确认 ready_path
    ready_ok = _check_ready_path(session, spec["ready_path"])
    if not ready_ok:
        raise RuntimeError(
            f"provision flag 已出现但 ready_path 不存在："
            f"{spec['ready_path']}（安装疑似失败）"
        )


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
