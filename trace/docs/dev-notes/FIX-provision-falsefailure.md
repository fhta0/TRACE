# 修正：provision 误报失败（`_start_bat_background` 判据错误）

## 实测发现（协调方真机跑过）

在全新 Windows 会话跑 `provision`：安装管线**实际成功**——自动下载 507MB、静默安装 WorkBuddy、启动到登录界面、flag 文件已写。但工具**报了失败**：
```
[TRACE] provision 失败：后台启动安装 bat 失败：<CommandResult ...>
```

## 根因

`_start_bat_background()` 用 `execute_command('start "" /B cmd /c "<bat>"')` 后台启动安装 bat，然后判断 `CommandResult.success`，为 False 就抛 `RuntimeError` 中止。
但 `start "" /B` 启动一个**分离进程**，AgentBay 的 `execute_command` 对这种分离启动**返回 success=False 是常态**（即使分离进程已成功拉起）。于是 `install()` 在真正轮询之前就中止了——而后台 bat 其实照跑到底、flag 也写了。

**核心问题**：不该拿"启动命令的返回值"当成功判据。设计里**唯一的成功信号本来就是 flag 轮询**——bat 跑完会写 flag，轮询到 flag 才算成功。启动命令返回什么无所谓。

## 修法（只改 `trace/provision.py`）

改 `_start_bat_background()`：**不要因 success=False 抛错**。分离启动是"发射后不管"，成功与否由后续 flag 轮询判定。

具体：
- 把 `_start_bat_background` 里 `if not getattr(r, "success", True): raise RuntimeError(...)` 去掉或降级为"不抛错"。可保留一行调试信息（如把返回值记进一个可选日志/忽略），但**绝不能因它中止 provision**。
- 函数改为：触发一次 `execute_command`（后台启动 bat），无论返回如何都正常返回 None。真正的成败交给 `install()` 里的 flag 轮询循环（那段逻辑不变：轮询到 flag→再验 ready_path→成功；超时→带 flag/ready 状态报失败）。

这样：启动命令返回 False 不再误伤；只有"轮询超时且 flag 未出现且 ready_path 不存在"才算真失败。

## 自查
- 确认 `_start_bat_background` 不再抛 `RuntimeError`（或仅在 execute_command 本身抛异常这种真正的传输错误时才抛，不因 success=False 抛）。
- 确认 `install()` 的成功判定仍然只依赖 flag 轮询 + ready_path 二次确认。

## 约束
- 只改 `trace/provision.py`。不改判定逻辑、oracle、report、target 坐标、cli、runner。
- 不运行、不安装、不发网络。
- 改完打印：`_start_bat_background` 修改后的完整函数体。
