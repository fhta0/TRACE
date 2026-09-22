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

from .target_headless_cli import HeadlessCliTarget

_DSH_DIR = "/root"


class DeepseekHarnessTarget(HeadlessCliTarget):
    """DeepSeek Harness 代理的驱动适配器。"""

    IMAGE_ID = "aio-ubuntu-2404"
    OS = "linux"
    DEFAULT_CASES = "cases/dsh-matrix/"
    DISPLAY_NAME = "DeepSeek Harness (headless CLI)"

    def __init__(self, session: Any):
        super().__init__(session)
        self._dsh_ready = False

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
