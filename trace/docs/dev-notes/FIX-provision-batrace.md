# 修正：provision.py 的 bat 文件竞争（P0 并发 bug）

协调方审出：安装 bat 与轮询检查**共用同一个 `_BAT_PATH`**，会在真实安装时炸。

## 根因

- `install()` 把安装脚本写到 `_BAT_PATH`（`_trace_install.bat`），然后 `_start_bat_background` 让它在**后台跑数分钟**（curl 下载 507MB + 静默安装）。
- cmd.exe 执行 .bat 是**逐行从磁盘读**的。
- 轮询期间 `_check_flag()` / `_check_ready_path()` → `_run_bat_once()` → `_write_bat()` **覆写同一个 `_BAT_PATH`**。
- 于是后台安装脚本读到下一行时，文件内容已被轮询脚本覆盖 → 安装命令 + 打 flag 那行被冲掉 → flag 永不出现 → provision 超时失败。

## 修法：轮询用独立 bat 路径，别碰后台安装 bat

1. 新增一个独立常量：
   ```python
   _INSTALL_BAT = r"C:\Users\Public\_trace_install.bat"   # 后台安装脚本，写一次、后台跑，轮询期间不得改动
   _POLL_BAT    = r"C:\Users\Public\_trace_poll.bat"      # 轮询/查询用，可反复覆写
   ```
   把原来的 `_BAT_PATH` 拆成这两个（原 `_BAT_PATH` 删除或保留为 `_INSTALL_BAT` 的别名，二选一，清晰即可）。

2. `_write_bat` / `_run_bat_once` 增加一个 `bat_path` 参数（默认用 `_POLL_BAT`），执行时 `cmd /c "{bat_path}"` 用传入的路径：
   ```python
   def _write_bat(session, body, bat_path=_POLL_BAT): ...
   def _run_bat_once(session, body, bat_path=_POLL_BAT): ...
   ```

3. **安装脚本**走 `_INSTALL_BAT`：
   - `install()` 里写安装 body 用 `_write_bat(session, body, _INSTALL_BAT)`。
   - `_start_bat_background` 启动 `_INSTALL_BAT`（把它内部的 `_BAT_PATH` 改成 `_INSTALL_BAT`）。

4. **轮询/查询**（`_check_flag` / `_check_ready_path`）走 `_POLL_BAT`：它们调 `_run_bat_once` 时用默认 `_POLL_BAT`，从而**永不触碰**正在后台跑的 `_INSTALL_BAT`。

5. 自查：确认 `_INSTALL_BAT` 在 `install()` 全流程中只被写一次（启动前），轮询循环里不再有任何对 `_INSTALL_BAT` 的写入。

## 约束
- 只改 `trace/provision.py`。不改判定逻辑、oracle、report、target 坐标、cli。
- 不运行、不安装、不发网络。
- 改完打印：两个 bat 路径常量、`install()` 里写安装 bat 那行、`_check_flag`/`_check_ready_path` 用的 bat 路径，自证安装 bat 与轮询 bat 已分离。
