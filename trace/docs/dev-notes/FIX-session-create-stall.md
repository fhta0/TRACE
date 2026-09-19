# 修正：session create 空转 180 秒 —— 规格错了，不是实现错了

## 实测证据

验收阶段一卡在第一条命令。ds-Claude 报告"`session create --json` 不退出，
陷入每 2 秒一次的无限循环"，在 120 秒时手动杀掉。

实际不是无限循环 —— `_wait_screen_stable` 的超时是 180 秒，它只是没跑完。
但诊断下去发现了**真正的 bug，而且根源是规格写错了**：

```
会话 s-04owxy77hkpo3nl4u（创建后已过一段时间）
doctor 读数：1024x768 DPI1.0
```

`1024x768` **不是"启动早期的过渡值"**。回看上一个会话：
创建时 1024x768，**用户扫码打开云桌面之后**才变成 1920x1060。
分辨率是在**有观看端接入**时才切换的。

而规格给的判据是「连续两次读数一致**且 width > 1024**」。
`1024 > 1024` 为假，所以在没人连云桌面之前这个条件**永远不成立**，
必然空转满 180 秒再报 `ENVIRONMENT_INVALID`。

**实现是忠实按规格写的，是规格错了。**
（原始错误推断记在 `FINDING-uiautomation.md`「附带确认的两件事」第 1 条，
 已在该文件就地更正。）

## 修法

### 1. 去掉 `width > 1024` 这个门槛

`_SCREEN_STABLE_MIN_WIDTH` 整个删掉。稳定判据只保留**连续两次读数一致**，
不管值是多少。1024x768 是合法的稳定态，只是还没有观看端接入而已。

### 2. 返回值里说明这一点

`session create --json` 的输出增加一个字段：

```json
{
  "session_id": "...",
  "screen": {"width": 1024, "height": 768, "dpi": 1.0},
  "desktop_url": "...",
  "stable": true,
  "screen_note": "当前无观看端接入；打开云桌面地址后分辨率通常会变化，
                  校准应在登录后进行"
}
```

非 JSON 模式在 stderr 给等价提示。**这很重要** —— 否则下一个人又会把
1024x768 当成异常。

### 3. 轮询期间必须有进度输出（本次真正的教训）

现在 `--json` 模式下整整 180 秒**一行输出都没有**。
对能力较弱的调用方来说，**沉默和卡死无法区分** —— ds-Claude 正是据此
判定"无限循环"并杀掉进程的。它的判断在信息不足的情况下是合理的。

每次轮询往 stderr 打一行（`--json` 模式也要打，JSON 只走 stdout）：

```
[TRACE] 等待屏幕参数稳定 12s/180s，当前读数 1024x768 DPI1.0
```

这条和下载看门狗那条是同一个道理，之前只在 provision 里落实了，
**没有推广到其他长耗时命令**。请检查 CLI 里所有可能长时间无输出的地方
（至少 `provision`、`session create`、`calibrate`、`run`），
确保都有周期性进度输出。

### 4. 超时文案要说清楚下一步

超时时不要只说"未稳定"，要写明：会话已创建、正在计费、session_id 是什么、
用哪条命令删，以及"若只是想拿地址扫码登录，可以忽略本超时，
直接用 `session url <id>`"。

## 顺带：`python` vs `python3`

同一轮验收发现容器里只有 `python3`，没有 `python`，而 `SKILL.md` 与各处文档
写的都是 `python -m trace.cli`。

把 `SKILL.md`、`README.md`、`CONTRACT.md` 里的命令示例统一改成 `python3 -m trace.cli`，
并在 SKILL.md 开头加一句：「若环境只有 `python` 没有 `python3`，反之替换即可」。

## 约束

- 只改 `trace/provider.py`、`trace/cli.py`、`SKILL.md`、`README.md`、`CONTRACT.md`，
  以及 `FINDING-uiautomation.md` 里那条错误推断（就地更正，注明原判断错在哪，
  不要删掉——错误推断本身有参考价值）。
- 不改判定逻辑：`runner.py` / `oracle.py` / `report.py` / `target_*.py` / `msaa.py` 不动。
- 改完打印：新的稳定判据代码、进度输出示例、超时文案示例。
