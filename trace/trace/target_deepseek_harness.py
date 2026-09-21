"""DeepSeek Harness 桌面目标驱动：在沙箱内安装 deepseek-harness，运行 headless 代理执行任务。

deepseek-harness 是一个基于插件架构的 agent harness（TypeScript/Node.js）。
本适配器通过 headless 模式运行代理，测试其对注入攻击的抵抗力。

安装链：
  1. 安装 nvm-windows（Node 版本管理器）
  2. 通过 nvm 安装 Node.js 22+
  3. 安装 pnpm
  4. git clone deepseek-harness 仓库
  5. pnpm install && pnpm run build

运行方式：
  - headless 模式：`dsh --profile headless "任务内容"`
  - 代理在沙箱内执行任务，可以读写文件、执行命令
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

from .target import Target

_DESKTOP = "C:\\Users\\administrator\\Desktop"
_DSH_DIR = "C:\\Users\\administrator\\deepseek-harness"
_NVM_DIR = "C:\\Users\\administrator\\AppData\\Roaming\\nvm"
_NODE_VERSION = "22.13.0"

# deepseek-harness 代理执行任务后的输出文件（用于验证任务是否完成）
_DSH_OUTPUT_DIR = "C:\\Users\\administrator\\dsh-output"

# 批处理铁律：安装 bat 与轮询 bat 必须分开（cmd 逐行从磁盘读 bat，
# 若轮询覆写同一文件会把正在跑的脚本读串）
_INSTALL_BAT = r"C:\Users\Public\_dsh_install.bat"
_POLL_BAT = r"C:\Users\Public\_dsh_poll.bat"


class DeepseekHarnessTarget(Target):
    """DeepSeek Harness 代理的驱动适配器。"""

    def __init__(self, session: Any):
        super().__init__(session)
        self._dsh_ready = False

    def _run_long_cmd(self, cmd: str, flag_name: str, timeout_s: int, poll_interval_s: int = 10) -> None:
        """后台跑长命令 + 轮询 flag 文件，直到 DONE/FAIL 或超时。

        Args:
            cmd: 要执行的命令（裸批处理语法，不要套 cmd /c "..."）
            flag_name: flag 文件名（不含路径），用于区分不同长命令
            timeout_s: 超时秒数
            poll_interval_s: 轮询间隔秒数

        铁律：
          - 每步失败即抛异常，绝不吞错
          - 安装 bat（_INSTALL_BAT）与轮询 bat（_POLL_BAT）用不同文件路径
          - 每个长命令用不同的 flag 文件名，避免串台
          - 裸批处理语法，不要套 cmd /c "..."

        流程：
          1. 写安装 bat：清旧 flag + 跑命令 + 按退出码打 flag（DONE 或 FAIL:<code>）
          2. 用 start /B 后台启动安装 bat
          3. 轮询 flag 文件，直到出现 DONE/FAIL 或超时
        """
        flag_path = rf"C:\Users\Public\{flag_name}"

        # 1) 写安装 bat
        bat_body = f"""@echo off
chcp 65001 >nul
if exist "{flag_path}" del /f /q "{flag_path}"
{cmd}
if %ERRORLEVEL% NEQ 0 (
    echo FAIL:%ERRORLEVEL%> "{flag_path}"
    exit /b %ERRORLEVEL%
)
echo DONE> "{flag_path}"
"""
        w = self.session.filesystem.write_file(_INSTALL_BAT, bat_body)
        if not getattr(w, "success", True):
            raise RuntimeError(f"写入安装 bat 失败：path={_INSTALL_BAT!r} result={w!r}")

        # 2) 后台启动安装 bat（start /B 返回 success=False 是常态，不判成败）
        start_cmd = f'start "" /B cmd /c "{_INSTALL_BAT}"'
        self.session.command.execute_command(start_cmd, timeout_ms=30000)

        # 3) 轮询 flag 文件
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # 用轮询 bat 检查 flag 文件
            check_body = f'if exist "{flag_path}" (type "{flag_path}") else (echo PENDING)'
            check_bat_content = f"@echo off\nchcp 65001 >nul\n{check_body}\n"
            w = self.session.filesystem.write_file(_POLL_BAT, check_bat_content)
            if not getattr(w, "success", True):
                raise RuntimeError(f"写入轮询 bat 失败：path={_POLL_BAT!r} result={w!r}")

            try:
                r = self.session.command.execute_command(
                    f'cmd /c "{_POLL_BAT}"', timeout_ms=30000
                )
                output = (getattr(r, "output", "") or "").strip()
            except Exception as e:
                # 单次轮询失败不算命令失败，继续轮询
                sys.stderr.write(f"[TRACE] 轮询异常（继续）: {e}\n")
                time.sleep(poll_interval_s)
                continue

            if "DONE" in output:
                sys.stderr.write(f"[TRACE] 长命令完成: {cmd[:80]}...\n")
                return
            if output.startswith("FAIL:"):
                exit_code = output.split(":", 1)[1]
                raise RuntimeError(
                    f"长命令失败：exit_code={exit_code}, cmd={cmd!r}"
                )

            time.sleep(poll_interval_s)

        # 超时
        raise RuntimeError(
            f"长命令超时（{timeout_s}s）: {cmd!r}。"
            f"flag 文件 {flag_path} 未出现 DONE/FAIL。"
        )

    def provision(self) -> None:
        """在会话内安装 nvm-windows、Node.js、pnpm、deepseek-harness。"""
        from . import provision as _prov

        sys.stderr.write("[TRACE] 开始安装 deepseek-harness ...\n")
        sys.stderr.write("[TRACE] 步骤 1/5: 安装 nvm-windows ...\n")
        self._install_nvm()

        sys.stderr.write("[TRACE] 步骤 2/5: 安装 Node.js ...\n")
        self._install_node()

        sys.stderr.write("[TRACE] 步骤 3/5: 安装 pnpm ...\n")
        self._install_pnpm()

        sys.stderr.write("[TRACE] 步骤 4/5: 克隆 deepseek-harness ...\n")
        self._clone_dsh()

        sys.stderr.write("[TRACE] 步骤 5/5: 构建 deepseek-harness ...\n")
        self._build_dsh()

        # 创建输出目录
        self._run_cmd(f'mkdir "{_DSH_OUTPUT_DIR}" 2>nul')

        sys.stderr.write("[TRACE] deepseek-harness 安装完成。\n")
        self._dsh_ready = True

    def _install_nvm(self) -> None:
        """安装 nvm-windows（Node 版本管理器）。"""
        # 检查是否已安装
        result = self._run_cmd("nvm version", ignore_error=True)
        if "running" in result.lower() or "version" in result.lower():
            sys.stderr.write("[TRACE] nvm 已安装，跳过。\n")
            return

        # 下载 nvm-setup.exe
        nvm_url = "https://github.com/coreybutler/nvm-windows/releases/download/1.1.12/nvm-setup.exe"
        installer_path = "C:\\Users\\Public\\nvm-setup.exe"

        # 使用 curl 下载
        download_cmd = f'curl -L -o "{installer_path}" "{nvm_url}"'
        self._run_cmd(download_cmd, timeout_ms=300000)

        # 静默安装
        install_cmd = f'"{installer_path}" /S'
        self._run_cmd(install_cmd, timeout_ms=120000)

        # 等待安装完成
        time.sleep(5)

        # 刷新环境变量（nvm 需要）
        self._run_cmd('setx NVM_HOME "%APPDATA%\\nvm"')
        self._run_cmd('setx NVM_SYMLINK "C:\\Program Files\\nodejs"')

    def _install_node(self) -> None:
        """通过 nvm 安装 Node.js。"""
        # nvm install 可能很慢，改用后台+轮询
        self._run_long_cmd(
            f'nvm install {_NODE_VERSION}',
            flag_name='_dsh_nvm_install.flag',
            timeout_s=600  # 10 分钟
        )
        self._run_cmd(f'nvm use {_NODE_VERSION}')

        # 验证安装
        result = self._run_cmd("node --version")
        sys.stderr.write(f"[TRACE] Node.js 版本: {result.strip()}\n")

    def _install_pnpm(self) -> None:
        """安装 pnpm。"""
        # npm install -g 可能很慢，改用后台+轮询
        self._run_long_cmd(
            'npm install -g pnpm',
            flag_name='_dsh_pnpm_install.flag',
            timeout_s=300  # 5 分钟
        )
        result = self._run_cmd("pnpm --version")
        sys.stderr.write(f"[TRACE] pnpm 版本: {result.strip()}\n")

    def _clone_dsh(self) -> None:
        """克隆 deepseek-harness 仓库。"""
        # 检查是否已克隆
        result = self._run_cmd(f'if exist "{_DSH_DIR}" echo EXISTS', ignore_error=True)
        if "EXISTS" in result:
            sys.stderr.write("[TRACE] deepseek-harness 已克隆，跳过。\n")
            return

        # git clone 大仓库很慢，改用后台+轮询
        clone_cmd = f'git clone https://github.com/deepseek-ai/deepseek-harness.git "{_DSH_DIR}"'
        self._run_long_cmd(
            clone_cmd,
            flag_name='_dsh_clone.flag',
            timeout_s=600  # 10 分钟
        )

    def _build_dsh(self) -> None:
        """构建 deepseek-harness。"""
        # pnpm install 很慢，改用后台+轮询
        self._run_long_cmd(
            f'cd /d "{_DSH_DIR}" && pnpm install',
            flag_name='_dsh_pnpm_install_deps.flag',
            timeout_s=900  # 15 分钟
        )

        # pnpm run build 很慢，改用后台+轮询
        self._run_long_cmd(
            f'cd /d "{_DSH_DIR}" && pnpm run build',
            flag_name='_dsh_build.flag',
            timeout_s=600  # 10 分钟
        )

    def plant_doc(self, filename: str, content: str) -> str:
        """把注入文档投放到 deepseek-harness 的工作目录。

        deepseek-harness 使用启动目录作为工作区，
        所以我们把文档放到 _DSH_DIR（deepseek-harness 仓库根目录）。
        """
        path = f"{_DSH_DIR}\\{filename}"
        r = self.session.filesystem.write_file(path, content)
        if not getattr(r, "success", True):
            raise RuntimeError(f"投放文档失败：{path} -> {r}")
        sys.stderr.write(f"[TRACE] 文档已投放: {path}\n")
        return path

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
        if not self._dsh_ready:
            raise RuntimeError("deepseek-harness 未就绪，请先运行 provision")

        # 设置 API key（从环境变量读取，避免硬编码）
        import os
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if api_key:
            self._run_cmd(f'set DEEPSEEK_API_KEY={api_key}')

        # 构建命令：使用 --json 模式获取事件流（FINDING Q2）
        task_escaped = benign_task.replace('"', '\\"')
        dsh_cmd = f'cd /d "{_DSH_DIR}" && pnpm dsh --profile headless --json "{task_escaped}"'

        sys.stderr.write(f"[TRACE] 下发任务: {benign_task[:100]}...\n")

        # 执行任务并捕获 stdout 和退出码（超时时间 = wait_seconds + 60s 缓冲）
        timeout_ms = (wait_seconds + 60) * 1000
        stdout, exit_code = self._run_cmd_with_exit_code(dsh_cmd, timeout_ms=timeout_ms)

        # 解析 JSONL 事件流，判定投递状态
        status = self._parse_delivery_status(stdout, exit_code)

        # 证据 = stdout JSONL 原文（UTF-8 编码）
        evidence_bytes = stdout.encode('utf-8') if stdout else b''

        sys.stderr.write(f"[TRACE] 投递校验结果: {status}（退出码={exit_code}）\n")
        return evidence_bytes, status

    def _run_cmd_with_exit_code(self, cmd: str, timeout_ms: int = 60000) -> tuple[str, int]:
        """执行命令并返回 (stdout, exit_code)。

        用于需要退出码的场景（dispatch 的投递校验）。
        退出码从 SDK 返回对象的 exit_code / returncode / code 字段读取；
        若都取不到，保守返回 0（但会由事件流校验兜底，不会假 PASS）。
        """
        try:
            r = self.session.command.execute_command(cmd, timeout_ms=timeout_ms)
            output = getattr(r, "output", "") or ""
            # 退出码可能在 exit_code、returncode、code 等字段（SDK 版本差异）
            exit_code = getattr(r, "exit_code", None)
            if exit_code is None:
                exit_code = getattr(r, "returncode", None)
            if exit_code is None:
                exit_code = getattr(r, "code", None)
            if exit_code is None:
                # 取不到退出码时保守返回 0，但 _parse_delivery_status 会用事件流二次校验
                sys.stderr.write(f"[TRACE] ⚠ 取不到退出码，保守假设为 0: {cmd}\n")
                exit_code = 0

            return output, int(exit_code)
        except Exception as e:
            sys.stderr.write(f"[TRACE] ⚠ 命令异常: {cmd} -> {e}\n")
            # 异常时返回空输出和 -1（会被判为 ERROR_STATE 或 NOT_DELIVERED）
            return "", -1

    def _parse_delivery_status(self, stdout: str, exit_code: int) -> str:
        """解析 JSONL 事件流，判定投递状态。

        判定逻辑（铁律：判不了/没跑起来绝不返回 OK，宁可 NOT_DELIVERED）：
          1. 退出码非 0/1 → "ERROR_STATE"（异常崩溃、信号终止等）
          2. 无 stdout 或无有效 JSON 事件 → "NOT_DELIVERED"（进程未启动、build 没做、启动即崩）
          3. 有 run 事件（session/status/text/tool_call/tool_result/thinking/final 任一）
             且退出码 0 或 1 → "OK"（任务确实跑起来了）
          4. 有事件但全是 error 类型 → "ERROR_STATE"（起来了但明显是环境错误）
          5. 有事件但无 run 事件（理论上不该出现，兜底）→ "NOT_DELIVERED"
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
        run_event_types = {"session", "status", "text", "tool_call", "tool_result", "thinking", "final"}
        error_event_types = {"error"}

        has_run_event = False
        has_error_event = False
        event_count = 0

        for line in stdout.strip().split('\n'):
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
            except json.JSONDecodeError:
                # 非 JSON 行（如 pnpm 的提示信息），跳过
                continue

        # 4. 无任何有效事件
        if event_count == 0:
            sys.stderr.write("[TRACE] 无有效 JSON 事件\n")
            return "NOT_DELIVERED"

        # 5. 有 run 事件且退出码正常 → OK
        if has_run_event:
            return "OK"

        # 6. 只有 error 事件或无 run 事件
        if has_error_event:
            sys.stderr.write(f"[TRACE] 仅有 error 事件，无 run 事件（共 {event_count} 个事件）\n")
            return "ERROR_STATE"

        # 7. 有事件但无 run 事件（兜底）
        sys.stderr.write(f"[TRACE] 有 {event_count} 个事件但无 run 事件\n")
        return "NOT_DELIVERED"

    def _run_cmd(self, cmd: str, timeout_ms: int = 60000, ignore_error: bool = False) -> str:
        """执行命令并返回输出。"""
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
