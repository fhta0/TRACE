"""headless CLI 类被测智能体的家族基类：容纳"后台跑长命令 + 投放文档"这类通用机制。
具体智能体（如 deepseek-harness）继承它，只写自己的 provision/dispatch/投递校验 + 元数据。"""
from __future__ import annotations

import sys
import time
from typing import Any

from .target import Target


class HeadlessCliTarget(Target):
    """headless CLI 智能体的通用机制基类。"""

    WORKDIR = "/root"   # 投放注入文档 / agent 工作目录；子类可覆盖

    # ------------------------------------------------------------------
    # 长命令执行（后台 + 轮询 flag）
    # ------------------------------------------------------------------
    def _run_long_cmd(self, cmd: str, step_name: str, timeout_s: int,
                      poll_interval_s: int = 10,
                      raise_on_fail: bool = True) -> tuple[str, int]:
        """后台跑 Linux 长命令 + 轮询 flag 文件，返回 (stdout, exit_code)。

        Args:
            cmd: bash 命令（裸命令，不要套 bash -c "..."）
            step_name: 步骤标识（仅允许 [A-Za-z0-9_-]），用于生成不同的 script/log/flag 文件名
            timeout_s: 超时秒数
            poll_interval_s: 轮询间隔秒数
            raise_on_fail: 是否在非 0 退出时抛异常。True（默认）= 抛（provision 各步用）；
                          False = 不抛，返回 (stdout, exit_code)（dispatch 用，交给 _parse_delivery_status 判）

        铁律：
          - raise_on_fail=True 时每步失败即抛异常，绝不吞错
          - raise_on_fail=False 时 FAIL 也返回，由调用方判定
          - 每个长命令用不同的 script/log/flag 文件名，避免串台
          - Python 侧轮询前先清 flag（防读到旧 DONE）
          - 后台脚本的 stdout/stderr 重定向到各自 log 文件

        流程：
          1. 写 .sh 脚本：跑命令；ec=$? 拿退出码；成功 echo DONE > flag，失败 echo FAIL:$ec > flag
          2. 用 `setsid bash <script> > <log> 2>&1 < /dev/null &` 后台启动
          3. 轮询 flag 文件，直到出现 DONE/FAIL 或超时
        """
        script_path = f"/tmp/_dsh_{step_name}.sh"
        log_path = f"/tmp/_dsh_{step_name}.log"
        flag_path = f"/tmp/_dsh_{step_name}.flag"

        # 1) 写 .sh 脚本（不用 set -e，否则命令失败时脚本立即退出，ec=$? 拿不到）
        sh_body = (
            "#!/usr/bin/env bash\n"
            f"{cmd}\n"
            "ec=$?\n"
            f'if [ "$ec" -eq 0 ]; then echo DONE > {flag_path}; '
            f"else echo FAIL:$ec > {flag_path}; fi\n"
        )
        w = self.session.filesystem.write_file(script_path, sh_body)
        if not getattr(w, "success", True):
            raise RuntimeError(f"写入脚本失败: path={script_path!r} result={w!r}")

        # 2) Python 侧先清 flag（防止读到旧 DONE）
        self.session.command.execute_command(
            f"rm -f {flag_path}", timeout_ms=10000
        )

        # 3) 后台启动（setsid 创建新 session，< /dev/null 断开 stdin，& 立刻返回）
        launch_cmd = (
            f"setsid bash {script_path} > {log_path} 2>&1 < /dev/null & echo LAUNCHED"
        )
        self.session.command.execute_command(launch_cmd, timeout_ms=30000)

        # 4) 轮询 flag 文件
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                r = self.session.command.execute_command(
                    f"cat {flag_path} 2>/dev/null || echo PENDING",
                    timeout_ms=10000,
                )
                flag_content = (getattr(r, "output", "") or "").strip()
            except Exception as e:
                # 单次轮询失败不算命令失败，继续轮询
                sys.stderr.write(f"[TRACE] 轮询异常（继续）: {e}\n")
                time.sleep(poll_interval_s)
                continue

            if "DONE" in flag_content:
                sys.stderr.write(f"[TRACE] 长命令完成: {step_name}\n")
                # 读 log 文件拿 stdout
                lr = self.session.command.execute_command(
                    f"cat {log_path}", timeout_ms=30000
                )
                stdout = getattr(lr, "output", "") or ""
                return stdout, 0

            if flag_content.startswith("FAIL:"):
                exit_code = int(flag_content.split(":", 1)[1])
                # 读 log 拿 stdout（dispatch 需要交给 _parse_delivery_status 判）
                try:
                    lr = self.session.command.execute_command(
                        f"cat {log_path}", timeout_ms=30000
                    )
                    log_content = getattr(lr, "output", "") or ""
                except Exception:
                    log_content = "(无法读取 log)"

                if raise_on_fail:
                    raise RuntimeError(
                        f"长命令失败: step={step_name}, exit_code={exit_code}, "
                        f"cmd={cmd!r}, log=\n{log_content[:2000]}"
                    )
                else:
                    # 不抛，返回 (stdout, exit_code)，交给调用方判定
                    sys.stderr.write(
                        f"[TRACE] 长命令失败（不抛）: step={step_name}, exit_code={exit_code}\n"
                    )
                    return log_content, exit_code

            time.sleep(poll_interval_s)

        # 超时
        raise RuntimeError(
            f"长命令超时（{timeout_s}s）: step={step_name}, cmd={cmd!r}。"
            f"flag 文件 {flag_path} 未出现 DONE/FAIL。"
        )

    def _run_cmd(self, cmd: str, timeout_ms: int = 60000,
                 ignore_error: bool = False) -> str:
        """执行短命令并返回 stdout。"""
        try:
            r = self.session.command.execute_command(cmd, timeout_ms=timeout_ms)
            output = getattr(r, "output", "") or ""
            success = getattr(r, "success", True)

            if not success and not ignore_error:
                sys.stderr.write(f"[TRACE] ⚠ 命令失败: {cmd}\n")
                sys.stderr.write(f"[TRACE]   输出: {output[:200]}\n")

            return output
        except Exception as e:
            if not ignore_error:
                sys.stderr.write(f"[TRACE] ⚠ 命令异常: {cmd} -> {e}\n")
            return ""

    # ------------------------------------------------------------------
    # plant_doc：投放注入文档
    # ------------------------------------------------------------------
    def plant_doc(self, filename: str, content: str) -> str:
        """把注入文档投放到 agent 工作目录（/root）。"""
        path = f"{self.WORKDIR}/{filename}"
        r = self.session.filesystem.write_file(path, content)
        if not getattr(r, "success", True):
            raise RuntimeError(f"投放文档失败：{path} -> {r}")
        sys.stderr.write(f"[TRACE] 文档已投放: {path}\n")
        return path
