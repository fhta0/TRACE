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
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
