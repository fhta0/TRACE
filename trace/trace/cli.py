"""TRACE CLI 入口：`python3 -m trace.cli run --case X.json --out Y.json [--session s-xxx]`。
若环境只有 `python` 没有 `python3`，反之替换即可。"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import threading
import time
import traceback
from typing import Any

from . import runner
from .provider import AgentBayProvider, get_desktop_url


# --- 退出码常量 ---
EXIT_SUCCESS = 0           # 测量完成，结论可信（PASS/FAIL 都是 0）
EXIT_ENVIRONMENT_INVALID = 2  # 环境无效，没测成
EXIT_INPUT_INVALID = 3     # 输入非法（case schema / 参数 / 文件问题）
EXIT_INTERNAL_ERROR = 1    # 工具自身异常


def _load_case(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"CASE_FILE_NOT_FOUND: case 文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"CASE_JSON_INVALID: case 文件不是合法 JSON：{path} ({e})")


def _validate_case(case: dict) -> None:
    """集中校验 case schema（§FIX-cli-contract 第三节第 2 步）。

    校验字段：id / target / vector / doc_filename / doc_content /
              benign_task / canary（且 canary.type 与 canary.path 都在）。
    失败抛出 ValueError，消息以 error.code 开头。
    """
    required_fields = ["id", "target", "vector", "doc_filename", "doc_content", "benign_task", "canary"]
    for field in required_fields:
        if field not in case:
            raise ValueError(f"CASE_SCHEMA_INVALID: case 缺少必填字段：{field}")

    if case.get("vector") not in ("doc_injection", "benign_control"):
        raise ValueError(
            f"UNSUPPORTED_VECTOR: v1 支持 vector ∈ {{doc_injection, benign_control}}，"
            f"收到 {case.get('vector')!r}"
        )

    canary = case.get("canary")
    if not isinstance(canary, dict):
        raise ValueError("CASE_SCHEMA_INVALID: canary 必须是对象")
    if "type" not in canary:
        raise ValueError("CASE_SCHEMA_INVALID: canary 缺少必填字段：type")
    if "path" not in canary:
        raise ValueError("CASE_SCHEMA_INVALID: canary 缺少必填字段：path")
    if canary.get("type") != "file_exists":
        raise ValueError(f"UNSUPPORTED_CANARY_TYPE: v1 仅支持 canary.type=file_exists，收到 {canary.get('type')!r}")


def _resolve_session_id(args: argparse.Namespace, case: dict) -> str:
    """优先级：--session > case["session_id"] > env TRACE_WB_SESSION。"""
    if args.session:
        return args.session
    if case.get("session_id"):
        return case["session_id"]
    env = os.environ.get("TRACE_WB_SESSION")
    if env:
        return env
    raise ValueError("SESSION_NOT_PROVIDED: 未提供 session_id。请通过 --session、case['session_id'] 或环境变量 TRACE_WB_SESSION 指定。")


def _error_result(case_id: str | None, error_code: str, error_message: str) -> dict:
    """构建失败时的 result.json 结构（§FIX-cli-contract 第二节）。"""
    return {
        "id": case_id,
        "runs": 0,
        "agent_security": "NOT_RUN",
        "system_protection": "N/A",
        "failure_rate": "0/0",
        "root_cause": None,
        "verdict_basis": None,
        "error": {"code": error_code, "message": error_message},
        "evidence": [],
    }


def _write_result(out_path: str, result: dict) -> bool:
    """写入 result.json。支持 `--out -` 输出到 stdout。
    返回 True 表示写入成功，False 表示 OUT_PATH_UNWRITABLE（此时已打印到 stdout）。
    """
    if out_path == "-":
        # 输出到 stdout，所有人类可读信息必须走 stderr
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return True

    try:
        out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return True
    except (OSError, IOError) as e:
        # OUT_PATH_UNWRITABLE：把 JSON 打到 stdout，stderr 说明
        sys.stderr.write(f"[TRACE] ⚠ 无法写入 {out_path}：{e}，结果将输出到 stdout\n")
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return False


def _extract_error_code(msg: str) -> str:
    """从异常消息中提取 error.code（冒号前的部分）。"""
    if ":" in msg:
        return msg.split(":", 1)[0].strip()
    return "INTERNAL_ERROR"


def _cmd_run(args: argparse.Namespace) -> int:
    """运行一次评测。异常映射到退出码，任何路径都生成 result.json。

    校验顺序（§FIX-cli-contract 第三节）：
      1. 读 case 文件           → CASE_FILE_NOT_FOUND / CASE_JSON_INVALID
      2. 校验 case schema       → CASE_SCHEMA_INVALID / UNSUPPORTED_*
      3. 解析 session_id        → SESSION_NOT_PROVIDED
      4. 准备输出目录            → OUT_PATH_UNWRITABLE
      5. 连接 AgentBay           → API_KEY_MISSING / SESSION_NOT_FOUND
      6. 跑用例
    """
    case_id: str | None = None

    try:
        # 1. 读 case 文件
        case = _load_case(args.case)
        case_id = case.get("id")

        # 2. 校验 case schema
        _validate_case(case)

        # 3. 解析 session_id
        session_id = _resolve_session_id(args, case)

        # 4. 准备输出目录（提前检查，不联网）
        out_path = args.out
        if out_path != "-":
            out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
            try:
                os.makedirs(out_dir, exist_ok=True)
                # 测试是否可写
                test_file = os.path.join(out_dir, ".trace_write_test")
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
            except (OSError, IOError) as e:
                err_result = _error_result(case_id, "OUT_PATH_UNWRITABLE", f"无法写入输出目录：{e}")
                _write_result(out_path, err_result)
                return EXIT_INPUT_INVALID

        evidence_dir = args.evidence_dir or os.path.join(
            os.path.dirname(os.path.abspath(out_path)) if out_path != "-" else ".", "evidence"
        )
        if out_path != "-":
            os.makedirs(evidence_dir, exist_ok=True)

        # 5. 连接 AgentBay
        if not os.environ.get("AGENTBAY_API_KEY"):
            err_result = _error_result(case_id, "API_KEY_MISSING", "环境变量 AGENTBAY_API_KEY 未设置")
            _write_result(out_path, err_result)
            return EXIT_INPUT_INVALID

        provider = AgentBayProvider()
        try:
            session = provider.get_session(session_id)
        except Exception as e:
            err_msg = str(e)
            if "not found" in err_msg.lower() or "session" in err_msg.lower():
                err_code = "SESSION_NOT_FOUND"
                exit_code = EXIT_ENVIRONMENT_INVALID
            else:
                err_code = "INTERNAL_ERROR"
                exit_code = EXIT_INTERNAL_ERROR
            err_result = _error_result(case_id, err_code, f"获取会话失败：{err_msg}")
            _write_result(out_path, err_result)
            return exit_code

        desktop_url = get_desktop_url(session)
        if desktop_url:
            sys.stderr.write(f"[TRACE] 沙箱桌面（可实时观看）：{desktop_url}\n")

        # 6. 跑用例
        result = runner.run_case(case, session, evidence_dir, auto_calibrate=args.auto_calibrate)

        _write_result(out_path, result)

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

        # 结论 -> 退出码。关键：PASS 和 FAIL 都是 0（都是一次成功的测量），
        # 但 ENVIRONMENT_INVALID 必须是非 0 —— 那是“没测成”，不是结论。
        # 若这里返回 0，外部平台会把“没测成”计入有效样本，
        # 三次 ENVIRONMENT_INVALID 就变成“3 次评测、0 次失败、通过率 100%”。
        verdict = result.get("agent_security")
        if verdict in ("PASS", "FAIL"):
            return EXIT_SUCCESS
        if verdict == "ENVIRONMENT_INVALID":
            sys.stderr.write(
                f"[TRACE] 环境无效，本次未产出可信结论"
                f"（root_cause={result.get('root_cause')}），"
                f"退出码 {EXIT_ENVIRONMENT_INVALID}。\n"
            )
            return EXIT_ENVIRONMENT_INVALID
        sys.stderr.write(
            f"[TRACE] 未知的 agent_security 取值：{verdict!r}，按工具内部异常处理。\n"
        )
        return EXIT_INTERNAL_ERROR

    except (FileNotFoundError, ValueError) as e:
        # 输入类错误（退出码 3）
        err_msg = str(e)
        err_code = _extract_error_code(err_msg)
        err_result = _error_result(case_id, err_code, err_msg)
        if out_path := getattr(args, "out", None):
            _write_result(out_path, err_result)
        else:
            json.dump(err_result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        return EXIT_INPUT_INVALID

    except Exception as e:
        # 其他未捕获异常（退出码 1）
        err_msg = str(e)
        err_result = _error_result(case_id, "INTERNAL_ERROR", f"未预期的异常：{err_msg}")
        if out_path := getattr(args, "out", None):
            _write_result(out_path, err_result)
        else:
            json.dump(err_result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        # traceback 写 stderr 供人排查
        sys.stderr.write(f"[TRACE] ✗ 内部异常：{err_msg}\n")
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL_ERROR


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

    # 通用 OS 守卫：会话真实 OS 必须与 target 声明的 OS 一致，否则 fail-fast。
    # 不针对任何具体 target——从 target.OS 读期望，探针查实际。
    expected_os = target.OS  # "linux" | "windows"
    detected_os = "windows"
    try:
        r = session.command.execute_command("uname -s")
        out = str(getattr(r, "output", None) or getattr(r, "data", None) or r)
        if "Linux" in out:
            detected_os = "linux"
    except Exception:
        detected_os = "windows"   # uname 失败通常意味着非 Linux（如 Windows）
    if expected_os and detected_os != expected_os:
        raise SystemExit(
            f"环境不匹配：目标 {target_name} 需要 {expected_os} 镜像"
            f"（{target.IMAGE_ID}），当前会话探测为 {detected_os}。"
            f"请用 `session create --target {target_name}` 重建会话。"
        )

    sys.stderr.write("[TRACE] 开始自动安装（后台 bat + flag 轮询）...\n")
    sys.stderr.write(
        "[TRACE] 提示：下载 507MB 需要数分钟；期间会持续打印进度。\n"
    )
    sys.stderr.flush()

    # 把 target.provision() 放到后台线程，主线程每 20 秒打一次心跳。
    # 沉默和卡死对弱模型调用方无法区分，必须让进度可见。
    provision_error: list[BaseException | None] = [None]

    def _run_provision() -> None:
        try:
            target.provision()
        except BaseException as e:  # noqa: BLE001
            provision_error[0] = e

    t = threading.Thread(target=_run_provision, daemon=True)
    t_start = time.monotonic()
    t.start()
    while t.is_alive():
        t.join(timeout=20)
        if t.is_alive():
            elapsed = time.monotonic() - t_start
            sys.stderr.write(
                f"[TRACE] 安装进行中 {elapsed:.0f}s ...（下载/安装仍在继续）\n"
            )
            sys.stderr.flush()
    t.join()

    if provision_error[0] is not None:
        e = provision_error[0]
        if isinstance(e, NotImplementedError):
            raise SystemExit(
                f"错误：target {target_name!r} 未实现 provision()，"
                f"请手动安装后使用 run 子命令。"
            )
        if isinstance(e, RuntimeError):
            sys.stderr.write(f"[TRACE] provision 失败：{e}\n")
            return 1
        # 其他异常（不该发生）
        sys.stderr.write(f"[TRACE] provision 异常：{e}\n")
        traceback.print_exc(file=sys.stderr)
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


def _cmd_calibrate(args: argparse.Namespace) -> int:
    """自校准：搜索候选坐标并用 prompt-vars 铁证验证，存盘标定 JSON。

    核心原则（§FEAT-self-calibration）：
      不让模型看截图定位按钮——模型的视觉定位系统性偏小（发送按钮偏 527px）。
      改为在候选区域搜索，用我们已经有的确定性信号（prompt-vars 文件数增加）
      逐个验证，找到第一组有效坐标就立刻停止（每次成功提交消耗真实额度）。

    校准允许操作 UI，但不得引入任何 LLM 判断——全部靠 prompt-vars 铁证。
    校准只在准备阶段运行，不得在 run 的测量环里自动触发。
    """
    from . import calibration as _cal
    from .target import get_target

    target_name = args.target or "workbuddy"
    session_id = args.session
    if not session_id:
        env = os.environ.get("TRACE_WB_SESSION")
        if env:
            session_id = env
    if not session_id:
        raise SystemExit(
            "错误：calibrate 需要 --session 或环境变量 TRACE_WB_SESSION。"
        )

    sys.stderr.write(
        f"[TRACE] calibrate target={target_name!r} session={session_id}\n"
    )

    provider = AgentBayProvider()
    session = provider.get_session(session_id)
    target = get_target(target_name, session)

    # Guard: only WorkBuddy is supported for now (the verification signal
    # — prompt-vars count — is WorkBuddy-specific).
    from .target_workbuddy import WorkBuddyTarget
    if not isinstance(target, WorkBuddyTarget):
        raise SystemExit(
            f"错误：calibrate 当前仅支持 target=workbuddy（"
            f"验证信号 prompt-vars 是 WorkBuddy 特有的）；收到 {target_name!r}。"
        )

    # Pre-flight: prompt-vars signal must be available for verification.
    n0 = target._count_prompt_vars()
    if n0 < 0:
        sys.stderr.write(
            "[TRACE] ⚠ prompt-vars 目录不可读；第一次提交会创建它，"
            "但若目录本身缺失则无法验证标定结果。请先手动提交一次任务后再跑 calibrate。\n"
        )

    try:
        result = _cal.calibrate(target, target_name)
    except RuntimeError as e:
        sys.stderr.write(f"[TRACE] calibrate 失败：{e}\n")
        return 1

    if not result:
        sys.stderr.write(
            "\n[TRACE] ✗ 所有候选坐标均未能通过验证（prompt-vars 始终未增加）。\n"
            "        排查方向：\n"
            "          1. WorkBuddy 是否已登录（未登录时提交不会生成 prompt-vars）\n"
            "          2. 是否有其他全屏遮挡（弹窗 / 系统对话框）\n"
            "          3. 网络或服务异常导致提交无法落盘\n"
            "          4. 沙箱分辨率/DPI 与常见布局差异过大\n"
        )
        return 1

    out_path = getattr(args, "out", None)
    try:
        written = _cal.save_calibration(result, out_path=out_path)
    except Exception as e:
        sys.stderr.write(f"[TRACE] 标定文件写入失败：{e}\n")
        return 1

    sys.stderr.write(f"\n[TRACE] ✓ 标定成功，已写入：{written}\n")
    sys.stderr.write(
        f"        target={result['target']} "
        f"screen={result['screen']['width']}x{result['screen']['height']}"
        f"DPI{result['screen']['dpi']}\n"
    )
    sys.stderr.write(
        f"        coords: new_task={result['coords']['new_task']} "
        f"input_box={result['coords']['input_box']} "
        f"send_button={result['coords']['send_button']}\n"
    )
    sys.stderr.write(
        f"        submit_method={result['submit_method']}  "
        f"verified_by={result['verified_by']}\n"
    )
    sys.stderr.write(
        f"        后续 run / doctor 会自动加载该文件；"
        f"切换屏幕后需要重新 calibrate。\n"
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
    --json 时输出结构化 JSON 到 stdout。
    """
    from .target import get_target
    from .target_workbuddy import (
        _CALIBRATED_SCREEN,
        _PROMPT_VARS_DIR,
        WorkBuddyTarget,
    )

    use_json = getattr(args, "json", False)
    checks: list[dict] = []

    def add_check(check_id: str, ok: bool, detail: str, fix: str | None = None, warn: bool = False) -> None:
        entry = {"id": check_id, "ok": ok, "detail": detail}
        if fix:
            entry["fix"] = fix
        if warn:
            entry["warn"] = True
        checks.append(entry)
        if not use_json:
            symbol = "✓" if ok else "✗"
            if warn and ok:
                symbol = "⚠"
            sys.stderr.write(f"{symbol} {check_id}: {detail}\n")
            if fix and not ok:
                sys.stderr.write(f"   修复：{fix}\n")

    target_name = args.target or "workbuddy"
    session_id = args.session
    if not session_id:
        env = os.environ.get("TRACE_WB_SESSION")
        if env:
            session_id = env
    if not session_id:
        if use_json:
            result = {"ready": False, "checks": [{"id": "session", "ok": False, "detail": "未提供 session_id", "fix": "通过 --session 或环境变量 TRACE_WB_SESSION 指定"}]}
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write("错误：doctor 需要 --session 或环境变量 TRACE_WB_SESSION。\n")
        return 1

    if not use_json:
        sys.stderr.write(f"[TRACE] doctor target={target_name!r} session={session_id}\n\n")

    # ① 会话可连接
    if not use_json:
        sys.stderr.write("① 会话可连接 ... ")
    try:
        provider = AgentBayProvider()
        session = provider.get_session(session_id)
        # 触发一次实际调用以确认连接
        _ = session.computer.get_screen_size()
        add_check("session", True, "会话可连接")
    except Exception as e:
        add_check("session", False, f"会话连接失败：{e}", "检查 session_id 是否正确、AGENTBAY_API_KEY 是否设置")
        if use_json:
            result = {"ready": False, "checks": checks}
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        return 1

    # ② 被测进程在运行
    if not use_json:
        sys.stderr.write("② 被测进程在运行 ... ")
    try:
        out = session.command.execute_command(
            "powershell -Command \"(Get-Process WorkBuddy -ErrorAction SilentlyContinue).Name\"",
            timeout_ms=30000
        )
        output_text = getattr(out, "output", "") or ""
        success = getattr(out, "success", True)
        snippet = output_text.strip()[:80] if output_text else "(empty)"
        if not success:
            add_check("process", False, f"命令执行失败：{snippet}", "检查会话状态或 PowerShell 可用性")
        elif output_text and "WorkBuddy" in output_text:
            add_check("process", True, f"WorkBuddy 进程在运行")
        else:
            add_check("process", False, f"未发现 WorkBuddy 进程（输出：{snippet}）", "启动 WorkBuddy 后重试；若未安装，运行 provision")
    except Exception as e:
        add_check("process", False, f"execute_command 调用失败：{e}", "检查会话状态")

    # ③ 目标窗口存在
    if not use_json:
        sys.stderr.write("③ 目标窗口存在 ... ")
    try:
        r = session.computer.list_root_windows()
        wins = getattr(r, "windows", None) or []
        titles = [str(getattr(w, "title", "")) for w in wins]
        snippet_titles = [t[:40] for t in titles[:5]]
        if any("WorkBuddy" in t for t in titles):
            add_check("window", True, f"WorkBuddy 窗口存在")
        else:
            add_check("window", False, f"未找到 WorkBuddy 窗口（可见窗口：{snippet_titles}）", "确认 WorkBuddy 已登录且主窗口已显示")
    except Exception as e:
        add_check("window", False, f"list_root_windows 调用失败：{e}", "检查 SDK 版本或会话状态")

    # ④ 屏幕参数与标定一致
    if not use_json:
        sys.stderr.write("④ 屏幕参数与标定一致 ... ")
    try:
        sz = session.computer.get_screen_size()
        data = getattr(sz, "data", None)
        if not isinstance(data, dict):
            add_check("screen", False, f"get_screen_size 返回异常：{sz!r}", "检查 SDK 版本")
        else:
            cur_w = data.get("width")
            cur_h = data.get("height")
            cur_dpi = data.get("dpiScalingFactor") or data.get("dpi") or 1.0
            cal = _CALIBRATED_SCREEN
            if (
                cur_w == cal["width"]
                and cur_h == cal["height"]
                and float(cur_dpi) == float(cal["dpi"])
            ):
                add_check("screen", True, f"屏幕参数匹配（{cur_w}x{cur_h} DPI{cur_dpi}）")
            else:
                # 不一致不判 ✗：坐标可能失准，由投递校验兜底
                add_check("screen", True, f"屏幕参数不一致（当前 {cur_w}x{cur_h} DPI{cur_dpi}, 标定 {cal['width']}x{cal['height']} DPI{cal['dpi']}）", warn=True)
    except Exception as e:
        add_check("screen", False, f"get_screen_size 调用失败：{e}", "检查 SDK 版本")

    # ⑤ 投递信号可用（prompt-vars 目录存在且可读）
    # §FIX-cli-contract：删除过期副本，直接调用 target._count_prompt_vars()
    if not use_json:
        sys.stderr.write("⑤ 投递信号可用（prompt-vars 目录）... ")
    target = get_target(target_name, session)
    if isinstance(target, WorkBuddyTarget):
        try:
            count = target._count_prompt_vars()
            if count < 0:
                add_check("prompt_vars", False, f"{_PROMPT_VARS_DIR} 目录不存在或不可读", "确认 WorkBuddy 已至少提交过一次任务")
            else:
                add_check("prompt_vars", True, f"prompt-vars 目录存在，{count} 个文件")
        except Exception as e:
            add_check("prompt_vars", False, f"_count_prompt_vars 调用失败：{e}", "检查 SDK 版本或会话状态")
    else:
        add_check("prompt_vars", True, "非 WorkBuddy 目标，跳过", warn=True)

    # ⑥ canary 路径可写
    if not use_json:
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
        if success:
            add_check("canary_path", True, f"canary 路径可写")
        else:
            add_check("canary_path", False, f"删除临时文件失败", "确认桌面路径权限")
    except Exception as e:
        add_check("canary_path", False, f"写入测试失败：{e}", f"确认桌面路径 {canary_dir} 可写")

    # ⑦ 标定文件（告警，不计入 failed —— 默认坐标可能仍然可用）
    if not use_json:
        sys.stderr.write("⑦ 标定文件（匹配当前屏幕）... ")
    if isinstance(target, WorkBuddyTarget):
        try:
            from . import calibration as _cal
            info = session.computer.get_screen_size()
            data = info if isinstance(info, dict) else getattr(info, "data", None) or None
            if isinstance(data, dict):
                cw = data.get("width")
                ch = data.get("height")
                cdpi = data.get("dpiScalingFactor") or data.get("dpi") or 1.0
                cal_data = _cal.load_calibration(
                    target_name, int(cw), int(ch), float(cdpi)
                )
                if cal_data:
                    add_check("calibration", True, f"标定文件匹配（screen={cw}x{ch} DPI{cdpi}）")
                else:
                    add_check("calibration", True, f"未找到匹配的标定文件，使用默认坐标", warn=True)
            else:
                add_check("calibration", True, "无法读取屏幕参数，跳过标定文件检查", warn=True)
        except Exception as e:
            add_check("calibration", True, f"标定文件检查失败：{e}", warn=True)
    else:
        add_check("calibration", True, "非 WorkBuddy 目标，跳过", warn=True)

    # ⑧ MSAA 树可抓取（§FEAT-msaa-locator；告警，不计入 failed ——
    # 抓不到时穷举搜索仍然兜底）。
    if not use_json:
        sys.stderr.write("⑧ MSAA 树可抓取 ... ")
    if isinstance(target, WorkBuddyTarget):
        try:
            from . import msaa as _msaa
            elements = _msaa.dump_tree(session, "WorkBuddy")
            if elements:
                add_check(
                    "msaa_tree", True,
                    f"MSAA 树抓取成功（{len(elements)} 个节点）",
                )
            else:
                add_check(
                    "msaa_tree", True,
                    "MSAA 树为空（Chromium 未响应 WM_GETOBJECT 或窗口未就绪），"
                    "退回穷举搜索仍可用",
                    warn=True,
                )
        except Exception as e:
            add_check(
                "msaa_tree", True,
                f"MSAA 抓取异常：{e}（穷举搜索仍可用）",
                warn=True,
            )
    else:
        add_check("msaa_tree", True, "非 WorkBuddy 目标，跳过", warn=True)

    ready = all(c["ok"] for c in checks)
    if use_json:
        result = {"ready": ready, "checks": checks}
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        failed = [c["id"] for c in checks if not c["ok"]]
        warnings = [c["id"] for c in checks if c.get("warn")]
        _emit_doctor_summary(failed, warnings)
    return 0 if ready else 1


def _emit_doctor_summary(failed: list[str], warnings: list[str] | None = None) -> None:
    warnings = warnings or []
    if failed:
        sys.stderr.write(
            f"\n[TRACE] doctor 完成：{len(failed)} 项未通过 ({', '.join(failed)})"
        )
        if warnings:
            sys.stderr.write(
                f"；{len(warnings)} 项告警 ({', '.join(warnings)})"
            )
        sys.stderr.write(
            f"。\n        请修复上述问题后重新运行 doctor。\n"
        )
    elif warnings:
        sys.stderr.write(
            f"\n[TRACE] doctor 完成：环境就绪（{len(warnings)} 项告警："
            f"{', '.join(warnings)}），可以运行评测。\n"
        )
    else:
        sys.stderr.write(
            "\n[TRACE] doctor 完成：环境就绪，可以运行评测。\n"
        )


def _cmd_session_create(args: argparse.Namespace) -> int:
    """创建新会话并轮询等屏幕参数稳定。

    关键约束：
      - 默认 manual_release=True，长流程不被空闲回收打断。
      - 创建后必须等屏幕参数稳定再返回——早期 get_screen_size 会返回过渡值，
        直接拿去 calibrate 会把坐标标错。
      - stderr 给出醒目的计费提醒，确保弱模型调用方看到"用完要删"。
    """
    use_json = getattr(args, "json", False)

    if not os.environ.get("AGENTBAY_API_KEY"):
        msg = "API_KEY_MISSING: 环境变量 AGENTBAY_API_KEY 未设置"
        if use_json:
            json.dump({"error": {"code": "API_KEY_MISSING", "message": msg}},
                      sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"[TRACE] ✗ {msg}\n")
        return EXIT_INPUT_INVALID

    provider = AgentBayProvider()
    labels = None
    if getattr(args, "label", None):
        labels = {"name": args.label}

    # 镜像优先级：显式 --image > --target 的 IMAGE_ID > 旧默认 windows_latest
    image = args.image
    if not image and getattr(args, "target", None):
        from .target import target_meta, known_targets
        try:
            image = target_meta(args.target).IMAGE_ID
        except Exception:
            msg = f"UNKNOWN_TARGET: 未知 target {args.target!r}，已知: {known_targets()}"
            if use_json:
                json.dump({"error": {"code": "UNKNOWN_TARGET", "message": msg}},
                          sys.stdout, ensure_ascii=False, indent=2)
                sys.stdout.write("\n")
            else:
                sys.stderr.write(f"[TRACE] ✗ {msg}\n")
            return EXIT_INPUT_INVALID
    if args.image and getattr(args, "target", None):
        # 两个都给且不一致 → 只警告不阻断（用户可能有意覆盖）
        tgt_img = None
        try:
            from .target import target_meta as _tm
            tgt_img = _tm(args.target).IMAGE_ID
        except Exception:
            tgt_img = None
        if tgt_img and tgt_img != args.image:
            sys.stderr.write(
                f"[TRACE] ⚠ --image={args.image} 与 target {args.target} 的默认镜像 "
                f"{tgt_img} 不一致，按 --image 覆盖。\n"
            )
    if not image:
        image = "windows_latest"   # 两者都没给时的兜底默认（保持旧行为）

    if not use_json:
        sys.stderr.write(
            f"[TRACE] 正在创建会话（image={image}, manual_release=True）...\n"
        )

    try:
        session = provider.create_session(
            image_id=image,
            labels=labels,
            manual_release=True,
        )
    except Exception as e:
        msg = f"SESSION_CREATE_FAILED: {e}"
        if use_json:
            json.dump({"error": {"code": "SESSION_CREATE_FAILED", "message": str(e)}},
                      sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"[TRACE] ✗ {msg}\n")
        return EXIT_INTERNAL_ERROR

    session_id = getattr(session, "session_id", None)
    if not session_id:
        sys.stderr.write("[TRACE] ✗ 创建成功但未返回 session_id\n")
        return EXIT_INTERNAL_ERROR

    if not use_json:
        sys.stderr.write(
            f"[TRACE] 会话已创建：{session_id}，等待屏幕参数稳定...\n"
        )

    # 周期性进度回调——沉默和卡死对弱模型调用方无法区分，必须让进度可见。
    # --json 模式也要打（JSON 只走 stdout，stderr 不影响解析）。
    def _screen_stable_progress(elapsed_s: float, timeout_s: float, cur: Any) -> None:
        if cur is None:
            reading = "(读取失败)"
        else:
            reading = f"{cur['width']}x{cur['height']} DPI{cur['dpi']}"
        sys.stderr.write(
            f"[TRACE] 等待屏幕参数稳定 {int(elapsed_s)}s/{int(timeout_s)}s，"
            f"当前读数 {reading}\n"
        )
        sys.stderr.flush()

    # 等屏幕参数稳定
    try:
        screen = _wait_screen_stable(session, on_poll=_screen_stable_progress)
    except RuntimeError as e:
        # 创建成功但稳定超时——会话已经在计费，必须告诉用户 session_id 以便手动删
        # 超时文案（§FIX-session-create-stall 第 4 节）：
        #   - 写明会话已创建、正在计费、session_id
        #   - 给出删除命令
        #   - 若只是想拿地址扫码登录，可以忽略超时，直接用 `session url <id>`
        if use_json:
            json.dump({
                "session_id": session_id,
                "screen": None,
                "desktop_url": None,
                "stable": False,
                "screen_note": (
                    "屏幕参数未在超时内稳定，但会话已创建并正在计费。"
                    "1024x768 是合法的稳定态（无观看端接入时的正常值），"
                    "打开云桌面地址后分辨率通常会变化。"
                    "如果只是想拿地址扫码登录，可以忽略本超时，直接用 `session url <id>`。"
                ),
                "cleanup": f"python3 -m trace.cli session rm {session_id}",
                "error": {"code": "SCREEN_NOT_STABLE", "message": str(e)},
            }, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"[TRACE] ⚠ 屏幕参数未在超时内稳定：{e}\n")
            sys.stderr.write(
                f"[TRACE] ⚠ 会话 {session_id} 已创建并持续计费。\n"
            )
            sys.stderr.write(
                f"[TRACE]   - 删除命令：python3 -m trace.cli session rm {session_id}\n"
            )
            sys.stderr.write(
                f"[TRACE]   - 若只是想拿地址扫码登录，可以忽略本超时，"
                f"直接用 `python3 -m trace.cli session url {session_id}` 取地址。\n"
            )
        return EXIT_ENVIRONMENT_INVALID

    desktop_url = get_desktop_url(session)

    # screen_note：告诉调用方当前屏幕参数的语义——
    # 1024x768 不是异常，只是还没人连云桌面；打开云桌面地址后分辨率通常会变化。
    screen_note = (
        "当前无观看端接入；打开云桌面地址后分辨率通常会变化，"
        "校准应在登录后进行"
    )

    if use_json:
        json.dump({
            "session_id": session_id,
            "screen": screen,
            "desktop_url": desktop_url,
            "stable": True,
            "screen_note": screen_note,
        }, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(
            f"[TRACE] ✓ 屏幕已稳定（{screen['width']}x{screen['height']} "
            f"DPI{screen['dpi']}）\n"
        )
        # 解释 1024x768 不是异常——否则下一个人又会把它当成异常
        sys.stderr.write(f"[TRACE] 注：{screen_note}\n")
        if desktop_url:
            sys.stderr.write(f"[TRACE] 云桌面地址：{desktop_url}\n")
        # 醒目的计费提示——这是让弱模型记得收尾的关键
        sys.stderr.write(
            f"\n"
            f"[TRACE] ╔══════════════════════════════════════════════════════════╗\n"
            f"[TRACE] ║  会话已创建：{session_id}（持续计费）              ║\n"
            f"[TRACE] ║  用完请务必执行：                                       ║\n"
            f"[TRACE] ║    python3 -m trace.cli session rm {session_id:<17s} ║\n"
            f"[TRACE] ╚══════════════════════════════════════════════════════════╝\n"
        )
    return EXIT_SUCCESS


def _wait_screen_stable(session: Any, timeout: float = 180.0, on_poll: Any | None = None) -> dict:
    """从 provider.wait_for_screen_stable 取稳定屏幕参数。

    单独拎出来是为了让 cli.py 里的错误处理路径能给出 session_id 让用户手动清理。
    on_poll 透传给 provider.wait_for_screen_stable（用于周期性进度输出）。
    """
    from .provider import wait_for_screen_stable
    return wait_for_screen_stable(session, timeout=timeout, on_poll=on_poll)


def _cmd_session_rm(args: argparse.Namespace) -> int:
    """删除会话（单个或全部）。删后回查残留，残留情况决定退出码。

    退出码：全部删干净 → 0，有残留 → EXIT_ENVIRONMENT_INVALID（钱还在烧）。
    """
    use_json = getattr(args, "json", False)

    if not os.environ.get("AGENTBAY_API_KEY"):
        msg = "API_KEY_MISSING: 环境变量 AGENTBAY_API_KEY 未设置"
        if use_json:
            json.dump({"error": {"code": "API_KEY_MISSING", "message": msg}},
                      sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"[TRACE] ✗ {msg}\n")
        return EXIT_INPUT_INVALID

    provider = AgentBayProvider()

    # 收集要删除的 session_id 列表
    targets: list[str] = []
    if getattr(args, "all", False):
        # --all：先把当前能列出来的都拿一遍。注意 list 不可靠，
        # 所以这里只是"尽力而为"——真正判残留要等删完后再 list 一次。
        listing = provider.list_sessions()
        if listing["success"]:
            targets = listing["session_ids"]
        if not use_json:
            sys.stderr.write(
                f"[TRACE] --all：从 list() 拿到 {len(targets)} 个会话 "
                f"（注意：list 结果不可靠，可能遗漏）\n"
            )
    else:
        sid = getattr(args, "session_id", None)
        if not sid:
            if use_json:
                json.dump({"error": {"code": "ARGUMENT_MISSING",
                                     "message": "必须提供 session_id 或 --all"}},
                          sys.stdout, ensure_ascii=False, indent=2)
                sys.stdout.write("\n")
            else:
                sys.stderr.write("[TRACE] ✗ 必须提供 session_id 或 --all\n")
            return EXIT_INPUT_INVALID
        targets = [sid]

    results: list[dict] = []
    for sid in targets:
        if not use_json:
            sys.stderr.write(f"[TRACE] 删除 {sid} ...\n")
        del_result = provider.delete_session(sid)
        # 回查残留
        residual = not provider.verify_gone(sid)
        results.append({
            "session_id": sid,
            "delete_success": del_result["success"],
            "delete_error": del_result.get("error"),
            "already_gone": del_result.get("already_gone", False),
            "residual": residual,
        })
        if not use_json:
            if del_result.get("already_gone"):
                sys.stderr.write(f"[TRACE]   ✓ {sid} 已不存在\n")
            elif del_result["success"] and not residual:
                sys.stderr.write(f"[TRACE]   ✓ {sid} 已删除并确认无残留\n")
            elif del_result["success"] and residual:
                sys.stderr.write(
                    f"[TRACE]   ⚠ {sid} 删除 API 返回成功，但回查仍存在——"
                    f"可能仍在计费\n"
                )
            else:
                sys.stderr.write(
                    f"[TRACE]   ✗ {sid} 删除失败："
                    f"{del_result.get('error') or 'unknown error'}\n"
                )

    any_residual = any(r["residual"] for r in results)
    any_failed = any(not r["delete_success"] and not r.get("already_gone")
                     for r in results)

    if use_json:
        json.dump({
            "deleted": len([r for r in results if r["delete_success"] or r["already_gone"]]),
            "failed": len([r for r in results if not r["delete_success"] and not r.get("already_gone")]),
            "residual": len([r for r in results if r["residual"]]),
            "results": results,
        }, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        if not results:
            sys.stderr.write("[TRACE] 没有找到要删除的会话。\n")
        if any_residual or any_failed:
            sys.stderr.write(
                f"\n[TRACE] ⚠ 收尾未干净：{sum(r['residual'] for r in results)} 个残留，"
                f"{sum(1 for r in results if not r['delete_success'] and not r.get('already_gone'))} 个失败。"
                f"请检查 API 密钥权限或稍后重试。\n"
            )
        else:
            sys.stderr.write(
                f"\n[TRACE] ✓ 已删除 {len(results)} 个会话，全部确认无残留。\n"
            )

    return EXIT_SUCCESS if not (any_residual or any_failed) else EXIT_ENVIRONMENT_INVALID


def _cmd_session_list(args: argparse.Namespace) -> int:
    """列举会话。

    ⚠️ 已知问题：ab.list() 在有会话运行时曾返回空列表——结果不可靠。
    输出里必须写明这一点；绝不能让调用方把"list 为空"当成"没在计费"的证据。
    """
    use_json = getattr(args, "json", False)

    if not os.environ.get("AGENTBAY_API_KEY"):
        msg = "API_KEY_MISSING: 环境变量 AGENTBAY_API_KEY 未设置"
        if use_json:
            json.dump({"error": {"code": "API_KEY_MISSING", "message": msg}},
                      sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"[TRACE] ✗ {msg}\n")
        return EXIT_INPUT_INVALID

    provider = AgentBayProvider()
    status_filter = getattr(args, "status", None)
    listing = provider.list_sessions(status=status_filter or None)

    reliability_warning = (
        "⚠️ list() 实测不可靠——有会话运行时曾返回空列表。"
        "空结果不能作为'没有会话在计费'的证据。"
        "要确认计费状态，请登录 AgentBay 控制台查看。"
    )

    if use_json:
        json.dump({
            "success": listing["success"],
            "session_ids": listing["session_ids"],
            "total_count": listing["total_count"],
            "status_filter": status_filter,
            "reliability_warning": reliability_warning,
            "error": listing.get("error"),
        }, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(
            f"[TRACE] {reliability_warning}\n"
        )
        if not listing["success"]:
            sys.stderr.write(
                f"[TRACE] ✗ list() 调用失败：{listing.get('error') or 'unknown error'}\n"
            )
            return EXIT_INTERNAL_ERROR
        ids = listing["session_ids"]
        if status_filter:
            sys.stderr.write(f"[TRACE] 过滤 status={status_filter}\n")
        if not ids:
            sys.stderr.write(
                "[TRACE] list() 返回空列表（再次提醒：这可能是 API 的已知问题，"
                "不代表没有会话在计费）。\n"
            )
        else:
            sys.stderr.write(f"[TRACE] 共 {len(ids)} 个会话：\n")
            for sid in ids:
                sys.stderr.write(f"        {sid}\n")
    return EXIT_SUCCESS


def _cmd_session_url(args: argparse.Namespace) -> int:
    """打印云桌面地址。可重复执行——authcode 过期就重跑。"""
    sid = args.session_id

    if not os.environ.get("AGENTBAY_API_KEY"):
        sys.stderr.write("[TRACE] ✗ API_KEY_MISSING: 环境变量 AGENTBAY_API_KEY 未设置\n")
        return EXIT_INPUT_INVALID

    provider = AgentBayProvider()
    try:
        session = provider.get_session(sid)
    except RuntimeError as e:
        sys.stderr.write(f"[TRACE] ✗ SESSION_NOT_FOUND: {e}\n")
        return EXIT_ENVIRONMENT_INVALID

    url = get_desktop_url(session)
    if not url:
        sys.stderr.write(
            f"[TRACE] ✗ 无法获取会话 {sid} 的云桌面地址（info() 没返回 resource_url）\n"
        )
        return EXIT_INTERNAL_ERROR

    # URL 直接输出到 stdout（纯值，方便脚本 `$(...)` 捕获）
    sys.stdout.write(url + "\n")
    sys.stderr.write(
        f"[TRACE] 会话 {sid} 的云桌面地址（浏览器打开可交互）。\n"
        f"[TRACE] 注意：authcode 有时效，过期重跑本命令即可。\n"
    )
    return EXIT_SUCCESS


def _resolve_cases(cases_arg: str) -> list[str]:
    """解析 --cases 到 case 文件路径列表。

    - 目录：取其下所有 *.json 文件（排除 _ 开头），按文件名排序。
    - 逗号分隔列表：直接拆分（保留原始顺序）。
    目录不存在 / 列表为空时抛 ValueError。
    """
    if os.path.isdir(cases_arg):
        files = sorted(
            os.path.join(cases_arg, f)
            for f in os.listdir(cases_arg)
            if f.endswith(".json") and not f.startswith(("_", "."))
        )
        if not files:
            raise ValueError(f"BATCH_CASES_EMPTY: 目录下没有 *.json 文件：{cases_arg}")
        return files
    if os.path.exists(cases_arg):
        raise ValueError(
            f"BATCH_CASES_NOT_DIR: --cases 既不是目录也不是文件列表：{cases_arg}"
        )
    # 视为逗号分隔路径列表
    parts = [p.strip() for p in cases_arg.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"BATCH_CASES_EMPTY: --cases 解析为空：{cases_arg}")
    return parts


def _cmd_cases_list(args: argparse.Namespace) -> int:
    """列出某 target（或某目录）的用例菜单。只读，不需要 session。"""
    from .target import target_meta, known_targets
    cases_dir = args.cases
    if not cases_dir and getattr(args, "target", None):
        try:
            cases_dir = target_meta(args.target).DEFAULT_CASES
        except Exception:
            sys.stderr.write(f"[TRACE] ✗ UNKNOWN_TARGET: {args.target!r}，已知: {known_targets()}\n")
            return EXIT_INPUT_INVALID
    if not cases_dir:
        sys.stderr.write("[TRACE] ✗ 需要 --target 或 --cases <目录> 之一\n")
        return EXIT_INPUT_INVALID
    if not os.path.isdir(cases_dir):
        sys.stderr.write(f"[TRACE] ✗ 用例目录不存在：{cases_dir}\n")
        return EXIT_INPUT_INVALID
    rows = []
    for fn in sorted(os.listdir(cases_dir)):
        if not fn.endswith(".json") or fn.startswith(("_", ".")):
            continue
        try:
            with open(os.path.join(cases_dir, fn), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        rows.append({
            "id": d.get("id", fn),
            "title": d.get("title", "-"),
            "suite": d.get("suite", "-"),
            "vector": d.get("vector", "-"),
        })
    if getattr(args, "json", False):
        json.dump({"cases_dir": cases_dir, "cases": rows}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(f"[TRACE] 用例目录：{cases_dir}（{len(rows)} 个）\n")
        for r in rows:
            sys.stdout.write(
                f"  {r['id']:<14} {r['title']:<30} suite={r['suite']:<9} vector={r['vector']}\n"
            )
    return EXIT_SUCCESS


def _resolve_batch_cases(args: argparse.Namespace) -> list[str]:
    """按优先级把 run-batch 的选择解析成 case 文件路径列表。

    优先级：--cases（旧行为：目录/路径列表）> --ids > --suite > --target 全部。
    都没有 → 抛 ValueError（调用方负责 fail-fast）。
    """
    if getattr(args, "cases", None):
        return _resolve_cases(args.cases)  # 旧行为，向后兼容
    from .target import target_meta, known_targets
    tgt = getattr(args, "target", None)
    if not tgt:
        raise ValueError("BATCH_SELECT_MISSING: 需要 --cases 或 --target 之一")
    try:
        cases_dir = target_meta(tgt).DEFAULT_CASES
    except Exception:
        raise ValueError(f"UNKNOWN_TARGET: {tgt!r}，已知: {known_targets()}")
    if not cases_dir or not os.path.isdir(cases_dir):
        raise ValueError(f"BATCH_CASES_DIR_MISSING: target {tgt!r} 的 DEFAULT_CASES 不存在：{cases_dir}")
    allcases: list[tuple[str, str, str]] = []
    for fn in sorted(os.listdir(cases_dir)):
        if not fn.endswith(".json") or fn.startswith(("_", ".")):
            continue
        p = os.path.join(cases_dir, fn)
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        allcases.append((d.get("id"), d.get("suite"), p))
    ids_arg = getattr(args, "ids", None)
    suite_arg = getattr(args, "suite", None)
    if ids_arg:
        want = [s.strip() for s in ids_arg.split(",") if s.strip()]
        idmap = {cid: p for cid, _s, p in allcases if cid}
        missing = [w for w in want if w not in idmap]
        if missing:
            raise ValueError(f"BATCH_IDS_NOT_FOUND: {cases_dir} 里找不到 id：{missing}")
        return [idmap[w] for w in want]
    if suite_arg:
        sel = [p for _cid, s, p in allcases if s == suite_arg]
        if not sel:
            raise ValueError(f"BATCH_SUITE_EMPTY: {cases_dir} 里没有 suite={suite_arg!r} 的用例")
        return sel
    return [p for _cid, _s, p in allcases]


def _format_duration(seconds: float) -> str:
    """格式化成 Xm Ys（便于进度输出，人读友好）。"""
    total = max(0, int(seconds))
    m, s = divmod(total, 60)
    return f"{m}m{s:02d}s"


def _do_health_check(target: Any, target_name: str) -> tuple[bool, str]:
    """零成本健康检查：窗口还在吗、prompt-vars 目录还读得到吗。

    只查状态，**不提交任务、不消耗额度**。
    返回 (ok, reason)；非 workbuddy target 直接视为 OK。
    """
    from .target_workbuddy import WorkBuddyTarget

    if not isinstance(target, WorkBuddyTarget):
        return True, "non-WorkBuddy target; skip"

    try:
        r = target.session.computer.list_root_windows()
        wins = getattr(r, "windows", None) or []
        titles = [str(getattr(w, "title", "")) for w in wins]
        if not any("WorkBuddy" in t for t in titles):
            return False, f"WorkBuddy 窗口不存在（可见：{titles[:5]}）"
    except Exception as e:
        return False, f"list_root_windows 失败：{e}"

    try:
        n = target._count_prompt_vars()
    except Exception as e:
        return False, f"_count_prompt_vars 异常：{e}"
    if n < 0:
        return False, "prompt-vars 目录不可读"
    return True, f"window OK, prompt-vars={n}"


def _summarize_case(case: dict, result: dict, result_path: str) -> dict:
    """构造单条 case 的批次汇总条目（字段名对齐 §FEAT-run-batch 示例）。"""
    return {
        "id": case["id"],
        "agent_security": result.get("agent_security"),
        "root_cause": result.get("root_cause"),
        "failure_rate": result.get("failure_rate"),
        "result_path": result_path,
    }


# --- §FEAT-run-batch：批次运行（一次校准，连跑整个用例矩阵）---
_MAX_RECALIBRATIONS_PER_BATCH = 3
_CONSECUTIVE_ERROR_STATE_ABORT = 2


def _cmd_run_batch(args: argparse.Namespace) -> int:
    """批次运行：一次校准，连跑整个用例矩阵。

    关键约束（§FEAT-run-batch）：
      - 校准只做一次；布局跳变时允许重新校准（整批最多 3 次）
      - 额度耗尽（连续 2 个 ERROR_STATE）早停
      - 单个用例失败不中断批次
      - 汇总里 ENVIRONMENT_INVALID 不计入通过率分母
      - 汇总只给原始计数，不给百分比
      - schema 校验在批次开始前全部完成
      - 批次层绝不引入新判定规则——单个用例判定完全复用 run_case
    """
    batch_start = time.monotonic()
    batch_id = _dt.datetime.now().astimezone().isoformat(timespec="seconds")

    # 1. 解析用例选择（--cases / --ids / --suite / --target；缺选择 → 退出 3）
    try:
        case_paths = _resolve_batch_cases(args)
    except ValueError as e:
        sys.stderr.write(f"[TRACE] ✗ {e}\n")
        return EXIT_INPUT_INVALID

    # 2. 批次开跑前一次性校验所有 case 的 schema（不能跑到第 37 个才发现第 38 个格式不对）
    cases: list[tuple[str, dict]] = []
    schema_errors: list[str] = []
    for path in case_paths:
        try:
            case = _load_case(path)
            _validate_case(case)
            cases.append((path, case))
        except (FileNotFoundError, ValueError) as e:
            schema_errors.append(f"{path}: {e}")
        except Exception as e:
            schema_errors.append(f"{path}: 未预期错误：{e}")

    if schema_errors:
        sys.stderr.write(
            f"[TRACE] ✗ {len(schema_errors)} 个 case schema 不合法，中止批次：\n"
        )
        for err in schema_errors:
            sys.stderr.write(f"        {err}\n")
        return EXIT_INPUT_INVALID

    # 3. 解析 session_id
    try:
        session_id = _resolve_session_id(args, cases[0][1])
    except ValueError as e:
        sys.stderr.write(f"[TRACE] ✗ {e}\n")
        return EXIT_INPUT_INVALID

    # 4. 准备输出目录
    out_dir = os.path.abspath(args.out_dir)
    try:
        os.makedirs(out_dir, exist_ok=True)
        evidence_dir = os.path.join(out_dir, "evidence")
        os.makedirs(evidence_dir, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"[TRACE] ✗ 无法创建输出目录：{e}\n")
        return EXIT_INPUT_INVALID

    # 5. 连接 session
    if not os.environ.get("AGENTBAY_API_KEY"):
        sys.stderr.write("[TRACE] ✗ API_KEY_MISSING: 环境变量 AGENTBAY_API_KEY 未设置\n")
        return EXIT_INPUT_INVALID

    provider = AgentBayProvider()
    try:
        session = provider.get_session(session_id)
    except Exception as e:
        err_msg = str(e)
        if "not found" in err_msg.lower() or "session" in err_msg.lower():
            sys.stderr.write(f"[TRACE] ✗ SESSION_NOT_FOUND: {err_msg}\n")
            return EXIT_ENVIRONMENT_INVALID
        sys.stderr.write(f"[TRACE] ✗ 获取会话失败：{err_msg}\n")
        return EXIT_INTERNAL_ERROR

    desktop_url = get_desktop_url(session)
    if desktop_url:
        sys.stderr.write(f"[TRACE] 沙箱桌面（可实时观看）：{desktop_url}\n")

    # 6. 批次开始时校准一次（复用给所有用例）
    total_cases = len(cases)
    sys.stderr.write(
        f"[TRACE] 批次开始：{total_cases} 个用例，session={session_id}\n"
    )

    first_target_name = cases[0][1]["target"]
    from .target import get_target
    shared_target = get_target(first_target_name, session)

    sys.stderr.write(
        f"[TRACE] auto-calibrate: 开始批次初始校准（target={first_target_name}）\n"
    )
    calibration_result = runner._auto_calibrate_for_run(shared_target, first_target_name)
    if calibration_result is None:
        sys.stderr.write("[TRACE] ✗ 批次初始校准失败，无法继续\n")
        return EXIT_ENVIRONMENT_INVALID

    coords_source = calibration_result.get("coords_source") or "default"
    recalibrations = 0

    # 7. 批次状态追踪
    summary_cases: list[dict] = []
    verdict_counts: dict[str, int] = {}
    env_invalid_count = 0
    measured_count = 0
    consecutive_error_state = 0
    aborted = False
    abort_reason: str | None = None

    # 8. 主循环：逐用例运行
    for idx, (case_path, case) in enumerate(cases, start=1):
        case_id = case["id"]
        case_start = time.monotonic()
        elapsed_total = time.monotonic() - batch_start

        sys.stderr.write(
            f"[TRACE] [{idx}/{total_cases}] {case_id} 开始"
            f"（已用时 {_format_duration(elapsed_total)}）\n"
        )
        sys.stderr.flush()

        case_result_path = os.path.join(out_dir, f"{case_id}.json")

        # --repeat 覆盖 case 内的 repeat 字段
        effective_case = case
        if args.repeat is not None:
            effective_case = dict(case)
            effective_case["repeat"] = args.repeat

        # 复用现有 run_case（不引入任何新判定规则）
        try:
            result = runner.run_case(
                effective_case, session, evidence_dir,
                auto_calibrate=False,
                target=shared_target,
            )
        except Exception as e:
            # 单个用例抛异常 → 该用例记 ENVIRONMENT_INVALID / RUN_EXECUTION_ERROR
            sys.stderr.write(f"[TRACE] ⚠ {case_id} 异常：{e}\n")
            result = {
                "id": case_id,
                "runs": 0,
                "agent_security": "ENVIRONMENT_INVALID",
                "system_protection": "N/A",
                "failure_rate": "0/0",
                "root_cause": "RUN_EXECUTION_ERROR",
                "verdict_basis": None,
                "error": {"code": "RUN_EXECUTION_ERROR", "message": str(e)},
                "evidence": [],
            }

        # 任务未送达 → 立刻重新校准一次，然后重跑该用例一次
        # 重跑仍失败 → 该用例记 ENVIRONMENT_INVALID / TASK_NOT_DELIVERED，继续下一个
        if (result.get("agent_security") == "ENVIRONMENT_INVALID"
                and result.get("root_cause") == "TASK_NOT_DELIVERED"):
            if recalibrations < _MAX_RECALIBRATIONS_PER_BATCH:
                sys.stderr.write(
                    f"[TRACE] ⚠ {case_id} 任务未送达，立刻重新校准"
                    f"（第 {recalibrations + 1}/{_MAX_RECALIBRATIONS_PER_BATCH} 次）...\n"
                )
                recalibrations += 1
                retry_cal = runner._auto_calibrate_for_run(
                    shared_target, first_target_name
                )
                if retry_cal is None:
                    sys.stderr.write(
                        f"[TRACE] ✗ {case_id} 重新校准失败，保留 ENVIRONMENT_INVALID 结论\n"
                    )
                else:
                    calibration_result = retry_cal
                    coords_source = retry_cal.get("coords_source") or coords_source
                    try:
                        result = runner.run_case(
                            effective_case, session, evidence_dir,
                            auto_calibrate=False,
                            target=shared_target,
                        )
                    except Exception as e:
                        sys.stderr.write(f"[TRACE] ⚠ {case_id} 重跑异常：{e}\n")
                        result = {
                            "id": case_id,
                            "runs": 0,
                            "agent_security": "ENVIRONMENT_INVALID",
                            "system_protection": "N/A",
                            "failure_rate": "0/0",
                            "root_cause": "RUN_EXECUTION_ERROR",
                            "verdict_basis": None,
                            "error": {"code": "RUN_EXECUTION_ERROR", "message": str(e)},
                            "evidence": [],
                        }
            else:
                # 用尽重新校准次数 → 中止整批（环境已不可靠，继续跑只是烧钱）
                _write_result(case_result_path, result)
                case_elapsed = time.monotonic() - case_start
                verdict = result.get("agent_security")
                sys.stderr.write(
                    f"[TRACE] [{idx}/{total_cases}] {case_id} -> {verdict} "
                    f"({result.get('failure_rate')}) 用时 {_format_duration(case_elapsed)}\n"
                )
                summary_cases.append(_summarize_case(case, result, case_result_path))
                if verdict in ("PASS", "FAIL"):
                    verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
                    measured_count += 1
                else:
                    env_invalid_count += 1
                aborted = True
                abort_reason = "MAX_RECALIBRATIONS_EXCEEDED"
                sys.stderr.write(
                    f"[TRACE] ✗ 整批重新校准次数已达上限，中止批次"
                    f"（已完成 {len(summary_cases)}/{total_cases}）\n"
                )
                break

        # 写入该用例的 result.json
        _write_result(case_result_path, result)

        # 进度输出（每个用例结束都打一行）
        case_elapsed = time.monotonic() - case_start
        verdict = result.get("agent_security")
        sys.stderr.write(
            f"[TRACE] [{idx}/{total_cases}] {case_id} -> {verdict} "
            f"({result.get('failure_rate')}) 用时 {_format_duration(case_elapsed)}\n"
        )
        sys.stderr.flush()

        # 分类统计（ENVIRONMENT_INVALID 不计入 measured / 通过率分母）
        summary_cases.append(_summarize_case(case, result, case_result_path))
        if verdict in ("PASS", "FAIL"):
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            measured_count += 1
            consecutive_error_state = 0
        elif verdict == "ENVIRONMENT_INVALID":
            env_invalid_count += 1
            # 连续 2 个 ERROR_STATE → 中止整批（额度可能耗尽）
            if result.get("root_cause") == "TARGET_AGENT_ERROR":
                consecutive_error_state += 1
                if consecutive_error_state >= _CONSECUTIVE_ERROR_STATE_ABORT:
                    sys.stderr.write(
                        f"[TRACE] ✗ 连续 {_CONSECUTIVE_ERROR_STATE_ABORT} 个用例 "
                        f"ERROR_STATE（额度可能耗尽），中止批次"
                        f"（已完成 {len(summary_cases)}/{total_cases}）\n"
                    )
                    aborted = True
                    abort_reason = "CONSECUTIVE_ERROR_STATE"
                    break
            else:
                consecutive_error_state = 0
        else:
            # 未知 agent_security 取值 → 保守计为环境无效
            env_invalid_count += 1
            consecutive_error_state = 0

        # 用例跑完做一次零成本健康检查（不提交任务）
        # 失败时不立刻中止——下一用例开跑前会重新校准
        health_ok, health_reason = _do_health_check(shared_target, first_target_name)
        if not health_ok:
            sys.stderr.write(
                f"[TRACE] ⚠ {case_id} 跑完后健康检查失败：{health_reason}\n"
            )
            if recalibrations < _MAX_RECALIBRATIONS_PER_BATCH:
                sys.stderr.write(
                    f"[TRACE] 尝试重新校准以恢复投递能力...\n"
                )
                recalibrations += 1
                retry_cal = runner._auto_calibrate_for_run(
                    shared_target, first_target_name
                )
                if retry_cal is None:
                    sys.stderr.write(
                        f"[TRACE] ✗ 重新校准失败，环境可能已不可靠，中止批次\n"
                    )
                    aborted = True
                    abort_reason = "HEALTH_CHECK_FAILED_AND_RECALIBRATE_FAILED"
                    break
                calibration_result = retry_cal
                coords_source = retry_cal.get("coords_source") or coords_source
            else:
                sys.stderr.write(
                    f"[TRACE] ✗ 已用尽重新校准次数，中止批次\n"
                )
                aborted = True
                abort_reason = "MAX_RECALIBRATIONS_EXCEEDED"
                break

        # 可选：每个用例生成一份 HTML 报告
        if args.report_dir:
            try:
                from . import report as _report
                os.makedirs(args.report_dir, exist_ok=True)
                html_text = _report.render_html(case, result)
                html_path = os.path.join(args.report_dir, f"{case_id}.html")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_text)
            except Exception as e:
                sys.stderr.write(
                    f"[TRACE] ⚠ {case_id} HTML 报告生成失败：{e}\n"
                )

    # 9. 写批次汇总（即使中途被中止也要把已完成的保留）
    # 没跑的用例清单：已完成 len(summary_cases) 个，剩余就是没跑的。
    # 这是『让没测到消失在一个数字里』的批次层版本——必须有字段记录，
    # 否则上游平台按 `cases` 算覆盖率会以为全部跑过了。
    not_run_case_ids = [c[1]["id"] for c in cases[len(summary_cases):]]
    not_run_count = len(not_run_case_ids)
    # 写入前断言：账目必须对得上。
    # cases == measured + environment_invalid + not_run
    # 不成立意味着有用例既没跑也没被记录——严重错误，必须立刻暴露，
    # 绝不能静默产出一份账对不上的汇总。
    if total_cases != measured_count + env_invalid_count + not_run_count:
        raise RuntimeError(
            f"BATCH_ACCOUNTING_BROKEN: cases({total_cases}) != "
            f"measured({measured_count}) + environment_invalid({env_invalid_count}) + "
            f"not_run({not_run_count})"
        )

    summary = {
        "batch_id": batch_id,
        "session_id": session_id,
        "calibration": {
            "coords_source": coords_source,
            "recalibrations": recalibrations,
        },
        "totals": {
            "cases": total_cases,
            "measured": measured_count,
            "environment_invalid": env_invalid_count,
            "not_run": not_run_count,
        },
        "not_run_case_ids": not_run_case_ids,
        "verdicts": verdict_counts,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "cases": summary_cases,
    }

    summary_path = os.path.join(out_dir, "_batch_summary.json")
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        sys.stderr.write(f"[TRACE] 批次汇总 -> {summary_path}\n")
    except OSError as e:
        sys.stderr.write(f"[TRACE] ⚠ 无法写入批次汇总：{e}\n")

    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")

    total_elapsed = time.monotonic() - batch_start
    sys.stderr.write(
        f"[TRACE] 批次结束：measured={measured_count} "
        f"environment_invalid={env_invalid_count} "
        f"not_run={not_run_count} "
        f"verdicts={verdict_counts} "
        f"aborted={aborted}"
        f"{(' (' + abort_reason + ')') if aborted else ''} "
        f"总用时 {_format_duration(total_elapsed)}\n"
    )

    # 10. 退出码：整批最严重的那个
    if aborted:
        return EXIT_ENVIRONMENT_INVALID
    if env_invalid_count > 0:
        return EXIT_ENVIRONMENT_INVALID
    return EXIT_SUCCESS


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
    pr.add_argument(
        "--auto-calibrate", dest="auto_calibrate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "每次评测前现场校准坐标（默认开启）。"
            "校准在 plant_doc 之前完成，屏幕上不会出现注入 payload。"
            "校准失败 → ENVIRONMENT_INVALID / CALIBRATION_FAILED。"
            "用 --no-auto-calibrate 关闭（不推荐，仅用于调试）。"
        ),
    )
    pr.set_defaults(func=_cmd_run)

    # ---- §FEAT-run-batch：批次运行（一次校准，连跑整个用例矩阵）----
    prb = sub.add_parser(
        "run-batch",
        help=(
            "批次运行：一次校准连跑整个用例矩阵，把校准次数从 N 降到 1"
            "（校准会提交探针任务消耗真实额度）。"
            "单个用例判定完全复用 run_case，批次层不引入新判定规则。"
        ),
    )
    prb.add_argument(
        "--cases", required=False, default=None,
        help=(
            "用例来源：目录（取其下所有 *.json），或逗号分隔的文件列表。"
            "schema 校验在批次开跑前一次性完成。"
            "与 --target/--suite/--ids 互斥优先级：--cases 最高（向后兼容）。"
        ),
    )
    prb.add_argument(
        "--target", default=None,
        help="按 target 默认用例目录选用例（来自 target 元数据声明）。",
    )
    prb.add_argument(
        "--suite", default=None,
        help="按 suite 名过滤（在 target 默认目录里匹配 case['suite']）。",
    )
    prb.add_argument(
        "--ids", default=None,
        help="按 case id 选择（逗号分隔，在 target 默认目录里按 case['id'] 匹配）。",
    )
    prb.add_argument(
        "--out-dir", required=True,
        help="输出目录：每个用例一份 <case-id>.json，以及 _batch_summary.json。",
    )
    prb.add_argument(
        "--session", default=None,
        help="会话 ID（优先级最高；缺省按首个 case['session_id'] → 环境变量 TRACE_WB_SESSION 回退）。",
    )
    prb.add_argument(
        "--repeat", type=int, default=None,
        help="覆盖各 case 里的 repeat 字段（缺省沿用 case 内的 repeat）。",
    )
    prb.add_argument(
        "--report-dir", default=None,
        help="可选，每个用例一份 HTML 报告，输出到该目录。",
    )
    prb.add_argument(
        "--json", action="store_true",
        help="把批次汇总结果输出到 stdout（人类可读进度仍走 stderr）。",
    )
    prb.set_defaults(func=_cmd_run_batch)

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
    pd.add_argument(
        "--json", action="store_true",
        help="输出结构化 JSON 到 stdout（人类可读的 ✓/✗ 仍走 stderr）。",
    )
    pd.set_defaults(func=_cmd_doctor)

    pc = sub.add_parser(
        "calibrate",
        help=(
            "自校准（§FEAT-self-calibration）：搜索候选坐标并用 prompt-vars 铁证验证，"
            "存盘标定 JSON。不让模型看截图定位按钮——模型的视觉定位系统性偏小。"
        ),
    )
    pc.add_argument(
        "--target", default="workbuddy",
        help="target 名称（默认 workbuddy；当前仅支持 workbuddy）。",
    )
    pc.add_argument(
        "--session", default=None,
        help="会话 ID（缺省按环境变量 TRACE_WB_SESSION 回退）。",
    )
    pc.add_argument(
        "--out", default=None,
        help=(
            "标定 JSON 输出路径（可选）。默认写入 calibration/<target>_<W>x<H>_dpi<DPI>.json。"
            "指定后写到给定路径，便于跨机器迁移。"
        ),
    )
    pc.set_defaults(func=_cmd_calibrate)

    # ---- session 子命令组：会话生命周期 ----
    ps = sub.add_parser(
        "session",
        help="会话生命周期：create / rm / list / url。上游测评平台不用管这组——会话由人工准备好。skill 使用者（工具内的模型）走完整流程才需要。",
    )
    ss = ps.add_subparsers(dest="session_cmd", required=True)

    # session create
    psc = ss.add_parser(
        "create",
        help="创建新会话（默认 manual_release=True，避免长流程被自动回收）。创建后轮询等屏幕参数稳定再返回。",
    )
    psc.add_argument(
        "--image", default=None,
        help="镜像 ID（可选；优先级：显式 --image > --target 的默认镜像 > windows_latest 兜底）。",
    )
    psc.add_argument(
        "--target", default=None,
        help="被测目标名，据此选默认镜像（可选）。",
    )
    psc.add_argument(
        "--label", default=None,
        help="给会话打一个 name label，便于 list 时辨识（可选）。",
    )
    psc.add_argument(
        "--json", action="store_true",
        help="输出结构化 JSON 到 stdout（session_id / screen / desktop_url）。",
    )
    psc.set_defaults(func=_cmd_session_create)

    # session rm
    psr = ss.add_parser(
        "rm",
        help="删除会话。删后回查残留，残留情况决定退出码。--all 删除全部（用于收尾兜底）。",
    )
    psr.add_argument(
        "session_id", nargs="?", default=None,
        help="要删除的会话 ID。与 --all 二选一。",
    )
    psr.add_argument(
        "--all", action="store_true",
        help="删除全部会话（基于 list()，注意 list 不可靠）。",
    )
    psr.add_argument(
        "--json", action="store_true",
        help="输出结构化 JSON 到 stdout。",
    )
    psr.set_defaults(func=_cmd_session_rm)

    # session list
    psl = ss.add_parser(
        "list",
        help="列举会话。⚠️ 已知问题：ab.list() 实测不可靠——空结果不能作为'没在计费'的证据。",
    )
    psl.add_argument(
        "--status", default=None,
        help="按状态过滤（RUNNING / PAUSED / DELETING / DELETED 等，具体取值见 SDK）。",
    )
    psl.add_argument(
        "--json", action="store_true",
        help="输出结构化 JSON 到 stdout。",
    )
    psl.set_defaults(func=_cmd_session_list)

    # session url
    psu = ss.add_parser(
        "url",
        help="打印云桌面地址（可重复执行；authcode 过期就重跑）。",
    )
    psu.add_argument(
        "session_id",
        help="会话 ID。",
    )
    psu.set_defaults(func=_cmd_session_url)

    # ---- cases 子命令组：用例菜单（只读，不进沙箱） ----
    pcs = sub.add_parser(
        "cases",
        help="用例管理：list（只读，不进沙箱、无 session）。",
    )
    scs = pcs.add_subparsers(dest="cases_cmd", required=True)

    # cases list
    pscl = scs.add_parser(
        "list",
        help="列出某 target（或某目录）的用例菜单（id / title / suite / vector）。",
    )
    pscl.add_argument(
        "--target", default=None,
        help="target 名称，取其 DEFAULT_CASES 目录（与 --cases 二选一）。",
    )
    pscl.add_argument(
        "--cases", default=None,
        help="直接用例目录（优先级高于 --target）。",
    )
    pscl.add_argument(
        "--json", action="store_true",
        help="输出结构化 JSON 到 stdout（供程序消费）。",
    )
    pscl.set_defaults(func=_cmd_cases_list)

    return p


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。异常映射到退出码，绝不让 traceback 泄漏成最终输出。

    退出码（§FIX-cli-contract 第一节）：
      0  测量完成，结论可信（PASS 和 FAIL 都是 0）
      2  环境无效，没测成（ENVIRONMENT_INVALID）
      3  输入非法（case schema / 参数 / 文件问题）
      1  工具自身异常（含一切未捕获异常）
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.func(args) or 0)
    except SystemExit as e:
        # argparse 或其他地方抛出的 SystemExit
        return int(e.code) if e.code is not None else 0
    except Exception as e:
        # 顶层兜底：绝不让 traceback 泄漏到退出码
        sys.stderr.write(f"[TRACE] ✗ 顶层异常：{e}\n")
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
