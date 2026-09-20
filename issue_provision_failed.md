# provision 自动安装在 AgentBay 沙箱中始终失败（EXITCODE=5 / 权限不足）

## 问题描述

`python -m trace.cli provision --target workbuddy --session s-xxxx` 在 AgentBay 沙箱中始终失败。NSIS 静默安装 (`/S`) 返回 **EXITCODE=5**（`ERROR_ACCESS_DENIED`），WorkBuddy.exe 未被安装。

## 环境

- 沙箱镜像：默认 `windows_latest`（Windows Server 2022）
- 分辨率：1024x768 / DPI 1.0（创建时的过渡值，最终也未变到 1920x1080）
- 用户：Administrator
- Python 3.12（miniconda）
- wuying-agentbay-sdk 0.22.3

## 复现步骤

1. `python -m trace.cli session create --json` → 创建会话成功
2. `python -m trace.cli provision --target workbuddy --session s-xxxx`
3. 507MB NSIS 安装包下载完成（文件大小校验通过：532,127,544 字节）
4. bat 执行 `"C:\\Users\\Public\\_trace_installer.exe" /S`
5. 安装程序返回 EXITCODE=5
6. `ready_path` 两处均找不到 WorkBuddy.exe
7. provision 报失败

## 已尝试的修复

| 方法 | 结果 |
|---|---|
| `/S` 静默安装 | EXITCODE=5 |
| `/CURRENTUSER /S` | EXITCODE=5 |
| `Start-Process -Verb RunAs`（PowerShell 提权） | `InvalidOperationException: 拒绝访问`（沙箱禁止 UAC 弹窗） |
| `cmd /c` 前台运行 | 超时（5min），安装程序仍在运行但未完成 |

## 根因分析

沙箱内 `EnableLUA=0`（UAC 已禁用），但安装程序仍报 `ERROR_ACCESS_DENIED`。可能原因：

1. **沙箱限制了管理员 token**：虽然注册表 UAC 已关闭，但沙箱的安全策略可能限制了进程的实际权限级别，导致 NSIS 安装程序检测到的 token 不是 full admin token
2. **安装程序需要写 `C:\\Program Files`**：WorkBuddy 的 NSIS 脚本可能硬编码了安装路径到 Program Files，而沙箱策略禁止写入
3. **安装程序不是标准 NSIS**：507MB 的安装包异常大，可能是 Electron self-extracting archive 或带 bootstrapper 的安装器，`/S` 参数不被正确解析

## 连带问题

### 1. `provision.py` 的 flag 误判

`_trace_install.bat` 用 `start "" /B` 后台启动安装程序后**立刻写 `DONE` flag**，不等待安装实际完成。这导致：
- 即使安装还在跑或已失败，flag 已经标记 DONE
- provision 的轮询逻辑认为安装成功，去检查 `ready_path` 发现文件不存在才报错
- 应该改为：前台等安装程序退出 → 检查退出码 → 再打 flag

### 2. `python3` 不可用

沙箱内只有 `python`，没有 `python3`。`cli.py` 文档和 skill 都写了 `python3 -m trace.cli`，实际应该用 `python`。

### 3. `pip install wuying-agentbay-sdk` 因 pydantic-core 损坏而失败

全新 conda 环境里 `pydantic-core` 的 dist-info 损坏（无 RECORD 文件，version=None），导致 pip 无法升级。需要手动删除 site-packages 下的目录后再装。

### 4. 屏幕分辨率始终为 1024x768

`session create` 报告 `stable: true`，但分辨率一直是 1024x768（过渡值）。WorkBuddy 适配器坐标基于 1920x954，在这个分辨率下会完全失准。

## 影响

WorkBuddy 评测完全无法自动化。目前只能手动在云桌面安装 WorkBuddy，无法通过 CI/CD 或批量脚本运行。

## 建议修复方向

1. **provision 改用前台等待**：bat 里去掉 `start /B`，直接运行安装程序，用 `%ERRORLEVEL%` 判断成败
2. **安装路径改为用户目录**：给 NSIS 传 `/D=C:\\Users\\Administrator\\AppData\\Local\\Programs\\WorkBuddy` 参数，避开 Program Files 权限问题
3. **提供备用安装方式**：支持 winget 或 zip 绿色版作为 fallback
4. **修复 pydantic-core 依赖问题**：在 README 或 provision 文档里说明
5. **屏幕稳定检测增强**：`session create` 不应在 1024x768 时就报 stable
