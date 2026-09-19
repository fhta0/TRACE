# TRACE CLI 集成测试报告

**测试日期**：2026-09-18  
**测试角色**：外部集成工程师（上游测评平台）  
**测试范围**：CLI 异常路径，不触碰真实沙箱  
**核心问题**：外部系统能否只凭 `returncode` + `result.json` 正确区分三种情况：①测出结论 ②环境问题没测成 ③工具自己崩了

---

## 1. 结果表

| # | 场景 | 退出码 | result.json | 外部系统能正确归类吗？ |
|---|---|---|---|---|
| 1 | 不给 session | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 2 | case 文件不存在 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 3 | case 不是合法 JSON | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 4 | case 缺必填字段 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 5 | vector 不支持 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 6 | canary.type 不支持 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 7 | session 不存在 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 8 | API key 缺失 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 9 | out 路径不可写 | 1 | 未生成 | 不能——只能看出失败但分不清原因 |
| 10 | 无子命令 | 2 | 不适用 | 能（命令行用法错误，不是评测失败） |
| 11 | help | 0 | 不适用 | 能（正常退出，显示帮助） |
| 12 | doctor 坏 session | 1 | 不适用 | 不能——只能看出失败但分不清原因 |
| 13 | calibrate 坏 session | 1 | 未生成 | 不能——只能看出失败但分不清原因 |

**关键发现**：所有 `run` 子命令的异常路径都返回退出码 1，且都不生成 `result.json`。外部系统**完全无法区分**"环境有问题没测成"和"工具自己崩了"。

---

## 2. 逐条说明「不能」的那些

### 场景 1：不给 session
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  错误：未提供 session_id。请通过 --session、case['session_id'] 或环境变量 TRACE_WB_SESSION 指定。
  ```
- **外部系统会错误地得出什么结论**：退出码 1 + 无 result.json，和工具 bug、环境问题完全无法区分。平台会把"调用方配置错误"记成"工具故障"或"环境问题"，但实际上这是参数缺失。
- **后果**：误报率上升，运维团队浪费时间去排查"工具为什么崩了"，实际上是调用方没传 session。

### 场景 2：case 文件不存在
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  Traceback (most recent call last):
    File "<frozen runpy>", line 198, in _run_module_as_main
    ...
  FileNotFoundError: [Errno 2] No such file or directory: '/TRACE/trace/.cli-it/nope.json'
  ```
- **外部系统会错误地得出什么结论**：Python traceback 暴露了内部实现细节，但外部系统只能看到"退出码 1 + 无 result.json"，和任何其他异常路径一样。平台无法判断这是"case 文件路径错了"还是"工具内部 bug"。
- **后果**：调用方文件路径拼写错误时，平台会记成"TRACE 工具故障"，触发不必要的告警。

### 场景 3：case 不是合法 JSON
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  Traceback (most recent call last):
    ...
  json.decoder.JSONDecodeError: ...
  ```
- **外部系统会错误地得出什么结论**：同上，JSON 解析错误被当成"工具崩溃"。外部系统无法区分"case 文件格式错了"和"工具内部异常"。
- **后果**：用例管理系统的 bug（生成了坏 JSON）会被记成 TRACE 工具故障。

### 场景 4：case 缺必填字段
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  错误：未提供 session_id。请通过 --session、case['session_id'] 或环境变量 TRACE_WB_SESSION 指定。
  ```
- **外部系统会错误地得出什么结论**：这里有个意外——case 缺 `canary` 字段，但工具先检查 `session_id`，所以报的是"未提供 session_id"。外部系统看到这个错误，会以为是调用方没传 session，但实际上是 case schema 不合法。
- **后果**：调用方会去检查 session 配置，浪费时间；真正的 schema 验证错误被掩盖。

### 场景 5：vector 不支持
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  2026-09-18 21:05:12.074 | AgentBay | INFO | ... | 🔗 API Call: GetSession
  2026-09-18 21:05:12.074 | AgentBay | INFO | ... |   └─ SessionId=s-test-001
  2026-09-18 21:05:13.047 | AgentBay | INFO | ... | Session not found: s-test-001
  Traceback (most recent call last):
    ...
  ```
- **外部系统会错误地得出什么结论**：工具在验证 `vector` 之前就尝试连接 session，所以报的是"Session not found"。外部系统会以为是 session 配置问题，但实际上是 `vector="api_injection"` 不支持。
- **后果**：调用方会去检查 session，而真正的 vector 不支持问题被掩盖。验证顺序不合理。

### 场景 6：canary.type 不支持
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  2026-09-18 21:05:16.313 | AgentBay | INFO | ... | 🔗 API Call: GetSession
  2026-09-18 21:05:16.975 | AgentBay | INFO | ... | Session not found: s-test-001
  Traceback (most recent call last):
    ...
  ```
- **外部系统会错误地得出什么结论**：同场景 5，工具先尝试连接 session，`canary.type="registry_key"` 的验证被跳过。外部系统看到的是"Session not found"，无法判断是 session 问题还是 canary 类型不支持。
- **后果**：schema 验证错误被掩盖，调用方无法快速定位问题。

### 场景 7：session 不存在
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  2026-09-18 21:05:33.479 | AgentBay | INFO | ... | 🔗 API Call: GetSession
  2026-09-18 21:05:34.133 | AgentBay | INFO | ... | Session not found: s-doesnotexist-000
  Traceback (most recent call last):
    ...
  ```
- **外部系统会错误地得出什么结论**：这是真正的"环境问题没测成"（session 不存在或已过期），但退出码 1 + 无 result.json，和工具 bug 完全无法区分。平台会把环境问题记成工具故障。
- **后果**：沙箱会话过期时，平台会触发"TRACE 工具故障"告警，而不是"环境问题，需要重新创建会话"。

### 场景 8：API key 缺失
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  Traceback (most recent call last):
    ...
  RuntimeError: AGENTBAY_API_KEY 未设置。请通过参数传入或环境变量提供。
  ```
- **外部系统会错误地得出什么结论**：这是调用方配置错误（缺少 API key），但退出码 1 + traceback，和工具内部 bug 一样。平台会记成"工具故障"。
- **后果**：部署配置遗漏时，运维团队会去排查 TRACE 工具本身，而不是检查环境变量。

### 场景 9：out 路径不可写
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  Traceback (most recent call last):
    ...
  FileNotFoundError: [Errno 2] No such file or directory: '/proc/nope'
  ```
- **外部系统会错误地得出什么结论**：这是调用方传了错误的 `--out` 路径，但退出码 1 + traceback，和工具内部异常一样。平台会记成"工具故障"。
- **后果**：路径配置错误被当成工具 bug。

### 场景 12：doctor 坏 session
- **退出码**：1
- **result.json**：不适用（doctor 不生成 result.json）
- **stderr 前 800 字**：
  ```
  [TRACE] doctor target='workbuddy' session=s-doesnotexist-000
  ① 会话可连接 ... 2026-09-18 21:06:29.277 | AgentBay | INFO | ... | 🔗 API Call: GetSession
  2026-09-18 21:06:30.130 | AgentBay | INFO | ... | Session not found: s-doesnotexist-000
  ✗
     原因：会话 's-doesnotexist-000' 不存在或已过期（AgentBay 返回 session=None）。
     修复：检查 session_id 是否正确、AGENTBAY_API_KEY 是否设置。

  [TRACE] doctor 完成：1 项未通过 (session)。
  ```
- **外部系统会错误地得出什么结论**：doctor 有结构化输出（✓/✗ 标记、原因、修复建议），但退出码只有 1（失败）或 0（成功）。外部系统无法从退出码判断"哪一项检查失败"，只能 parse stderr 文本。
- **后果**：doctor 的设计初衷是"评测前环境自检"，但外部系统无法自动化使用它——必须 parse 非结构化文本。

### 场景 13：calibrate 坏 session
- **退出码**：1
- **result.json**：未生成
- **stderr 前 800 字**：
  ```
  [TRACE] calibrate target='workbuddy' session=s-doesnotexist-000
  2026-09-18 21:06:38.090 | AgentBay | INFO | ... | 🔗 API Call: GetSession
  2026-09-18 21:06:38.773 | AgentBay | INFO | ... | Session not found: s-doesnotexist-000
  Traceback (most recent call last):
    ...
  ```
- **外部系统会错误地得出什么结论**：calibrate 在 session 不存在时直接抛 traceback，没有结构化错误输出。外部系统只能看到"退出码 1 + 无 result.json"，和工具崩溃一样。
- **后果**：calibrate 失败时，外部系统无法区分"session 配置错误"和"工具内部异常"。

---

## 3. 集成阻塞项清单

### 3.1 退出码语义不明确

**问题**：所有 `run` 子命令的异常路径都返回退出码 1，包括：
- 参数缺失（场景 1、8）
- 输入验证失败（场景 2、3、4、5、6）
- 环境问题（场景 7、9）
- 工具内部 bug

**后果**：外部系统无法从退出码区分"测出结论了（agent_security=FAIL）"、"环境有问题没测成（ENVIRONMENT_INVALID）"、"工具自己崩了"。三者都是退出码 1 + 无 result.json。

**对比契约**：CONTRACT.md §2 定义了 `agent_security` 的三种值：`PASS` / `FAIL` / `ENVIRONMENT_INVALID`，但 CLI 退出码没有对应区分。即使 `result.json` 能表达 `ENVIRONMENT_INVALID`，异常路径根本不生成 `result.json`。

**建议**：
- 退出码 0：成功，result.json 已生成，agent_security 任意值
- 退出码 1：评测完成但 agent_security=FAIL
- 退出码 2：环境问题，评测未完成（ENVIRONMENT_INVALID 或更糟）
- 退出码 3：输入验证失败（case 格式错误、参数缺失）
- 退出码 4：工具内部异常（Python traceback）

### 3.2 异常路径没有结构化输出

**问题**：所有异常路径都依赖 stderr 文本（Python traceback 或中文错误消息），没有结构化 JSON 输出。

**后果**：外部系统必须 parse 非结构化文本才能判断失败原因，而且不同语言/版本的 traceback 格式可能变化。

**对比**：`doctor` 子命令有半结构化输出（✓/✗ 标记），但外部系统仍然需要 parse stderr。

**建议**：
- 所有错误路径都应该生成 `error.json`（或 stderr 输出 JSON），包含 `error_code`、`error_message`、`context`（可选）。
- 或者，`result.json` 始终生成，失败时包含 `error` 字段。

### 3.3 没有 stdout JSON 输出

**问题**：所有结果都通过 `--out` 写入文件，stdout 只用于 help 和进度信息。

**后果**：外部系统必须知道文件路径、等待文件写入完成、处理文件锁。在容器化环境中，文件路径可能受限。

**建议**：
- 增加 `--output-format json` 参数，将 result.json 输出到 stdout。
- 或者，支持 `--out -` 表示输出到 stdout。

### 3.4 `--session` 来源不明确

**问题**：CONTRACT.md §4.1 说 `--session` 优先级最高，但没有说明外部系统如何获取 session_id。文档提到 `session_id` 可以来自：
- 命令行参数 `--session`
- case.json 的 `session_id` 字段
- 环境变量 `TRACE_WB_SESSION`

但外部系统如何**创建** session？文档没有说明。`provision` 子命令可以"在会话内自动安装并启动被测智能体"，但前提是 session 已经存在。

**后果**：外部系统无法自动化创建 session，必须依赖人工介入或外部系统自己的 session 管理逻辑。

**建议**：
- 增加 `create-session` 子命令，返回 session_id。
- 或者，在 `run` 子命令中增加 `--create-session` 参数，自动创建 session。

### 3.5 `doctor` 的结果外部系统无法自动化使用

**问题**：`doctor` 退出码只有 0（全部通过）或 1（至少一项失败），但外部系统无法从退出码判断"哪一项检查失败"。必须 parse stderr 文本。

**后果**：doctor 的设计初衷是"评测前确定性环境自检"，但外部系统无法自动化使用它。无人值守的系统必须先运行 doctor，然后 parse stderr，再决定是否运行 run。

**建议**：
- `doctor` 增加 `--json` 参数，输出结构化 JSON。
- 或者，`doctor` 始终输出 JSON 到 stdout，stderr 用于人类可读的进度信息。

### 3.6 缺少的子命令/参数

**无人值守系统需要的能力**：
1. **创建 session**：当前没有子命令可以创建 session。外部系统必须依赖 AgentBay API 或其他工具。
2. **检查 session 状态**：`doctor` 可以检查，但输出不结构化。
3. **等待 session 就绪**：当前没有参数可以等待 session 就绪（例如，等待智能体启动完成）。
4. **批量运行**：当前 `run` 只能运行单个 case，没有批量调度能力。
5. **取消运行**：当前没有参数可以取消正在运行的评测。
6. **查询运行状态**：当前没有参数可以查询正在运行的评测状态。

**建议**：
- 增加 `create-session`、`list-sessions`、`delete-session` 子命令。
- 增加 `run-batch` 子命令，支持批量运行。
- 增加 `status` 子命令，查询运行状态。
- 增加 `cancel` 子命令，取消运行。

### 3.7 验证顺序不合理

**问题**：场景 4、5、6 显示，工具先检查 session_id，再验证 case schema。这导致 schema 验证错误被掩盖。

**后果**：调用方看到"未提供 session_id"或"Session not found"，而真正的问题是 case 格式错误。

**建议**：
- 先验证 case schema（包括 vector、canary.type），再检查 session_id。
- 这样调用方可以先修复 case 格式问题，再处理 session 配置。

---

## 4. 总结论

**现在这个 CLI，能不能被外部系统无人值守地调用并拿到可信结果？**

**不能。**

最关键的缺失是：**退出码语义不明确，异常路径没有结构化输出**。外部系统无法从退出码 + result.json 区分"测出结论了"、"环境有问题没测成"、"工具自己崩了"。三者都是退出码 1 + 无 result.json。

**最关键的改进**：
1. **明确退出码语义**：0=成功、1=评测完成但 FAIL、2=环境问题、3=输入验证失败、4=工具内部异常。
2. **异常路径生成结构化输出**：所有错误路径都应该生成 `error.json` 或 `result.json`（带 `error` 字段），包含 `error_code`、`error_message`、`context`。

没有这两点，外部系统无法自动化运行 TRACE，必须依赖人工介入 parse stderr 文本。

---

## 附录：原始测试数据

所有原始输出存放在 `/TRACE/trace/.cli-it/` 目录：
- `scenario{N}_stdout.txt`：stdout 全文
- `scenario{N}_stderr.txt`：stderr 全文
- `scenario{N}/result.json`：result.json（如果生成）
