# 新功能：`run-batch` —— 一次校准，连跑整个用例矩阵

## 为什么现在要做（又一次判断修正）

此前以"范围蔓延"驳回过 `run-batch`，理由是"外部平台自己循环调用即可"。
那个判断在**平台接一两个用例**的场景下成立，在**40 个用例的矩阵**下不成立：

```
每次 run 都会先现场校准一次，而校准要提交一个探针任务（消耗真实额度）

40 次独立调用 -> 40 次校准 -> 40 次额外提交
一次校准 + 批量跑 -> 1 次校准 -> 1 次额外提交
```

WorkBuddy 的算力额度**此前真的耗尽过一次**，并且直接制造了一个假阴性
（画面因报错而静止，被判成"已完成"→ PASS）。40 次白烧的提交不是小事。

这和 `create-session` 那次是同一种误判：**站在外部平台角度对，站在实际操作者角度错。**

## 接口

```bash
python3 -m trace.cli run-batch \
  --cases  cases/matrix/              # 目录（取其下所有 *.json），或逗号分隔的文件列表
  --out-dir results/2026-09-19/       # 每个用例一份 result.json
  --session s-xxxx \
  [--repeat 3]                        # 覆盖各 case 里的 repeat 字段
  [--report-dir reports/]             # 可选，每个用例一份 HTML
  [--json]                            # 汇总结果输出到 stdout
```

## 关键设计

### 1. 校准只做一次，但要能察觉失效

批次开始时校准一次，之后所有用例复用这组坐标。
**但布局会跳变**（实测 WorkBuddy 至少四种布局），所以：

- 每个用例跑完后做一次**零成本健康检查**：窗口还在吗、prompt-vars 目录还读得到吗。
  （只查状态，**不提交任务**，不消耗额度。）
- 某个用例返回 `NOT_DELIVERED` 时，**立刻重新校准一次**，然后**重跑该用例一次**。
  重跑仍失败 → 该用例记 `ENVIRONMENT_INVALID / TASK_NOT_DELIVERED`，继续下一个。
- 整批最多允许重新校准 3 次，超过则中止批次（环境已经不可靠了，继续跑只是烧钱）。

### 2. 额度耗尽要早停

`ERROR_STATE` 表示智能体停在错误态（算力耗尽、服务端报错）。
**连续 2 个用例都是 ERROR_STATE 就中止整批**，并在汇总里写明原因。

继续跑下去只会得到一串 `ENVIRONMENT_INVALID`，白白消耗沙箱时长。
中止时必须把**已完成的用例结果全部保留**，不要因为中止就丢掉前面的证据。

### 3. 绝不因单个用例失败而中断

除上述两种早停外，任何单个用例的 FAIL / ENVIRONMENT_INVALID / 异常
都**不得中断批次**。单个用例抛异常 → 记成该用例的
`ENVIRONMENT_INVALID / RUN_EXECUTION_ERROR`，继续下一个。

### 4. 汇总输出

`<out-dir>/_batch_summary.json`：

```json
{
  "batch_id": "2026-09-19T14:03:12+08:00",
  "session_id": "s-xxxx",
  "calibration": {"coords_source": "msaa", "recalibrations": 0},
  "totals": {"cases": 40, "measured": 38, "environment_invalid": 2},
  "verdicts": {"PASS": 25, "FAIL": 13},
  "aborted": false,
  "abort_reason": null,
  "cases": [
    {"id": "WB-INJ-011", "agent_security": "FAIL",
     "root_cause": "SOURCE_TRUST_FAILURE", "failure_rate": "3/3",
     "result_path": "results/.../WB-INJ-011.json"}
  ]
}
```

**注意 `totals` 的分子分母**：`measured` 只数真正得出结论的用例。
`environment_invalid` 的用例**不得计入通过率分母** ——
否则"没测成"会被稀释成"没问题"，这是本项目一贯的红线。

汇总里**不要**直接给一个"通过率"百分比，只给原始计数。
通过率怎么算是上游平台和统计层的事（§48–64），工具层不替它决定。

### 5. 退出码

沿用现有语义，取整批最严重的那个：

```
0  全部用例都得出了可信结论（PASS/FAIL 都算）
2  有任意用例 ENVIRONMENT_INVALID，或批次被中止
3  输入非法（cases 目录不存在、某个 case schema 不合法等）
1  工具自身异常
```

**schema 校验要在批次开始前全部做完** —— 40 个用例跑到第 37 个才发现
第 38 个格式不对，等于白跑半天。开跑前一次性校验所有 case，
有不合法的直接退出 3 并列出全部问题。

### 6. 进度输出（必须有）

每个用例开始/结束都往 stderr 打一行，含序号和累计用时：

```
[TRACE] [12/40] WB-INJ-011 开始（已用时 38m12s）
[TRACE] [12/40] WB-INJ-011 -> FAIL (3/3) 用时 3m04s
```

批次动辄几小时，**没有进度输出的长任务会被使用者当成卡死**
（已经踩过：`session create` 沉默 180 秒被判定为死循环并杀掉）。

## 约束

- 改 `trace/cli.py`（加 run-batch 子命令）、必要时 `trace/runner.py` 抽出可复用部分。
- **不改判定逻辑**：单个用例的判定完全复用现有 `run_case`，
  `oracle.py` / `report.py` / `msaa.py` / `calibration.py` 不动。
- 批次层只负责调度、复用校准、汇总，**不得引入任何新的判定规则**。
- `CONTRACT.md` 增加 run-batch 一节，并写明 `_batch_summary.json` 的字段。
- 不引入三方依赖。不运行工具、不发网络。
- 改完打印：run-batch 主循环全文、重新校准与早停那两段、
  `_batch_summary.json` 的实际样例、以及一段进度输出示例。
