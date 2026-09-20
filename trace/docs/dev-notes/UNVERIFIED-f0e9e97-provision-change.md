# 待复验：f0e9e97 对 WorkBuddy provision 的改动（未经我验证）

`f0e9e97`（2026-09-20，ds-Claude 在被直接驱动时提交）改了 provision 的安装环节，
现在坐在 `main` tip 上。**它没经过"故障态 + 真机"验证，别默认信它。**

## 它改了什么

- `provision.py`：安装从"`start /B` 后台 + 立刻打 DONE flag"改成
  "**前台等待 + 查退出码**，失败即 `exit /b`"；安装路径加 `/S /D=<用户目录>`。
- `target_workbuddy.py`：`install_dir` 强制到
  `C:\Users\Administrator\AppData\Local\Programs\WorkBuddy`，避开 Program Files。

理由（见 `wip/deepseek-harness` 分支上的 `issue_provision_failed.md`）：
ds-Claude 声称 NSIS `/S` 在沙箱里返回 EXITCODE=5（ERROR_ACCESS_DENIED），
装不进 Program Files。

## 为什么标"待复验"而不是直接信

- **我 2026-09-19 那次 provision 是成功的，用的是改动前的版本**，装到默认位置没报
  EXITCODE=5。所以那个失败要么是 ds-Claude 环境/更早版本特有，要么分析有偏差。
- 改成**前台 `execute_command` 等待**安装，对慢安装器可能重新引入
  execute_command 的单命令时长上限超时——正是"后台+看门狗"当初要避开的
  （见 `FIX-download-watchdog.md`）。ds-Claude 自己的 issue note 也记了
  "cmd /c 前台运行超时(5min)"。

## 下次起沙箱时怎么裁决（在故障态验证后再定）

1. 用 **main 当前版本**（含 f0e9e97）跑一次 `provision --target workbuddy`：
   - 装机成功、无超时 → f0e9e97 可保留。
   - 出现 EXITCODE=5 或前台等待超时 → 说明它没解决问题甚至引入新问题。
2. 若要回退，`git revert f0e9e97` 即恢复 2026-09-19 已验证的装机路径
   （下载看门狗那套）。ds-Claude 的"装用户目录"想法不会丢，历史里有。

在做出上述验证之前，**不要在 f0e9e97 基础上继续叠 WorkBuddy 相关改动**，
以免把未验证的地基越垒越高。相关：[[verify-in-failure-state]]
