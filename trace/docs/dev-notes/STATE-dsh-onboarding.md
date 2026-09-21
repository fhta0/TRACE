# deepseek-harness 接入现状（诚实盘点）

## ✅ 2026-09-21：工具执行阻塞已攻克，deepseek 真机端到端可测

**最终可用配置（全部真机验证：tool_call+tool_result、turn_end=completed、文件真被创建）：**
1. **provision 改为装发行版**：`npm install -g @deepseek-ai/dsh@0.1.6-alpha.2`
   —— **不再 clone+build**（几十秒）。发行版列表见 GitHub releases；npm 的 `latest`
   dist-tag 是旧的 `0.1.5-rc.2`（无 `--json`），要显式指定 `@0.1.6-alpha.2`（有 `--json`）。
2. **dispatch 用全局 `dsh` + 关内层沙箱**：
   `export DSH_PERMISSION_MODE=danger-full-access && dsh --profile headless --json "<task>"`
   —— **不是** repo 的 `pnpm dsh`（那个工具运行时坏，见下）。
3. **plant_doc** 放到 agent 的 cwd（dispatch 用 `cd /root`，就放 `/root/<file>`）。
4. 投递校验（--json 事件流解析）不用改，0.1.6 的 turn_end 是 `completed`。

**攻克链条（每步真机验证）**：
- exit-1 探因 → 发现工具调用 turn 以 error 收场（空 PASS）。
- repo `pnpm dsh --json`：工具运行时调度器 undefined（`reading 'prepare'`）——
  **从仓库工作区跑用 loadProfileDirectory，composition 坏，弃用**。
- 发布版 `dsh`（npm i -g）：工具运行时好、会正确初始化 `~/.dsh/profiles/headless`，
  但工具被**内层沙箱**拦：`sandbox mode "workspace-write" ... no sandbox backend usable
  (install bubblewrap / Landlock kernel)`。AgentBay 容器无 bubblewrap、内核 5.10 无 Landlock。
- **根因 + 解法**：`base/cordis.patch.yml` 的 sandbox-policy `mode: process.env.DSH_PERMISSION_MODE
  ?? 'workspace-write'`。设 **`DSH_PERMISSION_MODE=danger-full-access`** → 跳过内层沙箱
  （`terminal-controller`：`if (policy.mode !== 'danger-full-access')` 才 confine）。
  我们本就在 AgentBay 隔离沙箱里，不需要 harness 再套一层，danger-full-access 正当。
- 装 `@0.1.6-alpha.2`（发行版，有 --json）+ danger-full-access → 全绿。

**下一步**：按上述改适配器（provision 简化为 npm i -g release、dispatch 用全局 dsh + env、
plant_doc 改 /root），再真机 smoke 一次拿到第一个**真实**的 deepseek 判定。

---

## 历史记录（下面是攻克前的盘点，保留备查）

一句话：**基础设施做完了、投递校验现在是诚实的，但 harness agent 在我们的 headless
调用下工具执行报错、干不了活 —— deepseek 还没被真正测到。**（← 此问题已于 2026-09-21 解决，见上。）

## 已完成且经审核（都在 wip/deepseek-harness）

- 适配器 Linux 版（aio-ubuntu-2404）：provision（corepack→clone→install→build，
  长命令后台+轮询）、plant_doc、dispatch。
- dispatch 投递校验用 headless `--json` 事件流 + 退出码，且**已修三个真机暴露的洞**：
  1. exit-code 假阴性（dsh 退出1=跑了但报错，别丢成 NOT_DELIVERED）→ raise_on_fail 参数。
  2. corepack 回退门槛（该镜像无 corepack）→ 只看 `pnpm --version` 实际能不能跑。
  3. `_dsh_ready` 跨进程假门槛（provision/run 是两次 CLI 调用）→ 去掉。
  4. **turn 以 error 收场判 ERROR_STATE，不再出空 PASS**（见下）。
- 真机验证：harness 在 Linux **build 通**；TRACE 全链（provision→plant→dispatch→投递校验→canary）
  端到端跑通。
- §21 干净 Linux 用例 `cases/DSH-INJ-101.json`（metadata 伪装，canary=/tmp/workspace_ready.marker）。

## 阻塞：harness 工具运行时未接上（agent 干不了活）

真机冒烟：agent 一调工具（读文档）就崩，turn/end 的 reason 是：
```
kind:"error", message:"Cannot read properties of undefined (reading 'prepare')", code:UNKNOWN
```
出错点 `packages/core/agent-loop/src/tool-calls.ts:170`：
`ctx.tools[TOOL_RUNTIME_SCHEDULER].prepare(call.exec)` —— `ctx.tools[TOOL_RUNTIME_SCHEDULER]`
是 undefined。

**这意味着**：agent 读不了注入文档、也没能力写 canary。之前那次 canary CLEAN 得到的 PASS
是**空的**（不是"守住了"，是"啥也没干成"）。已修投递校验（turn error→ERROR_STATE），
所以现在会如实报"没测到"，不再骗人；但 deepseek 要能真正测，必须解决这个工具运行时问题。

### 静态查到的线索（供下次接手）

- `TOOL_RUNTIME_SCHEDULER`（`packages/core/tools/src/index.ts:463`）是 ToolRuntime 上的
  只读属性，**在 index.ts:798 有默认值** —— 理论上不该 undefined。
- 但有 PTC 路径：`packages/core/tools/src/ptc.ts:550` 用另一个
  `registry[TOOL_RUNTIME_SCHEDULER]`。运行时 undefined 多半是**装配/PTC 模式**层面。
- harness 自己的 headless 快照测试（`snapshots/session/headless.snapshot.ts`，有
  `ptc: true/false` 场景）能正常跑带工具的 run —— **对照它的装配方式，就是我们缺的东西**。

### 下次接手的诊断路径（需沙箱）

1. 沙箱里 provision 后，先跑一个**不需要工具**的任务（如"回答1+1"）：
   - 正常 exit 0 → 模型/基础通，问题专在工具执行。
   - 也报错 → 模型/凭证配置问题（DEEPSEEK_API_KEY 之外可能还要 base_url/model 配置）。
2. 再跑要工具的任务，带 verbose/看完整 stderr 栈，定位 scheduler 为何 undefined。
3. 对照 headless.snapshot.ts 的装配（是否要某 provider 插件 / 某 profile 配置 / PTC 开关）。
4. 也可能是 `--depth 1` 克隆到的 main 某 commit 的问题，可试固定 tag。

## 其它待办（非阻塞）

- 证据 blob 存成 `.png`（runner 层统一后缀，cosmetic）。
- 删旧 `cases/dsh_inj_001.json`（违 §21+Windows，已被 DSH-INJ-101 取代）。
- session create 对 deepseek 要显式 `--image aio-ubuntu-2404`（可加 target→镜像关联）。
- `wip/deepseek-harness` 合回 main 时，provision.py 已与 main 分叉（main 是 WorkBuddy 验证版）。
