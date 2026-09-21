# 决策 + 发现：deepseek-harness 从 Windows 转 Linux（aio-ubuntu-2404）

## 起因：Windows 装机这条路太脆

deepseek-harness 是 Linux 原生的 Node CLI agent。最初 WIP 想在 AgentBay 的
**Windows 沙箱**里靠 nvm-windows 装 Node 再 build，build 冒烟第 1 步就崩：

```
步骤 1/5 装 nvm: "nvm-setup.exe /S" → Error 258 execute command timeout
步骤 2/5: nvm install → exit 9009 (nvm 不存在) → provision 失败
```

根因：nvm-windows 是 **Inno Setup**（`/S` 是 NSIS 语法，不触发静默 → 弹 GUI → 卡死超时）；
且该步没走后台+轮询、还吞错继续，误报到步骤 2。Windows 上装 Node monorepo 是一长串脆点。

## 转向：Linux 桌面镜像 aio-ubuntu-2404（用户提供）

真机探针（会话 s-04pomilwscrkq9ytc，已销毁）：

| 项 | 结果 |
|---|---|
| 建会话 / 桌面地址 / 分辨率 | ✅ 一上来就 1920x1080，有桌面（osType=Linux，可看） |
| node / npm / git / curl | ✅ **全预装**（node v26.3），**不需要 nvm** |
| filesystem / command MCP | ✅ write/list_directory 都通（oracle 能用） |

**build 冒烟（手动、detached 后台跑）**：
```
corepack enable → git clone --depth 1 → pnpm install → pnpm run build
=> BUILD DONE，"✓ built in 5.73s"，recorded 248 client artifact(s)
```
**最大未知数解决：deepseek-harness 在这个 Linux 上干净 build 通。**

## 结果：适配器按 Linux 重写（提交 961bf51）

- 路径全 Linux（`_DSH_DIR=/root/deepseek-harness`），去掉 nvm/`C:\`/setx。
- provision 从 5 步脆链缩成 **4 步**：corepack → clone → install → build（node/git 预装不装）。
- `_run_long_cmd` 改 Linux：写 `.sh` + `setsid bash …&` 后台 + 轮询 `/tmp/_dsh_<step>.flag`。
- dispatch 保留投递校验（--json 事件流 + 退出码），并修掉一个**假阴性集成 bug**：
  原来 dsh 退出 1（跑了但 completed-with-error）会被当 NOT_DELIVERED 丢弃 →
  加 `raise_on_fail=False`，让退出 1 + run 事件正常判 OK，再由 canary 定 PASS/FAIL。

## 教训

- **别硬套宿主镜像**：WorkBuddy 必须 Windows（GUI 应用），但 headless CLI agent 该用
  最贴近它原生环境的镜像。一个 `aio-ubuntu-2404` 把 nvm-windows 那一长串坑全绕过了。
- **build 冒烟先行**：花很少沙箱时间就否决了脆的 Windows 路、验证了 Linux 路，
  避免在错误地基上继续堆适配器代码。
- 弱模型（ds-Claude）配正确 launch 配方 + 精确 brief + 逐段审，能承担大重写；
  但**共享方法的契约叠加**（_run_long_cmd 的"非0即抛" × dispatch 的"异常即 NOT_DELIVERED"）
  这种集成 bug 得靠审核抓。见 [[dispatch-dsclaude-recipe]]。

## 离真机端到端冒烟（3b-2）还差

1. Linux §21 合规用例：`cases/DSH-INJ-101.json`（已写：metadata 伪装、canary=/tmp/workspace_ready.marker）。
2. `DEEPSEEK_API_KEY`（agent 跑任务要调模型，用户提供）。
3. 一次沙箱运行：建 Linux 会话(--image aio-ubuntu-2404) → provision → 投用例 → dispatch → 验 canary+投递。
   注意：session create 目前默认 windows_latest，deepseek 要显式 `--image aio-ubuntu-2404`。
