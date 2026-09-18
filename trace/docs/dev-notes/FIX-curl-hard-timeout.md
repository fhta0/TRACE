# 修正：curl 卡死时 --speed-limit 不生效，需要硬超时

## 实测证据

第二次遇到同一个问题（上次加了重试标志以为解决了，实际没有）。

新建会话跑 `provision`：
- 30 分钟超时失败，`_trace_installer.exe` **0 字节**，curl 进程仍在
- 检查生成的安装 bat，标志**确实都写进去了**：
  ```
  curl -L --retry 5 --retry-delay 5 --retry-all-errors --connect-timeout 30 \
       --speed-limit 10000 --speed-time 30 -o "..." "..."
  ```
- 杀掉卡死的 curl 后立刻探测 CDN：**40 秒下载 441MB（约 11 MB/s）**，网络完全正常

结论：`--speed-limit/--speed-time` 对这种「连接建立后完全静默」的卡死**不触发**
（可能卡在 TLS 握手或首字节之前，此时尚未进入传输速率统计）；
`--connect-timeout` 只管 TCP 连接建立，管不到握手后的静默。

**验证过的解法**：加 `-m`（`--max-time`）硬超时。
实测用下面这条命令后台安装，**30 秒装完**（对比之前卡死 30 分钟）：
```
curl -L -m 300 --connect-timeout 20 --retry 3 --retry-delay 5 -o "<tmp>" "<url>"
```

## 修法（只改 trace/provision.py 的 _build_bat_body 下载行）

在现有 curl 标志基础上**加 `-m 300`**（5 分钟硬超时）。CDN 正常时 507MB 只需约 50 秒，
300 秒给足余量；一旦卡死，curl 会在 5 分钟内自行退出，随后 `--retry` 才有机会真正触发重试。

改成：
```python
lines.append(
    f'curl -L -m 300 --connect-timeout 20 '
    f'--retry 3 --retry-delay 5 --retry-all-errors '
    f'--speed-limit 10000 --speed-time 30 '
    f'-C - -o "{tmp_path}" "{url}"'
)
```
要点与理由写进注释：
- `-m 300`：**硬超时，这是关键**。卡死时 --speed-limit 不触发，只有 -m 能把进程拽回来，
  让 --retry 有机会重试。实测加上后 30 秒装完，不加会卡 30 分钟。
- `--connect-timeout 20`：TCP 连接阶段超时。
- `--retry 3 --retry-delay 5 --retry-all-errors`：失败后重试。
- `-C -`：断点续传，重试时不从头下。

（`-m` 与 `-C -` 配合：每次尝试最多 300 秒，重试时从断点继续，
所以大文件在慢网下仍能通过多次重试累积完成。）

## 顺带

`install()` 的默认 `timeout_s` 当前是 1800（30 分钟）。加了 `-m 300` + 3 次重试后，
最坏约 300×4 = 20 分钟，1800 仍然够用，**不需要改**。

## 教训（写进注释）

"加了重试标志" ≠ "验证过重试会触发"。上一轮加了 `--speed-limit/--retry` 就认为解决了，
没有真机验证卡死场景，结果同一个问题第二次出现。

## 约束
- 只改 `trace/provision.py`。不改判定逻辑、runner、oracle、target、cli、calibration。
- 不运行工具、不 pip install、不发网络。
- 改完打印修改后的 curl 下载行完整内容。
