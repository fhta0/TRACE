"""TRACE CLI 入口：`python -m trace.cli run --case X.json --out Y.json [--session s-xxx]`。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import runner
from .provider import AgentBayProvider, get_desktop_url


def _load_case(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_session_id(args: argparse.Namespace, case: dict) -> str:
    """优先级：--session > case["session_id"] > env TRACE_WB_SESSION。"""
    if args.session:
        return args.session
    if case.get("session_id"):
        return case["session_id"]
    env = os.environ.get("TRACE_WB_SESSION")
    if env:
        return env
    raise SystemExit(
        "错误：未提供 session_id。请通过 --session、case['session_id'] "
        "或环境变量 TRACE_WB_SESSION 指定。"
    )


def _cmd_run(args: argparse.Namespace) -> int:
    case = _load_case(args.case)
    session_id = _resolve_session_id(args, case)

    evidence_dir = args.evidence_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.out)) or ".", "evidence"
    )
    os.makedirs(evidence_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)

    provider = AgentBayProvider()
    session = provider.get_session(session_id)

    desktop_url = get_desktop_url(session)
    if desktop_url:
        sys.stderr.write(f"[TRACE] 沙箱桌面（可实时观看）：{desktop_url}\n")

    result = runner.run_case(case, session, evidence_dir)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    if args.report:
        from . import report
        html_text = report.render_html(case, result)
        report_dir = os.path.dirname(os.path.abspath(args.report))
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(html_text)
        sys.stderr.write(f"[TRACE] report -> {args.report}\n")

    # 简要输出到 stderr，便于人工观测；结构化结果只写文件。
    sys.stderr.write(
        f"[TRACE] {result['id']} runs={result['runs']} "
        f"agent_security={result['agent_security']} "
        f"system_protection={result['system_protection']} "
        f"rate={result['failure_rate']}\n"
    )
    return 0


def _cmd_provision(args: argparse.Namespace) -> int:
    """在会话内自动安装被测智能体到'等登录'状态。provision 不进测量环。"""
    from .target import get_target

    case: dict | None = None
    if args.case:
        case = _load_case(args.case)
        target_name = args.target or case.get("target")
        if not target_name:
            raise SystemExit(
                f"错误：case 文件缺少 'target' 字段：{args.case}"
            )
    elif args.target:
        target_name = args.target
    else:
        raise SystemExit("错误：必须提供 --target 或 --case 之一")

    session_id = _resolve_session_id(args, case or {})

    sys.stderr.write(
        f"[TRACE] provision target={target_name!r} session={session_id}\n"
    )

    provider = AgentBayProvider()
    session = provider.get_session(session_id)
    target = get_target(target_name, session)

    sys.stderr.write("[TRACE] 开始自动安装（后台 bat + flag 轮询）...\n")
    try:
        target.provision()
    except NotImplementedError:
        raise SystemExit(
            f"错误：target {target_name!r} 未实现 provision()，"
            f"请手动安装后使用 run 子命令。"
        )
    except RuntimeError as e:
        sys.stderr.write(f"[TRACE] provision 失败：{e}\n")
        return 1

    desktop_url = get_desktop_url(session)
    if desktop_url:
        sys.stderr.write("[TRACE] 安装完成。\n")
        sys.stderr.write(
            "[TRACE] 打开下面地址（网页云桌面，可交互）扫码登录：\n"
        )
        sys.stderr.write(f"        {desktop_url}\n")
        sys.stderr.write(
            "[TRACE] 注意：地址内 authcode 有时效，"
            "过期后重新运行本命令或用 SDK 重新获取。\n"
        )
        sys.stderr.write("[TRACE] 登录完成后，用 run 子命令开始评测。\n")
    else:
        sys.stderr.write(
            "[TRACE] 安装完成。请用 session.info().resource_url 打开网页桌面手动登录，"
            "然后用 run 子命令评测。\n"
        )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """评测前确定性环境自检。逐项输出 ✓/✗，任一项 ✗ 给出修复建议。

    检查项（按 §FIX-verified-steps）：
      1. 会话可连接
      2. 被测进程在运行（Get-Process）
      3. 目标窗口存在（list_root_windows 匹配标题）
      4. 屏幕参数与 _CALIBRATED_SCREEN 一致
      5. 投递信号可用（prompt-vars 目录存在且可读）
      6. canary 路径可写

    全部通过 → 退出码 0；任一 ✗ → 退出码 1。
    """
    from .target import get_target
    from .target_workbuddy import (
        _CALIBRATED_SCREEN,
        _PROMPT_VARS_DIR,
        WorkBuddyTarget,
    )

    target_name = args.target or "workbuddy"
    session_id = args.session
    if not session_id:
        env = os.environ.get("TRACE_WB_SESSION")
        if env:
            session_id = env
    if not session_id:
        raise SystemExit(
            "错误：doctor 需要 --session 或环境变量 TRACE_WB_SESSION。"
        )

    sys.stderr.write(f"[TRACE] doctor target={target_name!r} session={session_id}\n\n")

    failed: list[str] = []

    # ① 会话可连接
    sys.stderr.write("① 会话可连接 ... ")
    try:
        provider = AgentBayProvider()
        session = provider.get_session(session_id)
        # 触发一次实际调用以确认连接
        _ = session.computer.get_screen_size()
        sys.stderr.write("✓\n")
    except Exception as e:
        sys.stderr.write(f"✗\n   原因：{e}\n   修复：检查 session_id 是否正确、AGENTBAY_API_KEY 是否设置。\n")
        failed.append("session")
        # 后续检查依赖 session，无法继续
        _emit_doctor_summary(failed)
        return 1

    # ② 被测进程在运行
    sys.stderr.write("② 被测进程在运行 ... ")
    try:
        out = session.command.execute_command(
            "powershell -Command \"(Get-Process WorkBuddy -ErrorAction SilentlyContinue).Name\"",
            timeout_ms=30000
        )
        output_text = getattr(out, "output", "") or ""
        success = getattr(out, "success", True)
        snippet = output_text.strip()[:80] if output_text else "(empty)"
        sys.stderr.write(f"[output: {snippet}]\n   ")
        if not success:
            sys.stderr.write("✗\n   原因：命令执行失败。\n   修复：检查会话状态或 PowerShell 可用性。\n")
            failed.append("process")
        elif output_text and "WorkBuddy" in output_text:
            sys.stderr.write("✓\n")
        else:
            sys.stderr.write("✗\n   原因：未发现 WorkBuddy 进程。\n   修复：启动 WorkBuddy 后重试；若未安装，运行 `python -m trace.cli provision`。\n")
            failed.append("process")
    except Exception as e:
        sys.stderr.write(f"✗\n   原因：{e}\n   修复：execute_command 调用失败，可能是会话异常。\n")
        failed.append("process")

    # ③ 目标窗口存在
    sys.stderr.write("③ 目标窗口存在 ... ")
    try:
        r = session.computer.list_root_windows()
        wins = getattr(r, "windows", None) or []
        titles = [str(getattr(w, "title", "")) for w in wins]
        snippet_titles = [t[:40] for t in titles[:5]]
        sys.stderr.write(f"[windows: {snippet_titles}]\n   ")
        if any("WorkBuddy" in t for t in titles):
            sys.stderr.write("✓\n")
        else:
            sys.stderr.write(
                "✗\n   原因：未找到标题含 'WorkBuddy' 的顶层窗口。\n"
                "   修复：确认 WorkBuddy 已登录且主窗口已显示（不是最小化到托盘）。\n"
            )
            failed.append("window")
    except Exception as e:
        sys.stderr.write(
            f"✗\n   原因：{e}\n"
            f"   修复：list_root_windows 调用失败，可能是 SDK 版本过旧或会话异常。\n"
        )
        failed.append("window")

    # ④ 屏幕参数与标定一致
    sys.stderr.write("④ 屏幕参数与标定一致 ... ")
    try:
        sz = session.computer.get_screen_size()
        data = getattr(sz, "data", None)
        if not isinstance(data, dict):
            sys.stderr.write(
                f"✗\n   原因：get_screen_size 返回对象无 data 字段（{sz!r}）。\n"
                f"   修复：检查 SDK 版本。\n"
            )
            failed.append("screen")
        else:
            cur_w = data.get("width")
            cur_h = data.get("height")
            cur_dpi = data.get("dpiScalingFactor") or data.get("dpi") or 1.0
            cal = _CALIBRATED_SCREEN
            sys.stderr.write(
                f"[当前 {cur_w}x{cur_h} DPI{cur_dpi}, "
                f"标定 {cal['width']}x{cal['height']} DPI{cal['dpi']}]\n   "
            )
            if (
                cur_w == cal["width"]
                and cur_h == cal["height"]
                and float(cur_dpi) == float(cal["dpi"])
            ):
                sys.stderr.write("✓\n")
            else:
                # 不一致不判 ✗：坐标可能失准，由投递校验兜底
                sys.stderr.write(
                    "✓（告警：参数与标定不一致，坐标可能失准，由投递校验兜底）\n"
                    f"   当前：{cur_w}x{cur_h} DPI{cur_dpi}\n"
                    f"   标定：{cal['width']}x{cal['height']} DPI{cal['dpi']}\n"
                    f"   修复：若出现 NOT_DELIVERED，调整沙箱分辨率/DPI 与标定一致，"
                    f"或重新标定坐标。\n"
                )
    except Exception as e:
        sys.stderr.write(f"✗\n   原因：{e}\n   修复：get_screen_size 调用失败。\n")
        failed.append("screen")

    # ⑤ 投递信号可用（prompt-vars 目录存在且可读）
    sys.stderr.write("⑤ 投递信号可用（prompt-vars 目录）... ")
    target = get_target(target_name, session)
    if isinstance(target, WorkBuddyTarget):
        try:
            r = session.filesystem.list_directory(_PROMPT_VARS_DIR)
        except FileNotFoundError:
            sys.stderr.write(
                f"✗\n   原因：{_PROMPT_VARS_DIR} 目录不存在。\n"
                f"   修复：确认 WorkBuddy 已至少提交过一次任务"
                f"（首次提交会创建该目录）。\n"
            )
            failed.append("prompt-vars")
        except Exception as e:
            sys.stderr.write(
                f"✗\n   原因：list_directory 调用失败：{e}\n"
                f"   修复：检查 SDK 版本或会话状态。\n"
            )
            failed.append("prompt-vars")
        else:
            success = getattr(r, "success", False)
            if success:
                # 统计文件数（与 _count_prompt_vars 同逻辑）
                entries = getattr(r, "entries", None) or getattr(r, "data", None) or []
                count = 0
                for entry in entries:
                    etype = str(getattr(entry, "type", "") or "").upper()
                    if etype in ("FILE", "[FILE]") or getattr(entry, "is_file", False) or "[FILE]" in str(entry):
                        count += 1
                sys.stderr.write(f"✓（目录存在，{count} 个文件）\n")
            else:
                sys.stderr.write(
                    f"✗\n   原因：{_PROMPT_VARS_DIR} 目录不存在或不可读。\n"
                    f"   修复：确认 WorkBuddy 已至少提交过一次任务"
                    f"（首次提交会创建该目录）。\n"
                )
                failed.append("prompt-vars")
    else:
        sys.stderr.write("⊘（非 WorkBuddy 目标，跳过）\n")

    # ⑥ canary 路径可写
    sys.stderr.write("⑥ canary 路径可写 ... ")
    canary_dir = r"C:\Users\administrator\Desktop"
    canary_path = canary_dir + r"\_trace_canary_probe.tmp"
    try:
        # 写一个临时文件再删掉
        session.filesystem.write_file(canary_path, "probe")
        # 尝试删除（用 execute_command）
        out = session.command.execute_command(f'del "{canary_path}"', timeout_ms=30000)
        output_text = getattr(out, "output", "") or ""
        success = getattr(out, "success", True)
        snippet = output_text.strip()[:80] if output_text else "(empty)"
        sys.stderr.write(f"[output: {snippet}]\n   ")
        if success:
            sys.stderr.write("✓\n")
        else:
            sys.stderr.write("✗\n   原因：删除命令执行失败。\n   修复：检查桌面路径权限。\n")
            failed.append("canary")
    except Exception as e:
        sys.stderr.write(
            f"✗\n   原因：{e}\n"
            f"   修复：确认桌面路径 {canary_dir} 可写；可能是权限问题或路径不存在。\n"
        )
        failed.append("canary")

    _emit_doctor_summary(failed)
    return 1 if failed else 0


def _emit_doctor_summary(failed: list[str]) -> None:
    if failed:
        sys.stderr.write(
            f"\n[TRACE] doctor 完成：{len(failed)} 项未通过 ({', '.join(failed)})。\n"
            f"        请修复上述问题后重新运行 doctor。\n"
        )
    else:
        sys.stderr.write(
            "\n[TRACE] doctor 完成：环境就绪，可以运行评测。\n"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trace", description="TRACE v1 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="运行一个 case")
    pr.add_argument("--case", required=True, help="case.json 路径")
    pr.add_argument("--out", required=True, help="result.json 输出路径")
    pr.add_argument("--session", default=None, help="会话 ID（优先级最高）")
    pr.add_argument(
        "--evidence-dir", default=None,
        help="截图存放目录（默认：out 同级 evidence/）",
    )
    pr.add_argument(
        "--report", default=None,
        help="HTML 报告输出路径（可选）。给出后在 result.json 之外额外渲染一份自包含 HTML 报告。",
    )
    pr.set_defaults(func=_cmd_run)

    pp = sub.add_parser(
        "provision",
        help="在会话内自动安装并启动被测智能体到'等登录'状态（一次性步骤，不进测量环）",
    )
    pp.add_argument(
        "--target", default=None,
        help="target 名称（例如 workbuddy）。与 --case 二选一。",
    )
    pp.add_argument(
        "--case", default=None,
        help="case.json 路径，从中读取 'target' 字段。与 --target 二选一。",
    )
    pp.add_argument(
        "--session", default=None,
        help="会话 ID（优先级最高；缺省按 case['session_id'] → 环境变量 TRACE_WB_SESSION 回退）",
    )
    pp.set_defaults(func=_cmd_provision)

    pd = sub.add_parser(
        "doctor",
        help="评测前确定性环境自检（§FIX-verified-steps）：逐项检查会话/进程/窗口/屏幕/投递信号/canary 路径，任一项 ✗ 给出修复建议。",
    )
    pd.add_argument(
        "--target", default="workbuddy",
        help="target 名称（默认 workbuddy）。",
    )
    pd.add_argument(
        "--session", default=None,
        help="会话 ID（缺省按环境变量 TRACE_WB_SESSION 回退）。",
    )
    pd.set_defaults(func=_cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
