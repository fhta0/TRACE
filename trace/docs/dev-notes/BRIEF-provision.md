# 任务：Target 的 provision（自动安装）能力

目标：让"声明一个下载链接 + 安装方式 → 在会话内自动装好并启动到'等登录'状态"对多数桌面 agent 开箱即用。
沙箱固定 AgentBay。**provision 是一次性步骤，绝不进测量环（run_case）**——它慢、且发生在注入 payload 之前。

## 背景（协调方今天实测过的安装链，照此实现）

WorkBuddy 的安装今天是全自动、无 Agent 完成的：
1. `curl -L -o <tmp.exe> <CDN直链>` 下载（507MB，约数分钟）
2. `<tmp.exe> /S` 静默安装（NSIS 标志，无弹窗）
3. 装完自动启动，程序目录出现 `WorkBuddy.exe`
坑：单条 `execute_command` 有时长上限，下载+安装会超时 → 必须**后台跑 + 轮询 flag 文件**（今天用 `.bat` + `start` 分离进程 + 轮询 `DONE` 标志验证过）。

## 要做的改动

### 1. `trace/target.py`：给 `Target` 基类加一个默认 no-op 的 provision

```python
    def provision(self) -> None:
        """在会话内安装并启动被测智能体到'等登录/就绪'状态。
        默认假设已预装（no-op）。需要自动安装的适配器覆盖此方法。"""
        return None
```
（run_case 不调用 provision；保持测量环纯净。）

### 2. 新建 `trace/provision.py`：通用安装器

一个函数 `install(session, spec: dict) -> None`，按 spec 在会话里下载+静默安装+轮询到就绪。**用后台 bat + flag 文件轮询**扛住命令超时。

spec 字段：
- `installer_type`: `"nsis" | "inno" | "msi" | "zip" | "winget"`
- `url`: 下载直链（nsis/inno/msi/zip 用；winget 为 None）
- `winget_id`: winget 用
- `silent_args`: 可选，覆盖各类型默认静默参数
- `install_dir`: zip 用（解压目标目录）
- `ready_path`: 判定"装好了"的文件绝对路径（如目标 exe），轮询到它存在即成功
- `launch_cmd`: 可选，安装后启动 agent 的命令（如目标 exe 路径）

各类型默认静默参数（这些是业界标准安装器标志，不是从 WorkBuddy 猜的）：
- nsis：`/S`
- inno：`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`
- msi：走 `msiexec /i "<file>" /quiet /norestart`
- winget：`--silent --accept-package-agreements --accept-source-agreements`
- zip：无（用 PowerShell `Expand-Archive` 解压到 `install_dir`）

install() 流程（url 类型）：
1. 生成一个 `.bat`（写到 `C:\Users\Public\_trace_install.bat`），内容：
   - `curl -L -o "<tmp>" "<url>"`（tmp 如 `C:\Users\Public\_trace_installer.<ext>`）
   - 按 installer_type 跑静默安装（nsis/inno：`"<tmp>" <silent_args>`；msi：`msiexec /i "<tmp>" /quiet /norestart`；zip：`powershell -c "Expand-Archive -Force '<tmp>' '<install_dir>'"`；winget：`winget install --id <winget_id> <silent_args>`，无下载步）
   - 结尾 `echo DONE> C:\Users\Public\_trace_provision.flag`
2. 先删旧 flag，再**后台启动** bat：`start "" /B cmd /c "C:\Users\Public\_trace_install.bat"`（用 execute_command 触发，立即返回）。
3. **轮询**：每 ~20s 调一次 `execute_command` 检查 flag 是否出现（`if exist flag echo READY`），最多 ~20 分钟（可参数化 `timeout_s=1200`）。用 Python `time.sleep` 间隔（provision.py 是普通进程，sleep 无限制）。
4. flag 出现后，用 `ready_path` 再确认目标文件存在；不存在则抛错（安装疑似失败）。
5. 若给了 `launch_cmd`，启动它（`start "" "<launch_cmd>"` 或直接 execute_command 触发）。
6. 成功返回 None；超时/失败抛 `RuntimeError`，消息含 flag 状态与 ready_path 检查结果。

> 长命令/含中文路径写进 bat 前用 chcp 65001；execute_command 触发 bat 用 `cmd /c "..."`。参考 oracle.py 里 `_run_via_bat` 的写 bat 手法。

**验证状态诚实标注**：在 install() docstring 里注明——`nsis` 路径已由协调方在 WorkBuddy 上实测；`inno/msi/zip/winget` 是标准安装器模式，首次实际使用时再验证。

### 3. `trace/target_workbuddy.py`：实现 WorkBuddyTarget.provision()

```python
    def provision(self) -> None:
        from . import provision as _prov
        _prov.install(self.session, {
            "installer_type": "nsis",
            "url": "https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/WorkBuddy-win32-x64-user-5.5.6.38337834-5f969292.exe",
            "ready_path": r"C:\Users\administrator\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe",
            "launch_cmd": r"C:\Users\administrator\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe",
        })
```
（其余方法 plant_doc/dispatch/坐标一律不动。）

### 4. `trace/cli.py`：加 `provision` 子命令

`python -m trace.cli provision (--case X.json | --target workbuddy) --session s-xxx`
- 解析 session（同 run 的优先级：--session > case["session_id"] > env）。
- target 来源：`--target` 直接给，或从 `--case` 读 `case["target"]`。
- 调 `get_target(name, session).provision()`。
- stderr 打印进度（开始/轮询中/完成 或 失败）。
- 完成后提示："安装完成，请用 session.info().resource_url 打开网页桌面手动登录，然后用 run 子命令评测。"

### 5. 文档

- README 第 3 节（准备会话）改：把"手动下载安装"更新为"可用 `python -m trace.cli provision ... ` 自动安装；登录仍需手动扫码"。
- CONTRACT.md 第 4 节 CLI 补 `provision` 子命令说明。
- README 第 9 节（接入新 agent）补：新适配器可覆盖 `provision()` 声明 installer_type/url/ready_path/launch_cmd 即可自动安装。

## 约束（严格）

- provision 独立于测量环；**不改 run_case、不改判定逻辑、不改 oracle/report/provider**。
- 不改 WorkBuddy 的 plant_doc/dispatch/坐标。
- **不运行工具、不 pip install、不发网络请求、不真的去装**（协调方会重建一个会话实测 provision）。
- 改完打印：新增/修改文件列表 + `install()` 的类型分支 + `WorkBuddyTarget.provision()` 全文 + `provision` 子命令的 argparse 片段。
