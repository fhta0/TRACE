# 修正：让 CLI 能被外部系统无人值守调用

## 背景

上一轮验收测试（见 `REPORT-cli-integration.md`）结论是"不能被无人值守调用"。
13 个场景里 11 个，外部系统只能看到 `exit 1` + 无 `result.json` + Python traceback，
分不清是"参数写错了""用例格式错了""会话过期了"还是"工具自己崩了"。

还有一条比这更危险、上一轮没覆盖到的（要真沙箱才会出现）：

```
agent_security = "ENVIRONMENT_INVALID"   ← 压根没测成
exit code      = 0                        ← 平台看到：成功
```

外部平台按惯例 `if returncode == 0: 入库`，于是三次"没测成"会被统计成
"3 次评测、0 次失败、通过率 100%"。**这是本项目最不能出的错**——
整个 TRACE 的存在意义就是不把"没测出来"报成"安全"。

## 一、退出码语义（本次核心）

```
0  测量完成，结论可信          —— PASS 和 FAIL 都是 0
2  环境无效，没测成            —— ENVIRONMENT_INVALID
3  输入非法                    —— case schema / 参数 / 文件问题
1  工具自身异常                —— 含一切未捕获异常
```

**特别注意：`FAIL` 必须是退出码 0。**

检出被测智能体不安全，是工具**成功**完成了工作。如果 FAIL 用非零退出码，
外部平台每抓到一次真实漏洞就会收到一次"工具失败"告警，甚至自动重试——
重试一个 FAIL 毫无意义。这也正是规格 §20 的要求：
**事实（智能体安全与否）和运行状态（工具成功与否）必须分开表达。**

实现上：`main()` 用 try/except 包住全部分发逻辑，把异常映射成上述退出码，
**绝不让 traceback 泄漏成最终输出**（traceback 可以写进 stderr 供人排查，
但退出码和 result.json 必须是结构化的）。

## 二、任何路径都必须生成 result.json

外部系统永远不应该面对"没有 result.json"这种状态。

失败时 result.json 的形状（在现有字段基础上加 `error`）：

```json
{
  "id": "<case 的 id；取不到时为 null>",
  "runs": 0,
  "agent_security": "NOT_RUN",
  "system_protection": "N/A",
  "failure_rate": "0/0",
  "root_cause": "INPUT_INVALID",
  "verdict_basis": null,
  "error": { "code": "CASE_SCHEMA_INVALID", "message": "case 缺少必填字段：canary" },
  "evidence": []
}
```

`agent_security` 新增取值 **`NOT_RUN`** —— 表示压根没进入测量。
它不是结论，且**永远不可能被误读成 PASS**。这是关键：
宁可多一个诚实的状态，也不要用 PASS/FAIL 去承载"没跑"。

### error.code 取值与退出码对应

| error.code | 退出码 | 触发场景 |
|---|---|---|
| `CASE_FILE_NOT_FOUND` | 3 | `--case` 指向的文件不存在 |
| `CASE_JSON_INVALID` | 3 | 文件不是合法 JSON |
| `CASE_SCHEMA_INVALID` | 3 | 缺必填字段 / 字段类型不对 |
| `UNSUPPORTED_VECTOR` | 3 | `vector != "doc_injection"` |
| `UNSUPPORTED_CANARY_TYPE` | 3 | `canary.type != "file_exists"` |
| `SESSION_NOT_PROVIDED` | 3 | 三种来源都没给 session |
| `API_KEY_MISSING` | 3 | 未设 `AGENTBAY_API_KEY` |
| `OUT_PATH_UNWRITABLE` | 3 | `--out` 所在目录建不了/不可写 |
| `SESSION_NOT_FOUND` | 2 | 会话不存在或已过期（**环境问题**，平台应重建会话后重试） |
| `INTERNAL_ERROR` | 1 | 其余一切未预料的异常 |

`OUT_PATH_UNWRITABLE` 是唯一写不出 result.json 的情况——
此时把同样那份 JSON **打到 stdout**，并在 stderr 说明，退出码仍是 3。

## 三、校验顺序：先校 case，后连会话

现在的顺序是"解析 session → 连 AgentBay → 才校验 vector/canary"，
导致用例格式错误全被 `Session not found` 掩盖。实测：

```
vector=api_injection（契约明确不支持）+ 无效 session
  → 报的是「会话不存在」，不是「vector 不支持」
```

改成：

```
1. 读 case 文件           → 失败：CASE_FILE_NOT_FOUND / CASE_JSON_INVALID
2. 校验 case schema       → 失败：CASE_SCHEMA_INVALID / UNSUPPORTED_*
3. 解析 session_id        → 失败：SESSION_NOT_PROVIDED
4. 准备输出目录            → 失败：OUT_PATH_UNWRITABLE
5. 连接 AgentBay           → 失败：API_KEY_MISSING / SESSION_NOT_FOUND
6. 跑用例
```

**不联网就能发现的错误，一律不要等到联网之后再报。**

新增一个 `_validate_case(case) -> None` 函数集中做第 2 步，
校验：`id` / `target` / `vector` / `doc_filename` / `doc_content` /
`benign_task` / `canary`（且 `canary.type` 与 `canary.path` 都在）。
`runner.run_case` 里那两处 `raise NotImplementedError` 可以保留作为兜底。

## 四、单次 run 的异常隔离

现在 `repeat=5` 时若第 3 次抛异常，整个 case 崩掉，**前两次的证据全部丢失**。

改成：`_run_once` 的调用包 try/except，异常时该次记为

```json
{"run": 3, "canary": "N/A", "delivery": "RUN_ERROR",
 "error": "<异常摘要>", "screenshot": null, "ts": "..."}
```

`_aggregate` 把 `RUN_ERROR` 与现有 `NOT_DELIVERED` / `TIMEOUT` / `ERROR_STATE` 同等对待：
整个 case → `ENVIRONMENT_INVALID`，`root_cause = "RUN_EXECUTION_ERROR"`。
已经跑完的那些 run 的证据**必须保留在 evidence 里**。

## 五、`--out -` 输出到 stdout

外部系统常常不方便约定文件路径。支持 `--out -`：把 result.json 打到 stdout。
此时**所有人类可读的进度信息必须走 stderr**（现在基本已经是了，检查一遍别混进 stdout）。

## 六、`doctor --json`

`doctor` 目前只有退出码 0/1，外部系统想自检拿不到结构化原因。加 `--json`：

```json
{
  "ready": false,
  "checks": [
    {"id": "session",      "ok": true,  "detail": "..."},
    {"id": "process",      "ok": false, "detail": "未发现 WorkBuddy 进程",
     "fix": "启动 WorkBuddy 后重试；若未安装，运行 provision"},
    {"id": "window",       "ok": true,  "detail": "..."},
    {"id": "screen",       "ok": true,  "warn": true, "detail": "..."},
    {"id": "prompt_vars",  "ok": true,  "detail": "目录存在，2 个文件"},
    {"id": "canary_path",  "ok": true,  "detail": "..."},
    {"id": "calibration",  "ok": true,  "warn": true, "detail": "..."}
  ]
}
```

给 `--json` 时，JSON 走 stdout，现有的 ✓/✗ 文字走 stderr（保留，人还要看）。

## 七、顺手修一个真 bug：doctor 里的过期副本

`cli.py` 第 ⑤ 项检查里有一份 `_count_prompt_vars` 的**过期拷贝**，
还在用早就被证伪的判定方式：

```python
etype = str(getattr(entry, "type", "") or "").upper()
if etype in ("FILE", "[FILE]") or getattr(entry, "is_file", False) or "[FILE]" in str(entry):
```

真机实测的 entry 结构是 `entry._data == {"isDirectory": False, "name": "xxx.prompt-vars.json"}`，
`target_workbuddy._count_prompt_vars` 早已修正，但 doctor 这份没跟上。

协调方用真机实测到的结构验证过，两条路径结果不一致：

```
真实文件数        : 2
target_workbuddy  : 2   （已修）
cli.py doctor ⑤   : 0   （过期副本）
```

后果：doctor 永远报"目录存在，**0 个文件**"却仍然打 ✓ ——
**勾是绿的，数字是假的**。对能力较弱的使用者，这是最坏的一种输出。

**修法：删掉这份副本，直接调用 `target._count_prompt_vars()`。**
同一个判定逻辑不允许存在两份实现——这次的 bug 正是复制带来的。

## 八、更新 CONTRACT.md

补上：

1. **§4.1 新增"退出码"小节**，就是本文第一节那张表，并显式写明
   "`FAIL` 的退出码是 0，因为那是一次成功的测量"。
2. **§2 的 `agent_security` 增加 `NOT_RUN` 取值**，说明其含义。
3. **§2 新增可选字段 `error`**（`{code, message}`），说明只在未完成测量时出现。
4. **§5 新增一条前提说明**（重要，集成方一定会踩）：

   > TRACE 需要一个**已经登录好被测智能体**的 AgentBay 会话。
   > 会话由人工一次性准备：创建会话 → `provision` 安装 → 打开云桌面扫码登录。
   > 扫码本质上需要真人，**不可自动化**。
   > 登录后该会话可长期复用，连续跑大量用例无需再次人工介入。
   > 因此 TRACE 的无人值守边界是：**一次人工准备，之后全自动**。

   这条一定要写清楚——否则集成方会以为能全自动建会话，接到一半才发现卡在扫码。

## 九、不要做的事

上一轮报告建议加 `create-session` / `list-sessions` / `status` / `cancel` / `run-batch`
等子命令，**本次一律不做**：

- `status` / `cancel` 暗示常驻服务，TRACE 是同步 CLI，不需要
- `create-session` 看似合理，但自动建的会话没登录，跑不了（见上条）
- `run-batch` 外部平台自己循环调用即可，没必要在工具里再做一层调度

保持工具小而锐利。

## 约束

- 只改 `trace/cli.py`、`trace/runner.py`、`CONTRACT.md`。
  判定逻辑（oracle / target / calibration / report）本次不动。
- 不引入三方依赖。不运行工具、不 pip install、不发网络（协调方负责真机验证）。
- 可以本地跑**不需要沙箱**的验证：用不存在的 case、坏 JSON、缺字段等，
  确认退出码和 result.json 符合上表。**跑完把实际观测到的退出码贴出来。**
- 改完打印：新的 `main()` 异常映射段、`_validate_case()` 全文、
  失败时 result.json 的实际样例、doctor ⑤ 改后的代码、
  以及你本地验证的退出码对照表。
