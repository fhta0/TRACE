#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证：WorkBuddy 登录态能否跨 AgentBay 会话迁移（决定"温会话池"方案是否成立）

原理：
  1) 连上当前那个已登录的 WorkBuddy 会话，把登录凭据目录同步进持久化 context 后销毁它；
  2) 开一个"全新"会话，只挂载 wb-data context（不含程序目录）；
  3) 检查凭据文件有没有随 context 过来；
  4) 从 CDN 直链重装 WorkBuddy（程序目录太大不做持久化），把凭据放回原位；
  5) 启动 WorkBuddy，截图。停在登录页=迁移失败；进主界面=迁移成功。

用法：
  pip install wuying-agentbay-sdk
  export AGENTBAY_API_KEY='你的key'
  python3 verify_login_persist.py
截图会存到当前目录：v1_fresh.png / v2_after_launch.png
"""
import os, sys, time, base64

try:
    from agentbay import (AgentBay, CreateSessionParams, ContextSync,
                          SyncPolicy, LifecyclePolicy)
except ImportError:
    sys.exit("先装 SDK：pip install wuying-agentbay-sdk")

KEY = os.environ.get("AGENTBAY_API_KEY")
if not KEY:
    sys.exit("先设置：export AGENTBAY_API_KEY='...'")

CDN = ("https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/"
       "WorkBuddy-win32-x64-user-5.5.6.38337834-5f969292.exe")
# 登录凭据真实位置（Local，未被任何 context 覆盖），迁移前已被复制进 wb-data 的 _cbe 子目录
AUTH_LIVE = r"C:\Users\administrator\AppData\Local\CodeBuddyExtension"
DATA_CTX  = r"C:\Users\administrator\AppData\Roaming\WorkBuddy"   # 挂 wb-data，_cbe 在这下面
OLD_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".wb_session")  # 若与 wb2.py 同目录会自动读到旧会话id

ab = AgentBay(api_key=KEY)

def run_ps(sess, script, timeout_ms=280000):
    """把 PowerShell 脚本写文件再执行，避开参数长度限制"""
    sess.filesystem.write_file(r"C:\Users\Public\v.ps1", script)
    r = sess.command.execute_command(
        "powershell -ExecutionPolicy Bypass -File C:\\Users\\Public\\v.ps1",
        timeout_ms=timeout_ms)
    return (r.output or "").strip()

def shot(sess, path):
    r = sess.computer.beta_take_screenshot()
    d = r.data if isinstance(r.data, bytes) else base64.b64decode(r.data)
    open(path, "wb").write(d)
    print(f"  截图已存 -> {path} ({len(d)} bytes)")

# ---------- 步骤 1：同步销毁旧会话（触发 wb-data 上行）----------
print("步骤 1：同步并销毁当前 WorkBuddy 会话（让凭据 context 上传）")
old_id = None
if os.path.exists(OLD_SESSION_FILE):
    old_id = open(OLD_SESSION_FILE).read().strip()
if old_id:
    try:
        g = ab.get(old_id)
        s_old = getattr(g, "session", None)
        if s_old:
            ab.delete(s_old, sync_context=True)   # ★关键：sync_context 让 _cbe 上传
            print(f"  旧会话 {old_id} 已同步销毁")
        else:
            print(f"  旧会话 {old_id} 已不存在（可能已被回收），继续")
    except Exception as e:
        print(f"  处理旧会话出错（忽略，继续）: {e}")
else:
    print("  没找到旧会话id文件，假设 wb-data context 已含凭据，继续")
time.sleep(20)

# ---------- 步骤 2：开全新会话，只挂 wb-data ----------
print("步骤 2：创建全新会话，挂载 wb-data context（不含程序目录）")
data_ctx_id = ab.context.get("wb-data", create=True).context.id
r = ab.create(CreateSessionParams(
    image_id="windows_latest",
    context_syncs=[ContextSync(data_ctx_id, DATA_CTX, SyncPolicy.default())],
    lifecycle_policy=LifecyclePolicy(manual_release=True),
    labels={"t": "verify-login"}))
if not r.success:
    sys.exit(f"  创建失败：{r.error_message}")
s = r.session
print(f"  新会话 = {s.session_id}")
shot(s, "v1_fresh.png")

# ---------- 步骤 3：确认凭据随 context 过来了 ----------
print("步骤 3：检查凭据文件是否随 context 迁移过来")
out = run_ps(s, r'''
$ErrorActionPreference="SilentlyContinue"
$f="C:\Users\administrator\AppData\Roaming\WorkBuddy\_cbe\Data\Public\auth\workbuddy-desktop.info"
if(Test-Path $f){ "AUTH_PRESENT size=" + (Get-Item $f).Length }
else { "AUTH_MISSING" }
''')
print("  ->", out)
if "AUTH_MISSING" in out:
    print("  ✗ 凭据没迁移过来。可能 context 没同步成功，或该文件被平台特殊处理。")
    print("    结论：会话池方案需另想办法（每用例重登，或换 beta_context_mounts）。")
    print(f"    会话 {s.session_id} 保留，供你排查。")
    sys.exit(0)

# ---------- 步骤 4：重装 WorkBuddy + 把凭据放回原位 ----------
print("步骤 4：从 CDN 重装 WorkBuddy（后台），并把凭据还原到 Local")
run_ps(s, f'''
$ErrorActionPreference="SilentlyContinue"
# 凭据还原：_cbe -> Local\\CodeBuddyExtension
$src="C:\\Users\\administrator\\AppData\\Roaming\\WorkBuddy\\_cbe\\*"
$dst="{AUTH_LIVE}"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item -Path $src -Destination $dst -Recurse -Force
# 后台下载+静默安装
$bat=@'
@echo off
curl -L -o C:\\Users\\Public\\wb.exe "{CDN}"
C:\\Users\\Public\\wb.exe /S
echo DONE> C:\\Users\\Public\\wb.flag
'@
Set-Content -Path C:\\Users\\Public\\inst.bat -Value $bat -Encoding ASCII
Start-Process -FilePath C:\\Users\\Public\\inst.bat -WindowStyle Hidden
"install started"
''')
print("  下载+安装约需 8-12 分钟，轮询中（期间保持心跳）...")
installed = False
for i in range(30):   # 最多 ~15 分钟
    time.sleep(30)
    try: s.keep_alive()
    except Exception: pass
    st = run_ps(s, r'''
$p="C:\Users\administrator\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"
if(Test-Path C:\Users\Public\wb.flag){ if(Test-Path $p){"READY"}else{"FLAG_NO_EXE"} }
else { "..." }
''', timeout_ms=120000)
    print(f"    +{(i+1)*30}s: {st}")
    if "READY" in st:
        installed = True; break
if not installed:
    print("  ✗ 安装未在预期时间内完成，会话保留供排查：", s.session_id)
    sys.exit(0)

# ---------- 步骤 5：启动 WorkBuddy，看是否免登录 ----------
print("步骤 5：启动 WorkBuddy 并截图")
run_ps(s, r'''
Start-Process "C:\Users\administrator\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"
Start-Sleep -Seconds 25
"launched"
''')
# 把窗口拉到前台
try:
    ws = s.computer.list_root_windows()
    wb = [w for w in (getattr(ws, "windows", []) or [])
          if "WorkBuddy" in str(getattr(w, "title", ""))]
    if wb:
        s.computer.activate_window(wb[0].window_id)
        s.computer.maximize_window(wb[0].window_id)
        time.sleep(4)
except Exception as e:
    print("  (窗口前置失败，忽略)", e)
shot(s, "v2_after_launch.png")

print("""
================= 判读方法 =================
打开 v2_after_launch.png：
  • 看到「登录」按钮 / 微信扫码       -> 迁移失败：凭据绑了本机，会话池需重登
  • 直接进主界面（有"新建任务/助理"）  -> 迁移成功：会话池方案成立！★
===========================================
""")
print(f"会话 {s.session_id} 已保留（manual_release）。看完后手动删除以省费用：")
print(f'  python3 -c "import os;from agentbay import AgentBay;'
      f"ab=AgentBay(api_key=os.environ['AGENTBAY_API_KEY']);"
      f'print(ab.delete(ab.get(\\"{s.session_id}\\").session).success)"')
