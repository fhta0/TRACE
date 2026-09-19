# 修正：下载改用外部看门狗，不再依赖 curl 的任何超时参数

## 实测证据（2026-09-19，真机）

这是同一个问题的**第三次**出现。前两次分别加了 `--speed-limit/--speed-time/--retry`
和 `-m 300`，都以为解决了。这次拿到了决定性证据：

```
沙箱里实际执行的 bat 里，参数确实都在：
  curl -L --retry 5 --retry-delay 5 --retry-all-errors --connect-timeout 30 \
       --speed-limit 10000 --speed-time 30 -m 300 -o "..." "..."

curl 进程 StartTime  = 09:33:05
当前时间             = 10:15:09     -> 已运行 42 分钟
文件最后写入时间      = 09:33:56     -> 41 分钟 0 字节增长
文件大小             = 16,908,288 / 532,127,544
```

**`-m 300` 没有开火。** 连同之前验证过的 `--connect-timeout` /
`--speed-limit` / `--retry`，curl 的超时机制在这个环境的卡死状态下**一个都不触发**
（推测：socket 读操作被楔死，没有回到 curl 的事件循环，计时器根本没机会被求值）。

### 上一轮"验证"错在哪（重要）

上一轮我手工跑 `curl -m 300 ...`，30 秒装完，就记成"验证过的解法"。
但那只证明了**健康下载跑得快**，完全没有证明 `-m` 在**卡死时**会生效。

> **验证一个修复，必须在它要修的那个故障状态下验证，而不是在正常状态下验证。**

这条已经是第三次交学费，请写进 `provision.py` 的注释。

## 已验证有效的解法：外部看门狗

协调方手工跑通了，**同一个卡死的下载被救回来**：

```
断点 16,908,288 -> 杀掉 curl -> curl -C - 续传
-> 下载完成 532,127,544 字节，与 Content-Length 完全一致
-> 静默安装成功，WorkBuddy 进程起来了
```

一次 kill + 续传就够了，剩下 515MB 一口气下完没再卡。

核心思想和本项目一贯做法一致：**不信标志，只信可观测的确定性信号。**
（prompt-vars 数文件、canary 查文件存在，都是这个思路。）

## 要实现的改动（`trace/provision.py`）

### 1. bat 只负责启动下载，不再承担超时责任

把现在"一条 bat 干完下载+安装+打 flag"拆开。下载阶段改由 Python 侧驱动：

- bat（或直接 `execute_command`）只做：`start "" /B curl -L -C - --connect-timeout 20 -o <tmp> <url>`
- **不要再写 `-m` / `--speed-limit` / `--speed-time` / `--retry`**，它们在这里是无效的安慰剂。
  保留 `--connect-timeout 20`（建连阶段确实有效）和 `-C -`（续传，看门狗重启时靠它）。

`-C -` 是安全的：进入 `install()` 时会先删掉旧的 tmp 文件（这个逻辑已经有了，保留），
所以续传的对象只可能是**本次运行**自己下了一半的同一个文件。

### 2. Python 侧看门狗（核心）

`install()` 已经在轮询 flag 了，把下载监控并进同一个循环：

```
STALL_S   = 90     # 连续 90 秒无增长即判定卡死
POLL_S    = 15
```

流程：

```
1. 先取 Content-Length：curl -sIL <url>，解析最后一个 Content-Length
   取不到 -> 抛 RuntimeError（没有完成判据就不要开始下，否则又回到"猜"）
2. 删除旧 tmp 文件
3. 循环（总时限 DEADLINE_S，建议 3000 秒）：
     cur = 文件大小
     if cur >= total:            下载完成，跳出
     if 没有 curl 进程在跑:       启动 curl（-C - 续传）
     elif cur > last_size:       记录增长，刷新 last_change
     elif now - last_change > STALL_S:
                                 杀掉 curl（Stop-Process -Name curl -Force）
                                 下一轮会自动重启，靠 -C - 续传
4. 完成判据：文件大小 == Content-Length
   **不要看 curl 的退出码** —— 本次实测它毫无参考价值
5. 再执行静默安装，然后走现有的 ready_path 轮询
```

### 3. 顺带白捡的完整性校验

以前只要 curl 说自己成功就拿去装，装出一个损坏的包也不知道。
现在有了 `Content-Length` 比对，等于免费获得一层完整性校验。
**大小不符一律不准进入安装步骤**，抛 RuntimeError 并在消息里写明
"实际 X 字节 / 期望 Y 字节"。

### 4. 进度可见

每次观察到增长时往 stderr 打一行：

```
[TRACE] 下载 123,456,789 / 532,127,544 (23.2%)  1024 KB/s
```

发布后跑这个工具的是能力较弱的模型，卡了 40 分钟却一行输出都没有是最糟的情况。
看得见进度，才能判断"在下载"还是"卡住了"。

### 5. 失败信息要自解释

超过总时限仍未下完时，stderr 要写清楚：已下多少 / 共多少 / 重启过几次 /
最后一次增长在多久以前，并给出建议（换时间重试 / 检查 CDN 可达性）。
不要只抛一句 "provision 超时"。

## 参考实现

协调方实测跑通的脚本在 scratchpad `rescue.py`，逻辑可直接移植。
关键几个 PowerShell 片段（都已在真机验证）：

```
取大小:   powershell -NoProfile -Command "if (Test-Path 'P') { (Get-Item 'P').Length } else { 0 }"
数进程:   powershell -NoProfile -Command "(Get-Process curl -ErrorAction SilentlyContinue | Measure-Object).Count"
杀进程:   powershell -NoProfile -Command "Stop-Process -Name curl -Force -ErrorAction SilentlyContinue"
取长度:   powershell -NoProfile -Command "(curl.exe -sIL 'URL' | Select-String -Pattern '^Content-Length' | Select-Object -Last 1).ToString()"
后台下载: start "" /B curl -L -C - --connect-timeout 20 -o "TMP" "URL"
```

注意 `start "" /B` 的 `success=False` 是常态，**不要拿它判成败**（老坑，已记录在 skill 里）。

## 约束

- 只改 `trace/provision.py`。不动判定逻辑、runner、oracle、target、cli、calibration。
- 不引入三方依赖。不运行工具、不 pip install、不发网络（协调方负责真机验证）。
- 把上面"验证必须在故障状态下做"那条教训写进代码注释，替换掉现在那段
  关于 `-m 300` 的注释（那段结论已被推翻，留着会误导后来的人）。
- 改完打印：新的下载看门狗主循环全文、Content-Length 获取与校验那段、
  以及一条进度输出和一条失败输出的示例文案。
