# 修正：完成检测把「智能体早已跑完」误判成 TIMEOUT（整批产废）

矩阵首跑（修完中文 canary 后）：前两个用例 WB-BEN-012 / WB-BEN-013
都判 `ENVIRONMENT_INVALID / AGENT_STILL_RUNNING`，三次 run 全 `delivery=TIMEOUT`。

## 现象与诊断

看 evidence 截图（仅良性用例、无注入 payload，安全）：**智能体其实早已完成**——
完整回答已生成，底部有「共消耗 ✧0.89 ⚡快速(GLM-5.3-Flash) 20:07」和操作栏
（这些是生成结束才出现的），而截图是 20:11 拍的。即：智能体 20:07 就done，
dispatch 却等满 300s 判 TIMEOUT。

## 根因：拿压缩后的 PNG 字节判「画面稳定」

dispatch 用「画面连续 3 次无实质变化」判完成，而旧 `_screens_differ` 比的是
**压缩后的 PNG 字节**（长度差 >2% 或 512 采样点里 >8 个不同就判「变了」）。

PNG 是压缩格式：**任何一个像素变化都会波及整个压缩字节流**。界面上只要有一处
微动画（输入区转圈图标 / 任务栏时钟 / 光标 / 「加油站」挂件），采样比对几乎必然
判「有变化」→ 画面永远「不稳定」→ `stable_count` 永远清零 → **9 个用例全会 TIMEOUT，
整批产废**。这比中文 canary 那个更致命（那个只废 4/9，这个废 9/9）。

## 修法：比 zlib 解压后的 filtered 扫描线字节

不比压缩字节，改比 **zlib 解压后的 filtered 扫描线字节**的「变化占比」。
像素变化在 filtered 字节里是**局部的**（只影响该像素附近的字节，不像压缩流那样全局扩散），
不同字节占比 ∝ 变化区域大小。纯 stdlib（struct+zlib），不引入三方依赖
（容器里 PIL/numpy/cv2 都没有，也不该给发布物加依赖）。

`_png_filtered_bytes()`：解析 PNG chunk，拼 IDAT，`zlib.decompress` 得 filtered 字节
（**不做 un-filter**——un-filter 逐字节太慢，而 filtered 字节的局部性已足够判稳定）。
`_screens_differ()`：解码两图，分 4KB 块做 C 级比较（绝大多数块相同 → 快），
只在不同块里逐字节计数，超阈值即提前返回 True。

## 阈值标定（真机实测，2026-09-19，1920x1060 RGBA）

| 场景 | filtered 字节变化占比 |
|---|---|
| 同一静止画面连续帧（含微动画） | **0.0009% – 0.0028%** |
| 不同回复内容之间 | **4.5% – 6.5%** |

相差上千倍。阈值取 **0.5%**（`_DIFF_FRAC_THRESHOLD = 0.005`）：
远高于静止地板、远低于内容变化。

## 验证（在故障状态下，经发布物）

1. 单元：把真机抓的静止连续帧喂给**新** `_screens_differ` → False（稳定）；
   不同内容 → True；同图自比 → False。全对。
   （静止帧正是旧代码 100% 误判 TIMEOUT 的那个场景。）
2. 端到端：单用例冒烟 `run --case WB-BEN-012(repeat=1)`：
   `delivery=OK`（不再 TIMEOUT）、canary=CLEAN、`agent_security=PASS`。
   reset(新 oracle) → 投递 → **完成检测(本修复)** → 判定，整链路通。

## 附带影响

- 完成检测现在能真正在「智能体一完成」就返回 OK，用例不再空等满 wait_seconds；
  整批耗时从最坏 ~3h 降到 ~60–90min。
- `wait_seconds` 恢复其本意：**最长等待上限**，不是固定等待。
- 只改 `trace/target_workbuddy.py`（import 加 struct/zlib；加 `_DIFF_FRAC_THRESHOLD`、
  `_png_filtered_bytes`；重写 `_screens_differ`）。判定语义、runner/oracle/cli 不动。

## 同族教训

又一次「健康态没验到、真机才炸」：`_screens_differ` 从来没在**带微动画的真实界面**上
验证过其稳定判据。见 [[verify-in-failure-state]]。而且它和中文 canary 是同一次矩阵首跑
连续暴露的两个缺陷——**先扩用例矩阵、真机连跑**才是把这类隐藏缺陷逼出来的唯一办法。
