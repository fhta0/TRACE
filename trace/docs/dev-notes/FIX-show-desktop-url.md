# 任务：把沙箱桌面地址打印出来（provision / run 都要）

## 背景

当前 `provision` 装完只提示"请用 `session.info().resource_url` 打开网页桌面登录"，但**没把地址本身打出来**，用户还得自己再写代码调一次 SDK 才能拿到。应该直接打印可点击的地址。

`run` 子命令也该打 —— 跑评测时能实时围观被测 agent 在干什么，调试用例价值很大。

## 已验证的取地址方式（协调方实测过）

```python
info = session.info()
data = getattr(info, "data", info)        # SessionInfo 对象
url = getattr(data, "resource_url", None) # 无影网页桌面地址，Basic 档即可用
```
这个地址是**可交互的网页云桌面**（URL 里带 `input=true&keyboard=true`），打开就能操作、扫码登录。注意 `authcode` 有时效（几十分钟到数小时），过期需重新取。

## 要做的改动（只改 `trace/cli.py`，可在 `trace/provider.py` 加一个小helper）

### 1. 加一个取地址的 helper（放 provider.py 或 cli.py 内部均可）

```python
def get_desktop_url(session) -> str | None:
    """取无影网页桌面地址（可交互，用于观看/扫码登录）。取不到返回 None，不抛错。"""
    try:
        info = session.info()
        data = getattr(info, "data", info)
        return getattr(data, "resource_url", None) or None
    except Exception:
        return None
```
**必须容错**：取不到就返回 None，绝不能因为拿不到地址让 provision/run 失败。

### 2. `provision` 成功后打印

把现在那句提示换成带真实地址的输出，例如：
```
[TRACE] 安装完成。
[TRACE] 打开下面地址（网页云桌面，可交互）扫码登录：
        <resource_url>
[TRACE] 注意：地址内 authcode 有时效，过期后重新运行本命令或用 SDK 重新获取。
[TRACE] 登录完成后，用 run 子命令开始评测。
```
取不到地址时退回原来那句文字提示（说明可用 `session.info().resource_url` 自取）。

### 3. `run` 开始时也打印一次

在 `_cmd_run` 里，拿到 session 之后、开始跑用例之前，打印一行：
```
[TRACE] 沙箱桌面（可实时观看）：<resource_url>
```
取不到就跳过这行，不影响评测。

## 约束
- 只改 `trace/cli.py`（+ 可选在 `trace/provider.py` 加 helper）。
- **不改判定逻辑、oracle、runner、report、target、provision 的安装流程**。
- 取地址失败必须静默降级，绝不能让 run/provision 因此失败。
- 不运行工具、不 pip install、不发网络请求。
- 改完打印：helper 函数全文 + provision 成功分支的输出片段 + run 里新增的那行。
