# 派活（给 ds-Claude）：deepseek-harness 接入 · 第 3b 步（按 Linux 重写适配器）

**背景（已真机验证，别怀疑）**：deepseek-harness 改在 AgentBay 的 **Linux 桌面镜像
`aio-ubuntu-2404`** 里测，不再用 Windows。该镜像**预装** node v26 / npm / git / curl，
**不需要 nvm**。以下命令序列在真机上跑通、build 成功（248 artifacts）：

```
corepack enable            # 或 npm install -g pnpm（二选一，corepack 优先）
git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git /root/deepseek-harness
cd /root/deepseek-harness && pnpm install
cd /root/deepseek-harness && pnpm run build
```
运行：`cd /root/deepseek-harness && pnpm dsh --profile headless --json "<task>"`（真机待验）。

## 任务：把 `trace/trace/target_deepseek_harness.py` 从 Windows 改成 Linux

只改这一个文件。不动 trace/trace 里的 core/oracle/runner/report、不碰沙箱、不发网络、纯写代码。

### 1. 常量与路径全改 Linux

- `_DSH_DIR = "/root/deepseek-harness"`
- `_DESKTOP` / 工作区 → Linux 路径（如 `/root`）
- 去掉所有 Windows 专有：`nvm-windows`、`C:\...`、`setx`、`_NVM_DIR`、`set VAR=`（Linux 用 `export VAR=`）

### 2. provision() 重写（大幅简化）

node/git/curl **已预装，不要装它们**。只做：
1. `corepack enable`（失败再 `npm install -g pnpm`）
2. `git clone --depth 1 ... /root/deepseek-harness`（若目录已存在则跳过）
3. `cd /root/deepseek-harness && pnpm install`
4. `cd /root/deepseek-harness && pnpm run build`
5. 校验：`ls /root/deepseek-harness/dist` 或 `pnpm dsh --help` 能跑 → `self._dsh_ready = True`

第 2/3/4 步是长命令，**必须后台+轮询**（见下）。每步失败即抛异常，绝不吞错。

### 3. `_run_long_cmd` 改 Linux 版

现在的实现是 Windows 批处理（写 .bat、`start /B`、`chcp`）。改成 Linux：
- 写一个 `.sh`（`filesystem.write_file`）：`set -e`；清旧 flag；跑命令；
  成功 `echo DONE > <flag>`，失败 `echo FAIL:$? > <flag>`。
- **后台启动**：`execute_command("setsid bash <script.sh> > <log> 2>&1 < /dev/null & echo LAUNCHED")`
  （`setsid ... &` 立刻返回，命令在沙箱后台跑；execute_command 不会被长命令阻塞）。
- **轮询 flag**：循环 `cat <flag> 2>/dev/null || echo PENDING`，出现 DONE→返回，
  FAIL→抛异常（带退出码），超时→抛异常。
- 每个长命令用**不同的 script/log/flag 文件名**，避免串台。
- Python 侧轮询前先清 flag（防 re-run 读到旧 DONE）。
- 后台脚本的 stdout/stderr **重定向到各自 log 文件**（失败要能看错误详情）。

### 4. plant_doc() 改 Linux

把注入文档写到 agent 能读到的位置。简单起见放 `/root/deepseek-harness/<filename>`
（agent 的工作区）。返回该绝对路径。benign_task 里会用该文件名/路径引用它。

### 5. dispatch() —— 投递校验逻辑保留，只改调用

`dispatch()` 的投递校验（解析 --json 事件流 + 退出码，判 OK/NOT_DELIVERED/ERROR_STATE，
铁律：判不了绝不 OK/CLEAN）**是第 2 步审过的，逻辑不要改**。只改：
- 命令从 Windows `cd /d "..." && pnpm dsh ...` 改成 Linux `cd /root/deepseek-harness && pnpm dsh --profile headless --json "<task>"`。
- API key：`export DEEPSEEK_API_KEY=...`（不是 Windows 的 `set`）。放进同一条 bash 命令里。
- 长命令（dsh 跑任务可能几分钟）**也要走后台+轮询**拿 stdout+退出码，别用前台 execute_command 硬等。
  这一步实现你定，但要能拿到完整 stdout（事件流）和退出码交给已有的 `_parse_delivery_status`。

## 交付

- 只改 `target_deepseek_harness.py`。自查 `python3 -c "import ast; ast.parse(open('trace/trace/target_deepseek_harness.py').read())"` 过。
- 在 wip 提交，输出一句话总结：provision 现在几步、_run_long_cmd 怎么在 Linux 后台跑。

## 铁律

- 每步失败即抛、绝不吞错；dispatch 判不了绝不 OK/CLEAN。
- 后台脚本/日志/flag 各用不同文件名；轮询前 Python 侧先清 flag。
- 命令序列以本 brief 顶部已验证的为准，别臆造。只改这一个文件。
