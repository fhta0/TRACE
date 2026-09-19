# 验收：全流程由工具内的模型独立完成

## 这次验的是什么（和以往不同，先读这段）

以往的验收验的是"功能对不对"。**这次验的是"发布形态成不成立"。**

TRACE 的发布形态是**类似 Claude Code 的工具里的一个 skill**，
实际运行者是能力较弱的模型。所以：

> **凡是你需要绕开 CLI、自己手写 Python 调 AgentBay SDK 才能完成的步骤，
> 都是工具的缺口，必须记录下来 —— 那比跑通本身更重要。**

协调方此前正是用自己写的脚本把这些缺口填上了（建会话、救下载、销毁会话），
所以缺口一直没暴露。这次不许填。

## 铁律

1. **只用 `python -m trace.cli ...` 和 skill 里写明的步骤。**
2. **不许自己写 Python 脚本调 agentbay SDK。** 一行都不行。
3. 遇到 CLI 做不到的事：**停下来记录**，写进报告的"工具缺口"一节，
   不要自己想办法绕过去。
4. 判据只看**退出码**和**命令输出的 JSON**。不看截图、不读 stderr 散文做判断。

## 阶段一：建环境（本次派单执行）

按 `.claude/skills/trace-onboard-agent/SKILL.md` 的指引走。预期是这些命令：

```bash
export AGENTBAY_API_KEY=<派单方已注入>
cd /TRACE/trace
python -m trace.cli session create --json
python -m trace.cli provision --target workbuddy --session <id>
python -m trace.cli session url <id>
```

拿到云桌面地址后**停下来**，把地址和 session_id 打出来交给协调方 ——
扫码登录必须真人完成，这是流程里唯一绕不开的人工点，不要试图自动化。

记录：每条命令的退出码、耗时、以及你是否需要偏离 skill 的指引。

**provision 这一步要特别留意**：下载 507MB，新实现的看门狗会盯文件大小增长、
卡死就杀 curl 续传重启。留意 stderr 有没有进度输出，卡了多久、重启了几次。
（此前真机上 curl 曾静默卡死 42 分钟，curl 自己的超时参数一个都没触发。）

## 阶段二：跑评测（登录后另行派单）

```bash
python -m trace.cli doctor    --session <id> --json
python -m trace.cli calibrate --session <id>
python -m trace.cli run --case cases/wb_inj_001.json --out r1.json --session <id>
python -m trace.cli run --case cases/wb_inj_003.json --out r3.json --session <id>
python -m trace.cli session rm <id>
```

**calibrate 这次要重点看**：新增了 MSAA 语义定位作为第一候选。
stderr 会打出走的是哪条路径，标定文件里也有 `coords_source` 字段：

- `"msaa"` —— MSAA 命中且通过 prompt-vars 验证（预期走这条）
- `"search"` —— MSAA 没命中或没验过，退回了穷举搜索

**两种都算正常**，如实记录即可。但要记下：耗时多少、prompt-vars 从几变到几。
MSAA 相关代码**一行都没在真机跑过**，这是它的第一次实跑。

## 报告

写到 `docs/dev-notes/REPORT-skill-e2e.md`：

### 1. 命令流水
| 步骤 | 命令 | 退出码 | 耗时 | 输出要点 |

### 2. 工具缺口（本次最重要的一节）
任何一处"CLI 做不到、我本来想自己写脚本"的地方，逐条写：
- 卡在哪一步
- 缺什么能力
- 如果硬要继续，你本来打算怎么绕（**只写打算，不要真做**）

**这一节为空，才说明发布形态成立。**

### 3. calibrate 走的哪条路径
`coords_source` 的值、MSAA 命中/未命中的 stderr 原文、耗时、prompt-vars 变化。

### 4. 评测结论
两个用例的 `agent_security` / `root_cause` / 退出码。
预期 001 = PASS、003 = FAIL，但**如实记录实际值**，不要为了对上预期而重试。

### 5. 收尾确认
`session rm` 的退出码，以及残留会话的实际输出 —— **这关系到是否还在计费**。

## 约束

- 不改任何代码。发现 bug 写进报告，不要动手修。
- 会话持续计费：阶段一做完就停，不要空转等待。
