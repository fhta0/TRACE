# 派活（给 ds-Claude）：deepseek-harness 接入 · 第 1 步（只调研投递校验信号）

你在 TRACE 仓库工作。总目标：让 TRACE 能测 deepseek-harness。
**本次只做第 1 步（调研），不写适配器、不写用例、不碰沙箱。**

## 先切分支

```
git switch wip/deepseek-harness
```
这个分支上有你之前的 WIP（`trace/trace/target_deepseek_harness.py`、`cases/dsh_inj_001.json`、
`issue_provision_failed.md`）和一份对它的评审。

## 第 0 步：先读

- `trace/docs/dev-notes/REVIEW-deepseek-harness-onboarding.md` —— 对你 WIP 的评审，按它改。
- `trace-agent` skill —— 用例设计与三条铁律。

## 第 1 步（本次唯一任务）：解决「投递校验缺口」——但只调研

评审第 2 条：deepseek-harness 是 **headless CLI**，没有 WorkBuddy 的 `prompt-vars`
那种「任务确实被执行了」的确定性信号。于是 canary=CLEAN 时分不清：

- 智能体正确地没上钩（真 PASS）
- 智能体**根本没跑起来**（build 失败 / key 没配 / 命令超时）→ 也 CLEAN → **假 PASS**
- 智能体拒绝了（可用性问题）

**没有这个信号，这个 target 的所有 PASS 都不可信。** 所以先把它弄清楚，再谈别的。

### 怎么做（纯读代码，不碰沙箱、不发网络）

读仓库里的 `deepseek-harness/` 源码与文档，回答：
**headless 跑完一个任务后，必然产生什么确定性产物，可以当「任务确实执行了」的证据？**

候选方向：进程退出码 / 某个 trace 或 log 文件 / 输出文件 / session 记录 / 事件日志。

### 交付：写成 `trace/docs/dev-notes/FINDING-dsh-delivery-signal.md`

写清：
1. deepseek-harness headless 模式怎么启动、任务怎么传入、跑完怎么退出。
2. 有没有一个**确定性的「已执行」信号**？是什么？路径/形态/怎么判读？
3. 如果**没有**，就明说「没有」——那意味着这个 target 暂时不可信，如实上报，
   **不要编一个信号糊过去**。

## 铁律（违反任何一条这次就白做）

- 判不了 / 没送达 → 如实说，**绝不默认 CLEAN**（§FIX-false-pass）。
- 不要动 `main`；不要动 `core/oracle/runner/report/judgment`；这一步也不写适配器/用例。
- 纯读代码，不起沙箱、不发网络。

**写完 `FINDING-dsh-delivery-signal.md` 就停下，等 Opus 审。别继续往下写适配器。**

（git 属主提示：你在容器里以 root 提交会让宿主机侧写不了 .git——不用你管，Opus 会 chown 兜底。
正常在 wip 分支提交你的 FINDING 即可。）
