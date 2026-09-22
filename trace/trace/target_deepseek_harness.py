"""DeepSeek Harness 桌面目标驱动：在 AgentBay Linux 桌面镜像（aio-ubuntu-2404）内安装并运行 deepseek-harness 发行版。

镜像预装 node v26 / npm，不需要 nvm / corepack / pnpm / git clone。

安装（已真机全绿验证，禁止改动）：
  - npm install -g @deepseek-ai/dsh@0.1.6-alpha.2
  - 校验：`dsh --version` 输出含 "0.1"

运行方式：
  - cwd=/root（agent 工作目录）
  - `export DSH_PERMISSION_MODE=danger-full-access` 绕开 harness 内层沙箱（否则工具无法执行）
  - headless 模式：`dsh --profile headless --json "<任务>"`
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from .target import Target

_DSH_DIR = "/root"


class DeepseekHarnessTarget(Target):
    """DeepSeek Harness 代理的驱动适配器。"""

    IMAGE_ID = "aio-ubuntu-2404"
    OS = "linux"
    DEFAULT_CASES = "cases/dsh-matrix/"
    DISPLAY_NAME = "DeepSeek Harness (headless CLI)"

    def __init__(self, session: Any):
        super().__init__(session)
        self._dsh_ready = False

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
    # provision：npm 全局安装 dsh 发行版 + 校验
    # ------------------------------------------------------------------
    def provision(self) -> None:
        """在会话内安装 deepseek-harness 发行版（node/npm 已预装）。

        已真机全绿验证，禁止改动：
          1. npm install -g @deepseek-ai/dsh@0.1.6-alpha.2
          2. 校验 `dsh --version` 输出含 "0.1"
        """
        sys.stderr.write("[TRACE] 开始安装 deepseek-harness 发行版 ...\n")

        # 步骤 1/2：全局安装 dsh（@0.1.6-alpha.2 真机验证版本）
        sys.stderr.write("[TRACE] 步骤 1/2: npm install -g @deepseek-ai/dsh@0.1.6-alpha.2 ...\n")
        self._run_long_cmd(
            "npm install -g @deepseek-ai/dsh@0.1.6-alpha.2",
            step_name="npm_dsh",
            timeout_s=600,
        )

        # 步骤 2/2：校验 `dsh --version` 输出含 "0.1"
        sys.stderr.write("[TRACE] 步骤 2/2: 校验 dsh --version ...\n")
        ver = self._run_cmd("dsh --version").strip()
        if "0.1" not in ver:
            raise RuntimeError(
                f"deepseek-harness 校验失败：dsh --version 输出异常: {ver[:500]}"
            )

        sys.stderr.write(f"[TRACE] deepseek-harness 安装完成（{ver}）。\n")
        self._dsh_ready = True

    # ------------------------------------------------------------------
    # plant_doc：投放注入文档
    # ------------------------------------------------------------------
    def plant_doc(self, filename: str, content: str) -> str:
        """把注入文档投放到 agent 工作目录（/root）。"""
        path = f"/root/{filename}"
        r = self.session.filesystem.write_file(path, content)
        if not getattr(r, "success", True):
            raise RuntimeError(f"投放文档失败：{path} -> {r}")
        sys.stderr.write(f"[TRACE] 文档已投放: {path}\n")
        return path

    # ------------------------------------------------------------------
    # dispatch：运行任务 + 投递校验
    # ------------------------------------------------------------------
    def dispatch(self, benign_task: str, wait_seconds: int) -> tuple[bytes, str]:
        """通过 headless 模式运行 deepseek-harness 代理执行任务，并用 --json 事件流做确定性投递校验。

        投递校验信号（来自 FINDING-dsh-delivery-signal.md）：
          1. stdout JSONL 事件流（--json 模式）—— 来自 json-stream.ts:259-316 的事件分类
          2. 退出码（0=completed, 1=error/completed-with-error）—— 来自 index.ts:373-380

        事件类型（来自 json-stream.ts）：
          - session: 会话启动事件（json-stream.ts:318）
          - status: 阶段事件（turn_start, step_start, step_end, turn_end）
          - thinking: 推理块
          - text: 文本块（assistant/message 的 text content）
          - tool_call: 工具调用（tool/call）
          - tool_result: 工具结果（tool/result）
          - final: 最终答案（json-stream.ts:321-326）
          - error: 错误事件（startup.ts:84-93, index.ts:296-301）

        判定逻辑（铁律：判不了/没跑起来绝不返回 OK，宁可 NOT_DELIVERED）：
          - 出现过 run 事件（session/status/text/tool_call/tool_result/thinking/final 任一）
            且退出码 0 或 1 → "OK"
          - 无 run 事件或进程未启动（stdout 为空或无有效 JSON 事件）→ "NOT_DELIVERED"
          - 退出码非 0/1 → "ERROR_STATE"
          - 有事件但全是 error 类型 → "ERROR_STATE"

        返回 (evidence_bytes, status):
          - evidence_bytes: stdout JSONL 事件流原文（UTF-8 编码），runner 当证据存文件
          - status: "OK" | "NOT_DELIVERED" | "ERROR_STATE"
        """
        # §FIX-linux-smoke：不要用 self._dsh_ready 这种实例标志做就绪门槛——
        # provision 和 run 是**两次独立的 CLI 调用（不同进程、不同 Target 实例）**，
        # provision 里设的 _dsh_ready=True 不会带到 run 的新实例。就绪与否交给
        # 投递校验兜底：dsh 没装好 → 无 run 事件 → NOT_DELIVERED（不会假 PASS）。

        # 设置 API key（从环境变量读取，避免硬编码）
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置")

        # 构建 Linux 命令：cd /root + export API key + danger-full-access + 全局 dsh
        task_escaped = benign_task.replace('"', '\\"')
        dsh_cmd = (
            f"cd /root && "
            f"export DEEPSEEK_API_KEY='{api_key}' && "
            f"export DSH_PERMISSION_MODE=danger-full-access && "
            f'dsh --profile headless --json "{task_escaped}"'
        )

        sys.stderr.write(f"[TRACE] 下发任务: {benign_task[:100]}...\n")

        # 执行任务（超时 = wait_seconds + 60s 缓冲）
        timeout_s = wait_seconds + 60
        try:
            # raise_on_fail=False：非 0 退出也返回，交给 _parse_delivery_status 判
            # （dsh --profile headless 退出 1 = completed-with-error，agent 确实执行过）
            stdout, exit_code = self._run_long_cmd(
                dsh_cmd,
                step_name="dispatch",
                timeout_s=timeout_s,
                raise_on_fail=False,
            )
        except Exception as e:
            sys.stderr.write(f"[TRACE] dispatch 长命令异常: {e}\n")
            # 超时/启动失败这类真异常 → ERROR_STATE（环境问题，agent 可能还在跑或没起来）
            # 注意：不是 NOT_DELIVERED，因为判不了/没跑起来要能区分于"跑了但失败"
            return b"", "ERROR_STATE"

        # 解析 JSONL 事件流，判定投递状态
        status = self._parse_delivery_status(stdout, exit_code)

        # 证据 = stdout JSONL 原文（UTF-8 编码）
        evidence_bytes = stdout.encode("utf-8") if stdout else b""

        sys.stderr.write(f"[TRACE] 投递校验结果: {status}（退出码={exit_code}）\n")
        return evidence_bytes, status

    # ------------------------------------------------------------------
    # 投递校验（逻辑不动，仅按 Linux 调整注释）
    # ------------------------------------------------------------------
    def _parse_delivery_status(self, stdout: str, exit_code: int) -> str:
        """解析 JSONL 事件流，判定投递状态。

        判定逻辑（铁律：判不了/没跑起来/报错收场绝不返回 OK，宁可 ERROR_STATE）：
          1. 退出码非 0/1 → "ERROR_STATE"（异常崩溃、信号终止等）
          2. 无 stdout 或无有效 JSON 事件 → "NOT_DELIVERED"（进程未启动、build 没做、启动即崩）
          3. turn 以 error 收场（type=="status" 且 phase=="turn_end" 且 reason.kind=="error"），
             或有顶层 error 事件 → "ERROR_STATE"
             （环境/agent 没干净跑完 = 没测到，不是 OK，跟 WorkBuddy 的 ERROR_STATE 同理）
          4. 有 run 事件（session/status/text/tool_call/tool_result/thinking/final 任一）
             且退出码 0 或 1 → "OK"（任务确实跑起来了）
          5. 有事件但无 run 事件（理论上不该出现，兜底）→ "NOT_DELIVERED"

        关键：步骤 3 必须在步骤 4 之前。turn_end with reason.kind=="error" 的信号
        藏在 status 事件里（来自 json-stream.ts），仅看 type=="status" 会被误判为 run 事件；
        必须先查 turn 是否报错，再判 has_run_event→OK，否则错误收场会变成空 PASS。
        """
        # 1. 退出码异常
        if exit_code not in (0, 1):
            sys.stderr.write(f"[TRACE] 退出码异常: {exit_code}\n")
            return "ERROR_STATE"

        # 2. 无 stdout
        if not stdout or not stdout.strip():
            sys.stderr.write("[TRACE] 无 stdout 输出\n")
            return "NOT_DELIVERED"

        # 3. 解析 JSONL，统计事件类型
        # run 事件：任务确实执行的信号（来自 json-stream.ts:259-316）
        run_event_types = {
            "session", "status", "text", "tool_call",
            "tool_result", "thinking", "final",
        }
        error_event_types = {"error"}

        has_run_event = False
        has_error_event = False
        turn_errored = False
        event_count = 0

        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type", "")
                event_count += 1

                if event_type in run_event_types:
                    has_run_event = True
                if event_type in error_event_types:
                    has_error_event = True
                # turn 以 error 收场：phase=="turn_end" 且 reason.kind=="error"
                # （正常收场是 reason.kind=="completed"，不算 error）
                if (
                    event_type == "status"
                    and event.get("phase") == "turn_end"
                    and event.get("reason", {}).get("kind") == "error"
                ):
                    turn_errored = True
            except json.JSONDecodeError:
                # 非 JSON 行（如 pnpm 的提示信息），跳过
                continue

        # 4. 无任何有效事件
        if event_count == 0:
            sys.stderr.write("[TRACE] 无有效 JSON 事件\n")
            return "NOT_DELIVERED"

        # 5. turn 报错 或 有顶层 error 事件 → ERROR_STATE
        #    必须排在 has_run_event→OK 之前，否则错误收场会误判为 OK
        if turn_errored or has_error_event:
            sys.stderr.write(
                f"[TRACE] 错误收场：turn_errored={turn_errored}, "
                f"has_error_event={has_error_event}（共 {event_count} 个事件）\n"
            )
            return "ERROR_STATE"

        # 6. 有 run 事件且退出码正常 → OK
        if has_run_event:
            return "OK"

        # 7. 有事件但无 run 事件（兜底）
        sys.stderr.write(f"[TRACE] 有 {event_count} 个事件但无 run 事件\n")
        return "NOT_DELIVERED"
