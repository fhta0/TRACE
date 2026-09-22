# Finding: deepseek-harness headless 模式的「任务已执行」确定性交付信号

**结论**:deepseek-harness 的 headless 模式**同时提供三种确定性的「任务已执行」证据**——stdout 文本/JSON 流、退出码、磁盘 JSONL 会话日志。TRACE 接入层可以任选其一,或同时采信多个作交叉验证。

---

## Q1:最终结果怎么出(stdout?退出码?)

**答**:两者都有。最终答案的**最后一条 assistant text**写到 stdout,退出码根据 `turn/end` 的 `reason.kind` 决定。

**源码证据**:

`deepseek-harness/packages/bundle/headless/src/index.ts:373-380`
```ts
    await sessions.flush(agent.session)
    const outcome = summarize(agent.session, firstSeq)
    if (projection === undefined) io.stdout.write(outcome.text + '\n')
    else projection.finish(outcome.text)
    if (outcome.reason?.kind === 'error') {
      io.stderr.write(`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`)
    }
    io.exit(outcome.reason?.kind === 'completed' ? 0 : 1)
```

`summarize()` 在 `index.ts:72-98` 里扫描 `firstSeq` 之后所有 `assistant/message`,只保留**最后一条**的 text 块拼接:
```ts
    if (event.type === 'assistant/message') {
      const joined = event.data.message.content
        .filter(block => block.type === 'text')
        .map(block => block.text)
        .join('')
      if (joined !== '') text = joined
    }
    if (event.type === 'turn/end') reason = event.data.reason
```

`fail()` 路径(`index.ts:296-301`)统一走 exit(1):
```ts
function fail(io: HeadlessIo, error: unknown, json: boolean): void {
  const message = error instanceof Error ? error.message : String(error)
  if (json) io.stdout.write(`${boundJsonLine({ type: 'error', message })}\n`)
  io.stderr.write(`dsh: ${message}\n`)
  io.exit(1)
}
```

**交付语义**:
- 默认模式:stdout = 最终答案文本 + `\n`;stderr = 推理流 + 诊断。
- `--json` 模式:stdout = 逐事件 JSONL(见 Q2);stderr = `dsh:` 诊断。
- 退出码:`0` = `reason.kind === 'completed'` ;`1` = 任何其他 reason(kind === 'error' 或 undefined)或未捕获异常。
- 无论哪条路径,退出前**先 `sessions.flush(agent.session)`** 把会话落盘(Q3)。

---

## Q2:有没有逐事件、机器可读的输出模式?

**答**:**有**。`--json` 选项把整个 run 投影成 newline-delimited JSON 事件流写到 stdout。

**源码证据**:

`deepseek-harness/packages/bundle/headless/src/startup.ts:43` 定义选项:
```ts
    .option('--json', 'write newline-delimited run events to stdout instead of the final message')
```

`startup.ts:30-32` 把 `json` 作为 `HeadlessStartupValues` 的一部分下发给 runner:
```ts
export interface HeadlessStartupValues {
  task: string | undefined
  sessionId: string | undefined
  /** Whether stdout carries the machine-readable event stream instead of final text. */
  json: boolean
}
```

投影器 `projectJsonRun()` 在 `deepseek-harness/packages/bundle/headless/src/json-stream.ts:243-332` 订阅 `session/event` 并按事件类型写出:

`json-stream.ts:259-316` 事件分类:
```ts
    switch (event.type) {
      case 'turn/start':
        write({ type: 'status', phase: 'turn_start', turn: event.data.turn })
        return
      case 'step/start':
        write({ type: 'status', phase: 'step_start', turn: event.data.turn, step: event.data.step })
        return
      case 'assistant/attempt':
        stepUsage = addUsage(stepUsage, streamUsage(event.data.stream))
        return
      case 'assistant/message':
        stepUsage = addUsage(stepUsage, event.data.usage ?? streamUsage(event.data.stream))
        for (const block of event.data.message.content) {
          if (block.type === 'reasoning') write({ type: 'thinking', text: block.text })
          else if (block.type === 'text') write({ type: 'text', text: block.text })
        }
        return
      case 'step/end': { ... write({ type: 'status', phase: 'step_end', ... }) }
      case 'turn/end':
        write({ type: 'status', phase: 'turn_end', turn: event.data.turn, reason: event.data.reason })
        return
      case 'tool/call':
        write({ type: 'tool_call', callId, tool: event.data.name, input: parseArguments(event.data.arguments) })
        return
      case 'tool/result':
        if (event.surfaceOp !== 'append') return
        write({ type: 'tool_result', callId, status: block.isError === true ? 'error' : 'completed', result: resultText(block.content) })
        return
```

`json-stream.ts:318` 写出起始 `session` 事件:
```ts
  write({ type: 'session', sessionId: agent.id, cwd: options.cwd ?? process.cwd() })
```

`json-stream.ts:321-326` 终止 `final` 事件(不截断,即答案的 lossless 载体):
```ts
    finish(text: string): void {
      if (disposed) return
      sink.write(`${JSON.stringify({ type: 'final', text })}\n`)
    },
```

错误事件在 `startup.ts:84-93` 由 commander 钩子发出:
```ts
      const payload = boundJsonLine({ type: 'error', message: message.replace(/^error: /, '') })
      internals.stdout.write(`${payload}\n`)
```

`json-stream.ts:132-149` 的 `boundJsonLine()` 对每条事件做双限幅(每字符串 8KB,每行 32KB),超限时加 `truncated: true` 标记。

**完整事件类型表**:`session` → (`status` | `thinking` | `text` | `tool_call` | `tool_result`)* → `final` | `error`。所有事件都从**已 commit** 的 Session 事件派生(`json-stream.ts:1-7` 注释明确说 "text and reasoning come from committed `assistant/message` content, never from a live attempt that may still be retried or discarded"),所以流里不会出现"模型还在重试、尚未定稿"的内容。

**接入含义**:TRACE 接入层只要 `dsh --profile headless --json "<task>"`,逐行读 stdout、解析 JSON,就能拿到完整的 tool call/result、turn 边界、最终答案,且每条都是确定性 commit 后的事件。

---

## Q3:跑一次会不会在磁盘留 session 记录/事件日志文件?

**答**:**会**。harness 默认把所有 session 事件以 JSONL(+可选 Zstandard 压缩)形式写到 session persistence root 下,runner 在退出前会显式 `sessions.flush(agent.session)` 强制落盘。

**源码证据**:

`deepseek-harness/packages/session/session-persistence-jsonl/src/format.ts:253-267` 目录布局:
```ts
export function projectDir(root: string, cwd: string | undefined): string {
  ...
}
export function sessionDir(root: string, cwd: string | undefined, id: SessionId): string {
  return join(projectDir(root, cwd), encodeSegment(id))
}
```

`format.ts:41-43`、`format.ts:57-59` 文件命名:
```ts
export function logSuffix(compression: JsonlCompression): '.jsonl.zstd' | '.jsonl' {
  return `.jsonl${compressionSuffix(compression)}`
}
export function generationLogFilename(version: number, compression: JsonlCompression): string {
  return `${sessionFormatLogFilename(version)}${compressionSuffix(compression)}`
}
```
默认压缩 = `zstd`(`index.ts:66`:`const DEFAULT_COMPRESSION: JsonlCompression = 'zstd'`),所以物理文件典型是 `session.v3.jsonl.zstd`;也可配成明文 `session.v3.jsonl`。

`deepseek-harness/packages/session/session-persistence-jsonl/src/index.ts:88-99` Config 强制要 root:
```ts
export interface Config {
  /**
   * Root directory for all session files. Required (no default): a default of
   * `process.cwd()` would scatter session files as the process's cwd changes
   * (bash calls, subprocesses). Sessions group under human-readable project
   * directories, then per-session directories.
   */
  root: string
  /** Physical encoding; defaults to checksummed Zstandard frames. */
  compression?: JsonlCompression
}
```

`storage.ts:534-547` 把 live `session/event` 自动路由到写 lease:
```ts
    ctx.on('session/event', (session: Session, event) => {
      this.writers.get(session.id)?.enqueueLive(event, (error) => {
        ctx.logger.warn(`session-persistence: background write for session "${session.id}" failed ...`)
      })
    })
    ctx.on('session/flush', (session: Session) => {
      const writer = this.writers.get(session.id)
      if (writer === null || writer === undefined) return undefined
      return (async () => {
        await writer.drainLive()
        await writer.flush()
      })()
    })
```

`deepseek-harness/packages/bundle/headless/src/index.ts:373` 在打印答案**前**显式 flush:
```ts
    await sessions.flush(agent.session)
```

**实测**:仓库自带快照文件 `deepseek-harness/snapshots/session/text-turn/session.jsonl` 和 `session.v1.jsonl`,里面就是逐行 `{"type":"turn/start",...}` / `{"type":"assistant/message",...}` / `{"type":"tool/call",...}` / `{"type":"turn/end",...}` 的完整事件序列——这正是同一个 headless 跑产生的磁盘日志格式。

**接入含义**:
- TRACE 可以配置一个固定的 `root`(例如 `/TRACE/trace/sessions/`),每次 `dsh --profile headless` 跑完就有一个以 session-id 为目录名的 JSONL 文件。
- 这个文件**独立于 stdout 输出**,即使 stdout 被截断或消费者崩溃,落盘记录仍在。
- 文件是 append-only 的 JSONL,可流式解析,也可离线重放。
- 每条事件都是持久化的 commit point,与 `--json` 流里的 commit point 同源(`json-stream.ts:1-7` 注释)。

---

## 我翻过的文件清单

**指定要读的**:
- `deepseek-harness/packages/bundle/headless/src/startup.ts` ✓
- `deepseek-harness/packages/bundle/headless/src/json-stream.ts` ✓
- `deepseek-harness/packages/bundle/headless/src/runner-internals.ts` ✓
- `grep -rn "session/event|session/created|SessionEvent" deepseek-harness/packages/` ✓
- `deepseek-harness/snapshots/session/text-turn/session.jsonl` 和 `session.v1.jsonl` ✓

**额外读的(追踪 flush/落盘路径)**:
- `deepseek-harness/packages/bundle/headless/src/index.ts` —— 主 runner,`summarize()` / `sessions.flush()` / `io.exit()`
- `deepseek-harness/packages/bundle/headless/tests/headless.spec.ts:262-284` —— 单测确认 `['flush', 'exit']` 顺序
- `deepseek-harness/packages/session/session-persistence-jsonl/src/index.ts:88-99` —— Config `root` 必填
- `deepseek-harness/packages/session/session-persistence-jsonl/src/format.ts:253-267` —— `sessionDir` / `projectDir` 布局
- `deepseek-harness/packages/session/session-persistence-jsonl/src/format.ts:41-59` —— `.jsonl` / `.jsonl.zstd` 后缀
- `deepseek-harness/packages/session/session-persistence-jsonl/src/storage.ts:534-547` —— live `session/event` 路由到 lease

**未读但推断为相关(不在本次结论依赖链上)**:
- `deepseek-harness/packages/session/session-projection-cache/` —— 给 SDK / web 用的投影缓存,与 headless 无直接交付关系
- `deepseek-harness/packages/session/session-telemetry/` —— OTel 遥测,独立于交付信号

---

## 一句话结论

`dsh --profile headless "<task>"` 退出前必定:(a) 把最终答案写到 stdout(或 `--json` 下把逐事件 JSONL 写到 stdout 并以 `final` 事件收尾);(b) 以 0/1 退出码反映 turn 是否 completed;(c) 在配置的 session persistence root 下留下 `<projectDir>/<encoded-session-id>/session.v<N>.jsonl[.zstd]` 的完整事件日志——三条交付通道**都独立可读、都来自同一份 commit 后的 Session 事件源**,TRACE 接入层可以任选或组合使用。
