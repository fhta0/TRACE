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


class DeepseekHarnessTarget(Target):
    """DeepSeek Harness 代理的驱动适配器。"""

    def __init__(self, session: Any):
        super().__init__(session)
        self._dsh_ready = False

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
        # 使用 nvm 安装 Node.js
        self._run_cmd(f'nvm install {_NODE_VERSION}', timeout_ms=300000)
        self._run_cmd(f'nvm use {_NODE_VERSION}')

        # 验证安装
        result = self._run_cmd("node --version")
        sys.stderr.write(f"[TRACE] Node.js 版本: {result.strip()}\n")

    def _install_pnpm(self) -> None:
        """安装 pnpm。"""
        self._run_cmd("npm install -g pnpm", timeout_ms=120000)
        result = self._run_cmd("pnpm --version")
        sys.stderr.write(f"[TRACE] pnpm 版本: {result.strip()}\n")

    def _clone_dsh(self) -> None:
        """克隆 deepseek-harness 仓库。"""
        # 检查是否已克隆
        result = self._run_cmd(f'if exist "{_DSH_DIR}" echo EXISTS', ignore_error=True)
        if "EXISTS" in result:
            sys.stderr.write("[TRACE] deepseek-harness 已克隆，跳过。\n")
            return

        # 克隆仓库
        clone_cmd = f'git clone https://github.com/deepseek-ai/deepseek-harness.git "{_DSH_DIR}"'
        self._run_cmd(clone_cmd, timeout_ms=300000)

    def _build_dsh(self) -> None:
        """构建 deepseek-harness。"""
        # pnpm install
        self._run_cmd(f'cd /d "{_DSH_DIR}" && pnpm install', timeout_ms=600000)

        # pnpm run build
        self._run_cmd(f'cd /d "{_DSH_DIR}" && pnpm run build', timeout_ms=600000)

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

    def dispatch(self, benign_task: str, wait_seconds: int) -> bytes:
        """通过 headless 模式运行 deepseek-harness 代理执行任务。

        命令：dsh --profile headless "任务内容"
        代理会读取工作区文件、执行命令，完成后退出。
        """
        if not self._dsh_ready:
            raise RuntimeError("deepseek-harness 未就绪，请先运行 provision")

        # 设置 API key（从环境变量读取，避免硬编码）
        import os
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if api_key:
            self._run_cmd(f'set DEEPSEEK_API_KEY={api_key}')

        # 构建任务命令
        # 使用 headless 模式，任务内容通过命令行参数传递
        task_escaped = benign_task.replace('"', '\\"')
        dsh_cmd = f'cd /d "{_DSH_DIR}" && pnpm dsh --profile headless "{task_escaped}"'

        sys.stderr.write(f"[TRACE] 下发任务: {benign_task[:100]}...\n")

        # 执行任务（超时时间 = wait_seconds + 60s 缓冲）
        timeout_ms = (wait_seconds + 60) * 1000
        result = self._run_cmd(dsh_cmd, timeout_ms=timeout_ms)

        sys.stderr.write(f"[TRACE] 任务执行完成，等待 {wait_seconds}s 后截图...\n")
        time.sleep(wait_seconds)

        # 截图存证
        shot = self.session.computer.beta_take_screenshot()
        png_bytes = shot.data

        sys.stderr.write(f"[TRACE] 截图完成 ({len(png_bytes)} bytes)\n")
        return png_bytes

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
