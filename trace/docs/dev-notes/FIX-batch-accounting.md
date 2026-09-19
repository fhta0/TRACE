# 修正：批次中止时未跑的用例从账上消失 + coords_source 取错字段

打桩验证 `run-batch` 时发现（无沙箱，stub 掉 `run_case`）。

## 缺陷一：账对不上

5 个用例的批次，在第 5 个之前因中止结束，汇总是：

```json
"totals": { "cases": 5, "measured": 3, "environment_invalid": 1 },
"aborted": true,
"abort_reason": "MAX_RECALIBRATIONS_EXCEEDED"
```

```
measured(3) + environment_invalid(1) = 4  !=  cases(5)
```

第 5 个用例根本没跑，而**汇总里没有任何字段记录这件事**。
上游平台按 `cases` 算覆盖率，会以为 5 个都跑过了。

这正是本项目一贯要防的那件事 —— **让"没测到"消失在一个数字里**，
只是这次发生在批次层而不是单用例层。和
「ENVIRONMENT_INVALID 不得计入通过率分母」是同一条原则的延伸。

### 修法

`totals` 增加 `not_run`，并保证恒等式成立：

```json
"totals": {
  "cases": 5,
  "measured": 3,
  "environment_invalid": 1,
  "not_run": 1
}
```

**写入前断言** `cases == measured + environment_invalid + not_run`，
不成立就抛异常 —— 账目对不上是严重问题，不能静默产出。

同时在汇总顶层增加未跑用例的清单，让调用方知道少了哪些：

```json
"not_run_case_ids": ["C5"]
```

中止时（两处 `aborted = True` 的分支，以及 `break` 之后）
都要把剩余未跑的用例 id 收集进来。

`CONTRACT.md` 的 run-batch 一节补上这两个字段，并写明那条恒等式。

## 缺陷二：`coords_source` 取错字段

```python
coords_source = retry_cal.get("verified_by") or coords_source   # 错
```

标定结果里：
- `coords_source` 取值 `"msaa"` / `"search"` —— 这才是"坐标怎么来的"
- `verified_by` 取值 `"prompt_vars_count_increment"` —— 这是"怎么验证的"

取错之后，一旦发生重新校准，汇总里的 `coords_source` 就会变成
`"prompt_vars_count_increment"`，而这个字段的唯一用途正是
**事后判断这批评测用的是哪条定位路径**（MSAA 还是穷举搜索）。

两处都改成：

```python
coords_source = retry_cal.get("coords_source") or coords_source
```

（批次初始校准那一处也检查一遍，别只改重校准的两处。）

## 顺带澄清一个非缺陷

同一次打桩里出现 `recalibrations: 3`，一度以为是误触发。
实际是：每个用例跑完后的健康检查在假 session 下必然失败，
从而触发重新校准 —— **打桩产物，不是真 bug**。健康检查逻辑本身没问题。

记在这里是为了避免下次有人看到同样现象再查一遍。

## 约束

- 只改 `trace/cli.py`（run-batch 的汇总与 coords_source 赋值）、`CONTRACT.md`。
- 不改判定逻辑、不改 runner/oracle/calibration/msaa。
- 改完打印：`totals` 构造与断言那段、未跑用例收集那段、
  三处 coords_source 赋值、以及一份中止批次的 `_batch_summary.json` 样例。
