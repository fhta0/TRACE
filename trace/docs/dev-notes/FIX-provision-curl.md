# 修正：provision 下载 curl 缺少 stall 检测/重试（导致偶发无限挂起）

## 实测发现（协调方真机诊断）

第二次 provision 超时。诊断：curl 进程在跑，但安装包**停在 0 字节挂了 20 分钟**——连接 stall 了。
杀掉后用带超时的 curl 探测：**30 秒下载 354MB（≈11.8 MB/s）**，CDN 本身飞快。
结论：不是网络慢、不是代码逻辑 bug，是**下载 curl 没带 stall 检测/重试标志，偶发的连接挂起会无限卡住**，直到轮询超时。（首次运行恰好没 stall，所以装成功了。）

## 修法（只改 `trace/provision.py` 的 `_build_bat_body` 下载行）

给 curl 加健壮性标志：连接超时、低速 stall 检测、自动重试、断点续传。

当前下载行大致是：
```python
lines.append(f'curl -L -o "{tmp_path}" "{url}"')
```
改成（加这些标志）：
```python
lines.append(
    f'curl -L --retry 5 --retry-delay 5 --retry-all-errors '
    f'--connect-timeout 30 --speed-limit 10000 --speed-time 30 '
    f'-C - -o "{tmp_path}" "{url}"'
)
```
标志含义（写进注释）：
- `--connect-timeout 30`：30 秒连不上就失败（触发重试）。
- `--speed-limit 10000 --speed-time 30`：下载速度低于 10KB/s 持续 30 秒即判为 stall、中断（触发重试）。这正是本次 0 字节挂起要抓的情况。
- `--retry 5 --retry-delay 5 --retry-all-errors`：失败最多重试 5 次，每次隔 5 秒。
- `-C -`：断点续传，重试时不从头下。

（只改这一行下载命令；winget/zip 分支不涉及 curl，不动。）

## 可选（belt-and-suspenders）
把 `install()` 的默认 `timeout_s` 从 1200 提到 1800（给极慢但在下的网络更多余量）。curl 标志才是真正的修复，这个只是保险。可改可不改，改了在注释里说明。

## 约束
- 只改 `trace/provision.py`。不改判定逻辑、oracle、report、target、cli、runner。
- 不运行、不安装、不发网络。
- 改完打印：修改后的 curl 下载行完整内容 + （若改了）新的默认 timeout_s。
