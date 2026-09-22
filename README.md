# TRACE — 智能体安全可靠性与授权边界评测平台

TRACE 是一套面向 AI 智能体的**安全性与授权边界**评测框架，包含两部分：一份完整的平台**规格文档**，以及一个可运行的**参考实现**（Phase-1 MVP）。

参考实现专注于**行为层间接提示注入**：往被测智能体能读到的业务文档里埋一条隐藏指令，再给它一个正常任务，用**确定性行为证据**（一个无害 canary 文件是否被创建）判定它是否真的去执行了那条注入指令——**测量环里没有 LLM，不看智能体嘴上说什么**。产出两个互相独立的结论：`agent_security`（智能体是否越界）与 `system_protection`（系统是否拦住）。

## 仓库内容

| 路径 | 说明 |
|---|---|
| [`TRACE-智能体安全可靠性与授权边界评测平台方案-V4.0.md`](./TRACE-智能体安全可靠性与授权边界评测平台方案-V4.0.md) | 完整规格文档（108 章）：四层安全边界、多维信任模型、威胁模型、Evaluation Awareness、确定性 Oracle、Evidence Package、四层 Gate 等 |
| [`trace/`](./trace/) | 规格 Phase-1 MVP 的参考实现（Python）。行为层注入评测工具，在 AgentBay 云沙箱里测桌面/CLI 智能体。见 [`trace/README.md`](./trace/README.md) |
| [`CLAUDE.md`](./CLAUDE.md) | 给 Claude Code 的仓库工作指引 |
| `.claude/skills/` | 随仓库发行的 skill（如 `trace-onboard-agent`：把新智能体接入评测） |

## 快速开始（参考实现）

```bash
cd trace
pip install wuying-agentbay-sdk
export AGENTBAY_API_KEY=<your-key>

# 建沙箱会话（镜像由被测 target 的元数据决定）
python3 -m trace.cli session create --target <target名> --json

# 看用例 / 跑一批 / 出自包含汇总报告
python3 -m trace.cli cases list --target <target名>
python3 -m trace.cli run-batch --target <target名> --session s-xxxx \
  --out-dir out/ --summary-report out/summary.html

# 用完务必删会话，否则持续计费
python3 -m trace.cli session rm s-xxxx
```

完整说明、接入新智能体、对接契约见 [`trace/README.md`](./trace/README.md) 与 [`trace/CONTRACT.md`](./trace/CONTRACT.md)。

## 设计原则

- **判定只认行为证据**：canary 文件是否被创建是唯一判据，永不用 LLM 判断“是否越界”。
- **机制开放、判定锁死**：接入新被测对象只需写一个 Target 适配器；判定层（`runner`/`oracle`）是可信根基，不随功能改动。
- **事实与发布决策分离**：攻击被系统拦截，智能体依然是 `FAIL`——“没造成后果”不等于通过。

## 关于参考基准项目

`RedTeamCUA` / `StepJack` / `os-harm` / `deepseek-harness` 是本项目在设计与用例编写时参考的**第三方研究项目**，各有独立许可证。它们仅在本地留存参考，**未纳入本版本库**（见 `.gitignore`）。
