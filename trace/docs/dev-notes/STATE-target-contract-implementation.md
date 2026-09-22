# 实现归档：Target 契约 + 参数优先级链 + 用例可选 + 汇总报告（5 个 Stage）

日期：2026-09-22。设计见 `DESIGN-target-contract.md`。分工：机制/装配交 ds-Claude（qwen3.7-plus，
模拟发行方式），判定层全程锁死、Opus 逐 stage 审计；质量/关键件 Opus 直接做。全部在
`wip/deepseek-harness`，已 push origin。

## 第一原则（贯穿）

**机制开放、判定锁死。** `runner.py` 的判定（run_case/_run_once/_aggregate）+ `oracle.py` 全程一字未动，
每个 stage 都验证过"判定层未被碰"。作者能改"怎么喂、怎么问"，不能改"怎么判"——这是评测可信的根。

## 五个 Stage（实现 + 审计结论 + 提交）

| Stage | 内容 | 实现 | 审计结论 | 提交 |
|---|---|---|---|---|
| 1a | Target 元数据契约（IMAGE_ID/OS/DEFAULT_CASES/DISPLAY_NAME）+ 注册表（懒构造）+ target_meta/known_targets/validate_meta | ds-Claude | AST×3、两 target 元数据正确、validate_meta 捕获缺失、未知 target 拒绝、无循环导入 | 3b56b4e |
| 1b | `session create --target` 据表选镜像（优先级 --image>target>windows_latest 兜底）+ provision 通用 OS 守卫（uname 探测≠target.OS 即 fail-fast） | ds-Claude | AST、--help 有 --target、守卫在 provision() 之前、判定层未动 | 9bca59a |
| 2a | 9 个 deepseek 用例加 title/suite | Opus（数据填充） | 9 个 JSON 合法、判定字段未动 | 3b(见 test 提交) |
| 2b | `cases list` 菜单 + run-batch 按 --target/--suite/--ids 选（优先级 --cases>--ids>--suite>--target 全部） | ds-Claude | 菜单 9 行、suite=round-3→301/302/303、ids→2、all→9、缺选择/坏 id fail-fast、判定层未动 | 887ee27 |
| 5 | `report.render_batch_summary` + run-batch `--summary-report`（自包含 HTML，双结论+破防率+总览计数，明暗主题） | Opus | 渲染器合成数据单测通过、AST、--help 有 --summary-report、判定层未动 | 24a9109 |
| 4 | skill/README 改为 target 契约（给 target 名即可、运行评测节、适配器加元数据声明）+ `.env.example` + `.gitignore` 让 skill 随仓库 | Opus | skill 已 tracked 且含新内容（origin 一致）、settings 仍忽略 | 3cbb726 / 7a3d418 |
| 3 | 抽 `HeadlessCliTarget` 家族基类（_run_long_cmd/_run_cmd/plant_doc 上移，deepseek 继承） | ds-Claude | 纯搬迁：deepseek 保留方法 diff 仅 import+类声明、搬移方法关键行 verbatim、MRO/方法解析正确、AST×2、workbuddy/注册表/判定层未动 | 3022036 |

## 落成的使用契约

给 target 名即可：`session create --target X`（自动镜像/OS）→ `provision --target X`（OS 守卫兜底）
→ `cases list --target X`（菜单）→ `run-batch --target X [--suite S | --ids a,b] --summary-report r.html`
（跑+汇总报告）→ `session rm`。参数优先级链：**显式 > target 默认 > 交互问用户 / headless fail-fast**。

## 接入未来智能体（第 3、4 个）

写 `target_<名>.py`：继承 `HeadlessCliTarget`（CLI 类）或 `Target`，声明 4 个元数据 + 实现
provision/plant_doc/dispatch 三个钩子（plant_doc/长命令 CLI 类可复用基类）；注册表 `_build_registry()` 加两行。
**CLI/判定/报告/skill 一字不改。**

## 已知延后

- **GuiDesktopTarget 家族基类**（容纳 WorkBuddy 的坐标/点击/截图）未抽——WorkBuddy 是已验证的关键 GUI 路径，
  单成员、重构收益低风险高，留待接第 2 个 GUI 智能体时再抽。
- WorkBuddy 用例（cases/matrix/）未加 title/suite；`cases list` 对缺字段已优雅回退（显示 "-"）。

## 相关

设计 [[DESIGN-target-contract]]；破防结论见 FINDING-dsh-round3-FIRST-BREAK / 深跑 301 FAIL 10/10、303 FAIL 4/9；
盲判 oracle 修复见 STATE-harness-fixes-and-oracle-backed-rerun。判定优先级修复（破防优先于环境无效）提交 570b484。
