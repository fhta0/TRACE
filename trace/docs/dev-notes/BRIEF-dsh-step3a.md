# 派活（给 ds-Claude）：deepseek-harness 接入 · 第 3a 步（provision 长命令改后台+轮询）

**只改 `trace/trace/target_deepseek_harness.py` 里 provision 相关的方法。**
不碰 dispatch（第 2 步已审过）、不碰 plant_doc、不动 trace/trace 里的 core/oracle/runner/report、
不改 provision.py（只读它当范例）、不碰沙箱、不发网络。纯写代码 + 静态自查。

## 问题

现有 provision 链（`_install_nvm`/`_install_node`/`_install_pnpm`/`_clone_dsh`/`_build_dsh`）
都用 `self._run_cmd(cmd, timeout_ms=...)`，即 `execute_command` **前台硬等**。
但 AgentBay 的 `execute_command` 有**单命令时长上限**（几分钟就超时）。
`git clone` 大仓库、`pnpm install`、`pnpm run build` 一个 monorepo，都会超时 → provision 假失败。

## 要做的

### 1. 加一个自包含的「后台跑长命令 + 轮询 flag」辅助方法

在 `DeepseekHarnessTarget` 里加，例如 `_run_long_cmd(self, cmd, flag_name, timeout_s, poll_interval_s=10)`：

- 写一个 bat（用 `filesystem.write_file`）：**先清旧 flag**，跑 `cmd`，
  **按退出码打 flag**（成功打 `DONE`、失败打 `FAIL:<code>`）。裸批处理语法。
- 用 `start "" /B cmd /c "<bat>"` 后台启动（`start /B` 的 `success=False` 是常态，别拿它判成败）。
- 轮询 flag 文件（`filesystem.read_file` 或 list_directory）直到出现 `DONE`/`FAIL` 或超时。
- 返回成功/失败（失败要带上 flag 里的退出码），超时也算失败。

**范例照 `trace/provision.py`（只读，别改）**：
- `_write_bat()` / `_start_bat_background()` / `_check_flag()` 的 SDK 用法
- 顶部注释里的 Windows 批处理铁律：**安装 bat 与轮询 bat 必须是不同文件**
  （cmd 逐行从磁盘读 bat，轮询覆写同一文件会把正在跑的脚本读串）；
  裸批处理语法、不要再套 `cmd /c "..."`、路径别用 `\"` 转义。
- 每个长命令用**各自不同的 flag 文件名**，避免串台。

### 2. 把长命令换成用它

- `_clone_dsh`（git clone）、`_build_dsh`（pnpm install + pnpm run build）**必须**改用 `_run_long_cmd`。
- `_install_node`（nvm install）、`_install_pnpm`（npm i -g pnpm）可能也慢，一并改。
- 短命令（版本查询、mkdir、setx）保持 `_run_cmd` 即可。
- 每步**失败要抛异常**（带清楚的错误信息），不要吞错继续 —— 否则 provision 假成功，
  dispatch 对着没装好的 dsh 跑，得到假结果。

## 交付

- 只改 `target_deepseek_harness.py` 的 provision 相关方法（可加私有辅助）。
- 自查：`python3 -c "import ast; ast.parse(open('trace/trace/target_deepseek_harness.py').read())"` 过。
- 在 wip 提交，输出一句话说明：哪些命令改成了后台+轮询、flag 文件怎么区分。

## 铁律

- 每步确定性后置校验、失败即抛，绝不吞错。
- 后台/轮询 bat 用不同文件路径；裸批处理语法。
- 只读代码得结论，不碰沙箱、不发网络、不动 provision.py/core/oracle/runner/report/dispatch。
