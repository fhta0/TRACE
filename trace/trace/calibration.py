"""Self-calibration: search candidate coordinates and verify each with the
deterministic prompt-vars count increment.

Core principle (FEAT-self-calibration.md):
  NEVER ask a model to visually locate buttons. Instead, search candidate
  coordinates and verify each with deterministic ground truth we already
  have — the WorkBuddy prompt-vars file count incrementing on a successful
  submit.

Calibration may drive the UI but must contain NO LLM judgment —
verification is purely the prompt-vars count.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Calibration file directory: project-root/calibration/
# (sibling to the `trace/` package, so `trace/cli.py` and users can find it.)
_CALIBRATION_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "calibration")
)

# Default probe text — harmless, short, likely to be accepted by any agent.
_PROBE_TEXT = "请只回复两个字：收到"

# Polling for verification after a submit attempt
_VERIFY_POLL_INTERVAL = 1.0   # seconds
_VERIFY_POLL_TIMEOUT = 15.0   # seconds

# §FIX-calibration-v2：候选比例（实测验证过的布局位置）
# y 比例把实测命中的 0.44 放在第一位，其余作为其他布局的兜底。
_INPUT_BOX_Y_RATIOS = (0.44, 0.40, 0.78, 0.55, 0.70, 0.85)
# x 比例加进 0.58（实测 x=1120 在 W=1920 下 ≈ 0.583，验证可用）。
_INPUT_BOX_X_RATIOS = (0.5, 0.35, 0.6, 0.58)


# ---------------------------------------------------------------------------
# File IO
# ---------------------------------------------------------------------------

def calibration_filename(target: str, width: int, height: int, dpi: float) -> str:
    """Canonical filename for a calibration file.

    Example: `workbuddy_1920x954_dpi1.25.json`
    """
    # Use `g` format so 1.0 becomes "1" and 1.25 stays "1.25"
    dpi_str = f"{float(dpi):g}"
    return f"{target}_{int(width)}x{int(height)}_dpi{dpi_str}.json"


def calibration_path(target: str, width: int, height: int, dpi: float) -> str:
    """Full path to the calibration file for the given screen."""
    return os.path.join(_CALIBRATION_DIR, calibration_filename(target, width, height, dpi))


def load_calibration(target: str, width: int, height: int, dpi: float) -> dict | None:
    """Load a calibration file matching the given screen parameters.

    Returns the calibration dict on success, or None if not found / unreadable
    / mismatched. Validation is strict: if the file's target/screen fields do
    not match the request, treat it as absent (caller falls back to defaults).
    """
    path = calibration_path(target, width, height, dpi)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("target") != target:
        return None
    screen = data.get("screen") or {}
    try:
        if int(screen.get("width", -1)) != int(width):
            return None
        if int(screen.get("height", -1)) != int(height):
            return None
        if float(screen.get("dpi", 0)) != float(dpi):
            return None
    except (TypeError, ValueError):
        return None
    return data


def save_calibration(calibration: dict, out_path: str | None = None) -> str:
    """Save a calibration dict to disk.

    If `out_path` is given, write there; otherwise write to the canonical path
    under _CALIBRATION_DIR. Returns the path written.
    """
    target = calibration["target"]
    screen = calibration["screen"]
    if out_path:
        path = out_path
    else:
        path = calibration_path(
            target, screen["width"], screen["height"], screen["dpi"]
        )
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(calibration, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

def _input_box_candidates(width: int, height: int) -> list[tuple[int, int]]:
    """Input-box candidates: §FIX-calibration-v2 实测验证过的布局位置。

    §FIX-calibration-v2：y 把实测命中的 0.44 放在第一位，其余作为其他布局兜底；
    x 加进 0.58（实测 x=1120 在 W=1920 下 ≈ 0.583，验证可用）。
    Total: len(_INPUT_BOX_Y_RATIOS) × len(_INPUT_BOX_X_RATIOS) candidates,
    ordered so that the highest-priority (0.44 × 0.5) is tried first.
    """
    xs = [int(width * r) for r in _INPUT_BOX_X_RATIOS]
    ys = [int(height * r) for r in _INPUT_BOX_Y_RATIOS]
    out: list[tuple[int, int]] = []
    for y in ys:
        for x in xs:
            out.append((x, y))
    return out


def _new_task_candidates(width: int, height: int) -> list[tuple[int, int]]:
    """New-task button candidates: left sidebar top.

    x = 0.04 × width
    y ∈ {0.10, 0.14, 0.18} × height
    Default (80, 133) on 1920×954 corresponds to (0.04, 0.14), so try 0.14 first.
    """
    x = int(width * 0.04)
    return [(x, int(height * r)) for r in (0.14, 0.10, 0.18)]


def _send_button_candidates(
    input_x: int, input_y: int, width: int, height: int,
) -> list[tuple[int, int]]:
    """Send-button candidates: right-lower area relative to the input box.

    Only used when Enter-key submission fails for every input-box candidate.
    6 points to the right of / below the input box, prioritizing the most
    likely spot (immediately to the right of the input box at the same y).
    """
    # Right-of-input candidates (within the input row)
    right_1 = min(width - 20, input_x + 80)
    right_2 = int(width * 0.82)
    right_3 = int(width * 0.88)
    # Below-input candidates (to catch send buttons that sit under the row)
    below_1 = min(height - 20, input_y + 40)
    below_2 = min(height - 20, input_y + 80)
    return [
        (right_1, input_y),
        (right_2, input_y),
        (right_1, below_1),
        (right_3, input_y),
        (right_2, below_1),
        (right_1, below_2),
    ][:6]


# ---------------------------------------------------------------------------
# Verification primitives
# ---------------------------------------------------------------------------

def _count_prompt_vars(target_obj: Any) -> int:
    """Delegate to target_obj._count_prompt_vars(); never raise."""
    try:
        return int(target_obj._count_prompt_vars())
    except Exception:
        return -1


def _close_popup_default(target_obj: Any) -> None:
    """Close popup using ratio-based coordinates (not saved calibration / not
    hardcoded pixels). §FIX-calibration-v2: 按当前屏幕尺寸比例换算，避免
    「要关弹窗得先有坐标，而坐标正是要校准的」死锁。

    由实测的两个 ✕ 位置反推的比例（见 target_workbuddy._POPUP_CLOSE_RATIOS）：
      (0.927, 0.104)  右侧面板 ✕
      (0.183, 0.099)  内层卡片 ✕
    """
    from .target_workbuddy import _POPUP_CLOSE_RATIOS, _get_screen_info
    info = _get_screen_info(target_obj.session) or {}
    w = info.get("width") or 1920
    h = info.get("height") or 954
    try:
        w_i, h_i = int(w), int(h)
    except (TypeError, ValueError):
        w_i, h_i = 1920, 954
    for rx, ry in _POPUP_CLOSE_RATIOS:
        try:
            target_obj.session.computer.click_mouse(int(w_i * rx), int(h_i * ry))
            time.sleep(1.5)
        except Exception:
            pass


def _wait_for_increment(target_obj: Any, baseline: int) -> bool:
    """Poll prompt-vars until count > baseline or timeout. Returns True on increment."""
    deadline = time.monotonic() + _VERIFY_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_VERIFY_POLL_INTERVAL)
        n = _count_prompt_vars(target_obj)
        if n > baseline:
            return True
        if n < 0:
            # Signal momentarily unavailable; keep polling
            continue
    return False


# ---------------------------------------------------------------------------
# §FIX-calibration-v2：归一化前置步骤
# 每个探针前，先走「新建任务 + 关弹窗」，把布局落到确定的已知状态（输入框 ≈ 0.44H）。
# ---------------------------------------------------------------------------

def _normalize_before_probe(target_obj: Any, new_task_xy: tuple[int, int]) -> None:
    """把布局归一化到已知状态（实测连续两轮 prompt-vars 2→3→4 验证通过）。

    步骤：
      1. 点击「新建任务」候选坐标
      2. sleep 3
      3. 关弹窗（比例法）
      4. sleep 1
    之后输入框会稳定落在 ≈ 0.44H。归一化是 best-effort：任一步失败不阻断后续。
    """
    comp = target_obj.session.computer
    try:
        comp.click_mouse(new_task_xy[0], new_task_xy[1])
        time.sleep(3)
    except Exception:
        pass
    _close_popup_default(target_obj)
    time.sleep(1)


# ---------------------------------------------------------------------------
# Probe: try one candidate combination
# ---------------------------------------------------------------------------

def _try_probe(
    target_obj: Any,
    new_task_xy: tuple[int, int],
    input_xy: tuple[int, int],
    submit_method: str,
    send_xy: tuple[int, int] | None,
    nt_xy_for_norm: tuple[int, int] | None = None,
) -> bool:
    """Try one candidate combination. Returns True iff prompt-vars incremented.

    Does NOT use target_obj's calibrated coords — it clicks the given
    candidates directly, so calibration search is independent of any
    previously-loaded calibration.

    §FIX-calibration-v2：每次探针前先跑归一化前置步骤（新建任务 + 关弹窗），
    把布局落到确定的已知状态，再进入实际探测序列。

    Steps:
      0. (normalize) click new-task candidate → sleep 3 → close popup → sleep 1
      1. Close any popup (ratio-based)
      2. Click new-task candidate
      3. Click input-box candidate
      4. Clear input (ctrl+a + BackSpace × 80)
      5. Type probe text
      6. Submit (Enter or click send button)
      7. Poll prompt-vars count for up to _VERIFY_POLL_TIMEOUT seconds
    """
    comp = target_obj.session.computer
    try:
        # 0. §FIX-calibration-v2：归一化前置步骤（best-effort）
        if nt_xy_for_norm is not None:
            _normalize_before_probe(target_obj, nt_xy_for_norm)

        # 1. Close popup (ratio-based)
        _close_popup_default(target_obj)

        # 2. Click new-task
        comp.click_mouse(new_task_xy[0], new_task_xy[1])
        time.sleep(1.5)

        # 3. Click input box
        comp.click_mouse(input_xy[0], input_xy[1])
        time.sleep(0.8)

        # 4. Clear input (defensive: new-task may leave draft text behind)
        comp.press_keys(["ctrl", "a"])
        time.sleep(0.2)
        for _ in range(80):
            comp.press_keys(["BackSpace"])
        time.sleep(0.2)

        # 5. Type probe
        comp.input_text(_PROBE_TEXT)
        time.sleep(0.5)

        # 6. Submit
        if submit_method == "enter":
            comp.press_keys(["Enter"])
        elif submit_method == "click" and send_xy is not None:
            comp.click_mouse(send_xy[0], send_xy[1])
        else:
            return False

        # 7. Verify — prompt-vars count must strictly increase
        n_before = _count_prompt_vars(target_obj)
        if n_before < 0:
            # Signal unavailable → can't verify this probe, treat as failure
            return False
        return _wait_for_increment(target_obj, n_before)
    except Exception as e:
        sys.stderr.write(f"[TRACE] calibrate: probe raised: {e}\n")
        return False


# ---------------------------------------------------------------------------
# §FEAT-msaa-locator: MSAA-first candidate
# ---------------------------------------------------------------------------

def _try_msaa_first(
    target_obj: Any,
    target_name: str,
    width: int, height: int, dpi: float,
) -> dict | None:
    """Try MSAA semantic locate + prompt-vars verification.

    Returns a verified calibration dict on success (coords_source="msaa"),
    or None on any failure so the caller falls through to the exhaustive
    search. Never raises — all exceptions are caught and logged to stderr.

    Verification uses the exact same iron criterion as the exhaustive path:
    the prompt-vars file count must strictly increase after a probe submit.
    MSAA being a "better guess" does not change what "success" means.
    """
    try:
        msaa_coords = target_obj.locate_via_msaa()
    except Exception as e:
        sys.stderr.write(
            f"[TRACE] MSAA 路径异常：{e}，退回穷举搜索\n"
        )
        return None

    if not msaa_coords:
        # locate_via_msaa already logged "MSAA 未命中 (...)" to stderr.
        return None

    nt_xy = msaa_coords["new_task"]
    ib_xy = msaa_coords["input_box"]
    sb_xy = msaa_coords["send_button"]

    # Snapshot prompt-vars baseline before the probe.
    n_before = _count_prompt_vars(target_obj)
    if n_before < 0:
        sys.stderr.write(
            "[TRACE] MSAA 验证跳过：prompt-vars 信号不可用，退回穷举搜索\n"
        )
        return None

    comp = target_obj.session.computer
    try:
        # Normalize layout (new-task click + close popup), same as the
        # exhaustive search does per probe.
        _normalize_before_probe(target_obj, nt_xy)
        _close_popup_default(target_obj)

        # Click new-task
        comp.click_mouse(nt_xy[0], nt_xy[1])
        time.sleep(1.5)

        # Click input box
        comp.click_mouse(ib_xy[0], ib_xy[1])
        time.sleep(0.8)

        # Clear input (defensive: same sequence as _try_probe)
        comp.press_keys(["ctrl", "a"])
        time.sleep(0.2)
        for _ in range(80):
            comp.press_keys(["BackSpace"])
        time.sleep(0.2)

        # Type probe text
        comp.input_text(_PROBE_TEXT)
        time.sleep(0.5)

        # Submit — try Enter first; MSAA has given us a send_button but
        # we still prefer Enter (cheaper, fewer assumptions). If the probe
        # fails, the exhaustive search's Phase 2 will try click-submit.
        comp.press_keys(["Enter"])

        if _wait_for_increment(target_obj, n_before):
            n_after = _count_prompt_vars(target_obj)
            sys.stderr.write(
                f"[TRACE] MSAA 坐标验证通过"
                f"（prompt-vars {n_before} -> {n_after}），"
                f"跳过穷举搜索\n"
            )
            return _build_calibration(
                target_name, width, height, dpi,
                new_task=nt_xy, input_box=ib_xy,
                submit_method="enter", send_button=sb_xy,
                coords_source="msaa",
            )

        sys.stderr.write(
            "[TRACE] MSAA 坐标验证未通过"
            "（prompt-vars 未增加），退回穷举搜索\n"
        )
        return None
    except Exception as e:
        sys.stderr.write(
            f"[TRACE] MSAA 验证探针异常：{e}，退回穷举搜索\n"
        )
        return None


# ---------------------------------------------------------------------------
# Top-level search
# ---------------------------------------------------------------------------

def calibrate(target_obj: Any, target_name: str) -> dict | None:
    """Search candidate coordinates and return a verified calibration dict.

    Returns None if no valid combination was found after exhausting all
    candidates.

    Strategy:
      0. MSAA first (FEAT-msaa-locator.md) — semantic locate by name/role.
         If it produces coordinates, verify them with the same prompt-vars
         criterion as the exhaustive search. On pass, return immediately.
         On fail (or if MSAA can't locate), fall through to step 1.
      1. Phase 1 — new-task × input-box × Enter key (exhaustive)
      2. Phase 2 — new-task × input-box × send-button click
                   (only if Phase 1 failed for every input candidate)
      Stop at first verified combination (each probe may consume real quota).
    """
    # Get current screen info
    from .target_workbuddy import WorkBuddyTarget, _get_screen_info
    info = _get_screen_info(target_obj.session)
    if not info:
        raise RuntimeError("Cannot get screen info for calibration")
    width = info.get("width")
    height = info.get("height")
    dpi = info.get("dpiScalingFactor") or info.get("dpi") or 1.0
    if not width or not height:
        raise RuntimeError(f"Invalid screen info: {info!r}")
    width, height = int(width), int(height)
    dpi = float(dpi)

    sys.stderr.write(
        f"[TRACE] calibrate: screen {width}x{height} DPI{dpi}, "
        f"target={target_name}\n"
    )

    # ------------------------------------------------------------------
    # Step 0: §FEAT-msaa-locator — MSAA as the first-priority candidate
    # source. Does NOT replace the exhaustive search: MSAA just hands us
    # a guess, and we still verify it with the prompt-vars iron ground
    # truth. The criterion is identical to the search path.
    # ------------------------------------------------------------------
    if isinstance(target_obj, WorkBuddyTarget):
        msaa_result = _try_msaa_first(target_obj, target_name, width, height, dpi)
        if msaa_result is not None:
            return msaa_result
        # msaa_result is None → fall through to exhaustive search below

    new_task_cands = _new_task_candidates(width, height)
    input_cands = _input_box_candidates(width, height)

    sys.stderr.write(
        f"[TRACE] calibrate: {len(new_task_cands)} new-task × "
        f"{len(input_cands)} input-box candidates with Enter\n"
    )

    # Phase 1: new-task × input-box × Enter
    for nt_xy in new_task_cands:
        for ib_xy in input_cands:
            sys.stderr.write(
                f"[TRACE]   probe new_task={nt_xy} input={ib_xy} submit=Enter ... "
            )
            sys.stderr.flush()
            if _try_probe(
                target_obj, nt_xy, ib_xy, "enter", None,
                nt_xy_for_norm=nt_xy,
            ):
                sys.stderr.write("✓ verified (prompt-vars incremented)\n")
                return _build_calibration(
                    target_name, width, height, dpi,
                    new_task=nt_xy, input_box=ib_xy,
                    submit_method="enter", send_button=None,
                )
            sys.stderr.write("✗\n")

    # Phase 2: Enter failed for all — search send-button coordinates
    sys.stderr.write(
        "[TRACE] calibrate: Enter-key submission failed for all input candidates; "
        "searching send-button coordinates\n"
    )
    for nt_xy in new_task_cands:
        for ib_xy in input_cands:
            send_cands = _send_button_candidates(ib_xy[0], ib_xy[1], width, height)
            for sb_xy in send_cands:
                sys.stderr.write(
                    f"[TRACE]   probe new_task={nt_xy} input={ib_xy} "
                    f"send={sb_xy} submit=click ... "
                )
                sys.stderr.flush()
                if _try_probe(
                    target_obj, nt_xy, ib_xy, "click", sb_xy,
                    nt_xy_for_norm=nt_xy,
                ):
                    sys.stderr.write("✓ verified (prompt-vars incremented)\n")
                    return _build_calibration(
                        target_name, width, height, dpi,
                        new_task=nt_xy, input_box=ib_xy,
                        submit_method="click", send_button=sb_xy,
                    )
                sys.stderr.write("✗\n")

    return None


def _build_calibration(
    target: str,
    width: int, height: int, dpi: float,
    new_task: tuple[int, int],
    input_box: tuple[int, int],
    submit_method: str,
    send_button: tuple[int, int] | None,
    coords_source: str = "search",
) -> dict:
    """Build a calibration dict from verified coordinates.

    `coords_source` records how the coordinates were obtained:
      - "msaa"   — MSAA semantic locator (FEAT-msaa-locator.md)
      - "search" — exhaustive prompt-vars-verified search
    Diagnostic only; not used by the runner. Helps triage which path fired.
    """
    from .target_workbuddy import _COORD_POPUP_CLOSE_CARD, _COORD_POPUP_CLOSE_PANEL

    now = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "target": target,
        "screen": {"width": width, "height": height, "dpi": dpi},
        "coords": {
            "new_task": list(new_task),
            "input_box": list(input_box),
            "send_button": list(send_button) if send_button is not None else None,
        },
        "submit_method": submit_method,
        "popup_close": [list(_COORD_POPUP_CLOSE_PANEL), list(_COORD_POPUP_CLOSE_CARD)],
        "calibrated_at": now,
        "verified_by": "prompt_vars_count_increment",
        "coords_source": coords_source,
    }
