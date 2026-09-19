# 新功能：会话生命周期子命令（补上工具的头和尾）

## 为什么现在要做（这是一次判断修正）

之前有人建议加 `create-session` / `delete-session`，被以"范围蔓延"驳回了。
那个判断**站在上游测评平台的角度是对的**——平台拿到的是一个已登录的长期会话，
自己不需要建也不需要删。

但**站在 skill 使用者的角度是错的**。发布形态是"类似 Claude Code 的工具里的一个 skill"，
实际执行者是能力较弱的模型。对它来说：

```
建会话  -> 工具里没有，只能照 skill 里的 Python 片段抄
provision -> 有
calibrate -> 有
run       -> 有
销毁会话 -> 工具里没有，只能照 Python 片段抄
```

**整个流程的头和尾都不在工具里。** 之前没暴露，是因为协调方每次都用自己写的
Python 脚本把缺口补上了——而那条路径在发布物里根本不存在。

销毁缺失的代价尤其直接：**忘了删就一直计费**，而弱模型最容易漏的就是收尾。
让它照着文档手写 SDK 调用去删会话，是把最不能出错的一步交给最不可靠的环节。

## 要实现的子命令

全部走 `python -m trace.cli session <动作>`，人类可读信息走 stderr，
结构化结果走 stdout（`--json` 时）。

### `session create`

```
python -m trace.cli session create [--image windows_latest] [--label <名字>] [--json]
```

- 用 `CreateSessionParams(image_id=..., lifecycle_policy=LifecyclePolicy(manual_release=True))`。
  **`manual_release=True` 不可省** —— 否则长流程跑到一半会话被自动回收。
- 创建后**轮询等屏幕参数稳定**再返回。实测：启动早期 `get_screen_size` 会返回
  过渡值（见过 1024x768 DPI1.0），稳定后才是真实值（1920x1060 DPI1.25）。
  判据：连续两次读数一致且 width > 1024。
- stdout（`--json`）：`{"session_id": "...", "screen": {...}, "desktop_url": "..."}`
- stderr 要**醒目提示计费**：
  ```
  [TRACE] 会话已创建：s-xxxx（持续计费）
  [TRACE] 用完请务必执行：python -m trace.cli session rm s-xxxx
  ```

### `session rm`

```
python -m trace.cli session rm <session_id> [--json]
python -m trace.cli session rm --all
```

- 删除后**必须回查一次**并把残留情况打出来，不能只报 `success=True`。
- `--all` 删除全部会话，用于收尾兜底。
- 退出码：删干净 0，有残留非 0（让调用方知道钱还在烧）。

### `session list`

```
python -m trace.cli session list [--json]
```

- ⚠️ **已知问题，实现时要处理**：协调方实测 `ab.list()` **不可靠** ——
  有会话正在运行时它返回过空列表。所以：
  - `list` 的结果只能作为参考，输出里要写明这一点；
  - **不要**把"list 为空"当成"没有会话在计费"的证据。
- 若能找到更可靠的枚举方式（比如带 `status` 参数、或按 label 过滤），优先用那个，
  并在注释里写明实测结论。

### `session url`

```
python -m trace.cli session url <session_id>
```

- 打印云桌面地址供人工扫码登录。
- **authcode 有时效**，过期要重新取 —— 所以这必须是个能随时重跑的独立命令，
  而不是只在 provision 结尾打印一次。

## 同时更新 skill

`.claude/skills/trace-onboard-agent/SKILL.md` 里现在教使用者**手写 Python 建会话**：

```python
from agentbay import AgentBay, CreateSessionParams, LifecyclePolicy
ab = AgentBay(api_key=os.environ["AGENTBAY_API_KEY"])
r = ab.create(CreateSessionParams(...))
```

**全部替换成 CLI 命令。** 理由同上：skill 的读者是能力较弱的模型，
每让它手写一段 SDK 调用，就多一个出错面。skill 里应该只剩：

```bash
export AGENTBAY_API_KEY=<key>
python -m trace.cli session create --json        # 拿 session_id
python -m trace.cli provision --target workbuddy --session s-xxxx
python -m trace.cli session url s-xxxx           # 拿地址给人扫码
python -m trace.cli doctor --session s-xxxx --json
python -m trace.cli run --case cases/x.json --out r.json --session s-xxxx
python -m trace.cli session rm s-xxxx            # 务必收尾
```

## 同时更新 CONTRACT.md

说明**两类调用方**，避免上游平台以为自己也要管会话：

- **上游测评平台**：只用 `run`，会话由人工一次性准备好后长期复用，
  平台不需要 `session` 子命令。
- **skill 使用者（工具内的模型）**：走完整生命周期，需要 `session` 子命令。

## 约束

- 只改 `trace/cli.py`、`trace/provider.py`（如需）、`CONTRACT.md`、
  以及 `.claude/skills/trace-onboard-agent/SKILL.md`。
- **不改判定逻辑**：`runner.py` / `oracle.py` / `report.py` / `target_*.py` 一行不动。
- 不引入三方依赖。不发网络、不真的创建会话（真机验证由协调方安排，
  且**必须由 ds 容器里的模型用这些命令自己跑一遍**才算数）。
- 改完打印：`session create/rm/list/url` 四个处理函数全文、
  屏幕参数稳定判定那段、以及改写后的 SKILL.md 操作步骤部分。
