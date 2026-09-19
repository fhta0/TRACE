# 验收测试：TRACE 作为 CLI 被外部系统调用

## 你的角色

你**不是 TRACE 的开发者**。这一轮请你扮演**上游测评平台的集成工程师**：
你拿到了 `CONTRACT.md` 和一个命令行工具，要把它接进自己的系统。

你的系统是这样调用它的：

```python
proc = subprocess.run([...], capture_output=True)
if proc.returncode == 0:
    result = json.load(open(out_path))
    # 按 result["agent_security"] 入库、算通过率、出报表
else:
    # 报警：这次评测没跑成
```

**所以你只能用两样东西做判断：`returncode` 和 `result.json`。**

明确禁止（违反即本轮测试作废）：
- 不许看截图判断发生了什么
- 不许读 stderr 的文字来推断结论（stderr 可以**记录**下来，但不能作为判据）
- 不许读 TRACE 的源码来推断它内部干了什么

你要回答的唯一问题：

> **每一种情况下，外部系统能不能只凭 `returncode` + `result.json`，
> 正确区分这三件事：①测出结论了 ②环境有问题没测成 ③工具自己崩了**

## 本轮不碰真沙箱

本轮**不创建、不连接任何真实沙箱会话**（省钱）。全部用非法/不存在的参数触发各条错误路径。
真机多次运行是下一轮，由协调方安排登录后再做。

## 环境

```
cd /TRACE/trace
python3 -m trace.cli ...        # 用 /usr/bin/python3，已装 wuying-agentbay-sdk
```

工作文件一律放 `/TRACE/trace/.cli-it/`（该目录已在 .gitignore 外，测完你不用清理，
但**绝对不要动 `/TRACE` 下的任何其他文件**——那是宿主机的真实仓库）。

## 测试矩阵

逐条执行，每条都记录：**退出码**、**result.json 是否生成**、**若生成则其内容**、
**stdout 全文**、**stderr 前 800 字**（仅记录）。

| # | 场景 | 构造方式 |
|---|---|---|
| 1 | 不给 session | `run --case <合法case> --out <path>`，且不设 `TRACE_WB_SESSION` |
| 2 | case 文件不存在 | `run --case /TRACE/trace/.cli-it/nope.json --out <path>` |
| 3 | case 不是合法 JSON | 写一个内容为 `{这不是json` 的文件 |
| 4 | case 缺必填字段 | 合法 JSON，但删掉 `canary` 字段 |
| 5 | vector 不支持 | 合法 case，把 `vector` 改成 `"api_injection"` |
| 6 | canary.type 不支持 | 合法 case，把 `canary.type` 改成 `"registry_key"` |
| 7 | session 不存在 | `--session s-doesnotexist-000`，`AGENTBAY_API_KEY` 设为下面给的真 key |
| 8 | API key 缺失 | 合法 case + `--session s-xxx`，但 **不设** `AGENTBAY_API_KEY` |
| 9 | out 路径不可写 | `--out /proc/nope/result.json` |
| 10 | 无子命令 | `python3 -m trace.cli` |
| 11 | help | `python3 -m trace.cli --help` 和 `run --help` |
| 12 | doctor 坏 session | `doctor --session s-doesnotexist-000`（带真 key） |
| 13 | calibrate 坏 session | `calibrate --session s-doesnotexist-000`（带真 key） |

第 7/12/13 条用的 API key（只读探测，不会创建会话、不产生费用）：
```
AGENTBAY_API_KEY=akm-b7993fc5-c916-495c-b254-6ca225161cd2
```

合法 case 用 `/TRACE/trace/cases/wb_inj_003.json` 拷贝出来改。

## 你要产出的东西

写到 `/TRACE/trace/docs/dev-notes/REPORT-cli-integration.md`，包含：

### 1. 结果表

| # | 场景 | 退出码 | result.json | 外部系统能正确归类吗？ |
|---|---|---|---|---|

最后一列只填三种：`能` / `不能——会误判成成功` / `不能——只能看出失败但分不清原因`。

### 2. 逐条说明「不能」的那些

对每条判「不能」的，写清楚：外部系统会**错误地**得出什么结论，后果是什么。
例：「退出码 1 + 无 result.json，和工具 bug 完全无法区分，平台会把环境问题记成工具故障」。

### 3. 集成阻塞项清单

站在集成工程师立场，列出**你认为接不进去或很别扭的地方**。我特别想知道：

- 退出码语义是否明确？`agent_security=FAIL`（测出来智能体不安全）和
  `ENVIRONMENT_INVALID`（没测成）退出码一样吗？一样的话外部系统怎么分？
- 异常路径有没有结构化输出？还是只能 parse Python traceback？
- 有没有办法**不读文件**直接拿到结果（stdout JSON）？
- `--session` 从哪来？外部系统能自己创建会话吗？契约里说清楚了吗？
- `doctor` 的结果外部系统怎么用？只有退出码够吗？
- 还缺什么子命令/参数，才能让一个无人值守的系统完整跑一轮？

### 4. 你的总结论

一句话回答：**现在这个 CLI，能不能被外部系统无人值守地调用并拿到可信结果？**
如果不能，缺的最关键的一件是什么。

## 约束

- **只读测试，不要修任何代码。** 发现问题写进报告，不要动手改——
  退出码语义是接口决策，由协调方定。
- 不要创建真实沙箱会话，不要跑 `provision`。
- 不要动 `/TRACE` 下除 `.cli-it/` 和你那份报告之外的任何文件。
- 每条命令都加超时（`timeout 120 ...`），避免卡住。
- 报告里每一条结论都要**附上你实际观测到的退出码和输出**，不要凭源码推断。
