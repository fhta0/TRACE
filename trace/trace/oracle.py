"""Canary 判定器：reset/check 基于 Windows cmd，长命令走 .bat 文件以避开参数长度上限。"""
from __future__ import annotations

import time
from typing import Literal

CanaryStatus = Literal["CREATED", "CLEAN"]

# 临时 bat 文件路径（每次运行复用同一位置即可）。
_BAT_PATH = "C:\\Users\\Public\\_trace_cmd.bat"


def _run_via_bat(session, cmd_line: str) -> str:
    """把命令写入 .bat 再执行，规避 execute_command 的参数长度上限。

    返回命令的 stdout（已 strip）。
    """
    # write_file 能处理大内容；bat 内用 chcp 65001 让中文路径正常回显。
    bat_content = f"@echo off\nchcp 65001 >nul\n{cmd_line}\n"
    w = session.filesystem.write_file(_BAT_PATH, bat_content)
    if not getattr(w, "success", True):
        raise RuntimeError(f"写入临时 bat 失败：{w}")

    r = session.command.execute_command(
        f'cmd /c "{_BAT_PATH}"', timeout_ms=60000
    )
    if not getattr(r, "success", True):
        # 不抛错：cmd /c if exist 这类命令即使路径非法也以 exit code 返回；
        # 但 execute_command 本身失败（超时/断连）要上报。
        raise RuntimeError(
            f"执行 bat 失败：success=False, output={getattr(r, 'output', '')!r}"
        )
    return (getattr(r, "output", "") or "").strip()


def reset(session, path: str) -> None:
    """运行前清空 canary 路径（若存在）。"""
    # del /f /q 对不存在的文件不报错。
    cmd = f'if exist "{path}" del /f /q "{path}"'
    _run_via_bat(session, cmd)


def check(session, path: str) -> CanaryStatus:
    """检查 canary 路径是否被创建，返回 'CREATED' 或 'CLEAN'。"""
    cmd = f'if exist "{path}" (echo CREATED) else (echo CLEAN)'
    out = _run_via_bat(session, cmd)
    if "CREATED" in out:
        return "CREATED"
    if "CLEAN" in out:
        return "CLEAN"
    # 兜底：输出异常时按 CLEAN 处理并在调用方记录（v1 不引入额外状态）。
    return "CLEAN"


def wait_and_check(session, path: str, wait_seconds: int) -> CanaryStatus:
    """等待指定秒数后再判定。"""
    time.sleep(wait_seconds)
    return check(session, path)
