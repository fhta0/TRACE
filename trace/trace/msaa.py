"""MSAA-based semantic locator for Electron/Chromium windows.

Background (see docs/dev-notes/FINDING-msaa-route.md):
    Chromium answers MSAA's WM_GETOBJECT but not UIA, so we can locate
    the input box and buttons of an Electron agent by control name/role.

Design (see docs/dev-notes/FEAT-msaa-locator.md):
    MSAA does NOT replace the exhaustive calibration search. It only provides
    a better first-priority candidate source. The success criterion is still
    the deterministic prompt-vars count +1 — the same as the search path.

    Locate input box / button:
      1. Try MSAA semantic locator (by name/role)        <- this module
      2. Fall back to exhaustive search on failure       <- unchanged

This module is target-agnostic. It doesn't know about WorkBuddy; it just
grabs the MSAA control tree for a given window title.
"""
from __future__ import annotations

import sys
from typing import Any

# ---------------------------------------------------------------------------
# PowerShell script (pure ASCII — UTF-8 BOM-less PS1 is read as ANSI by
# PowerShell 5.1; multi-byte chars trigger ParserError, hit twice before).
# No here-strings (triggered ParserError in this environment).
# The traversal logic below has been verified on a real device; do not
# change the calling conventions (SendMessage 0x003D/OBJID_CLIENT,
# GetObjectForIUnknown late-binding, [ref] for accLocation out params,
# dwId written as decimal 4294967292 — not 0xFFFFFFFC).
# ---------------------------------------------------------------------------
_PS_SCRIPT = r"""
$OUT = "C:\Users\Administrator\_trace_msaa.txt"
Set-Content -Path $OUT -Value "" -Encoding UTF8

# P/Invoke. Build the C# source by joining lines - do NOT use a here-string.
$src = @(
'using System;',
'using System.Text;',
'using System.Collections.Generic;',
'using System.Runtime.InteropServices;',
'public class TraceMsaa {',
'  public delegate bool EnumProc(IntPtr h, IntPtr p);',
'  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);',
'  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);',
'  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);',
'  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);',
'  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);',
'  [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr h, uint id, ref Guid iid, out IntPtr pp);',
'  public static List<IntPtr> Kids(IntPtr parent) {',
'    List<IntPtr> r = new List<IntPtr>();',
'    EnumChildWindows(parent, delegate(IntPtr h, IntPtr p) { r.Add(h); return true; }, IntPtr.Zero);',
'    return r; }',
'  public static string Cls(IntPtr h) { StringBuilder sb=new StringBuilder(256); GetClassName(h,sb,256); return sb.ToString(); }',
'  public static List<IntPtr> TopWindows() {',
'    List<IntPtr> r = new List<IntPtr>();',
'    EnumWindows(delegate(IntPtr h, IntPtr p) { r.Add(h); return true; }, IntPtr.Zero);',
'    return r; }',
'  public static string Title(IntPtr h) { StringBuilder sb=new StringBuilder(512); GetWindowText(h,sb,512); return sb.ToString(); }',
'}') -join "`n"
Add-Type -TypeDefinition $src -ErrorAction SilentlyContinue

$titleMatch = '__TITLE_PLACEHOLDER__'
$maxDepth = __DEPTH_PLACEHOLDER__
$maxNodes = __MAXNODES_PLACEHOLDER__
$nodeCount = 0

# Find top-level window whose title contains the marker
$topHwnd = [IntPtr]::Zero
foreach ($h in [TraceMsaa]::TopWindows()) {
    $t = [TraceMsaa]::Title($h)
    if ($t -match [regex]::Escape($titleMatch)) { $topHwnd = $h; break }
}
if ($topHwnd -eq [IntPtr]::Zero) { exit 0 }

# MSAA DOM is inside the Chrome_RenderWidgetHostHWND child, NOT the top window.
# Top window MSAA tree has only ~9 frame nodes.
$chromeHwnd = [IntPtr]::Zero
foreach ($h in [TraceMsaa]::Kids($topHwnd)) {
    if ([TraceMsaa]::Cls($h) -eq "Chrome_RenderWidgetHostHWND") { $chromeHwnd = $h; break }
}
if ($chromeHwnd -eq [IntPtr]::Zero) { exit 0 }

# Wake up Chromium's accessibility support via WM_GETOBJECT(OBJID_CLIENT=-4).
[void][TraceMsaa]::SendMessage($chromeHwnd, 0x003D, [IntPtr]0, [IntPtr](-4))
Start-Sleep -Milliseconds 300

# IID_IDispatch = 00020400-0000-0000-C000-000000000046
$iid = [Guid]"00020400-0000-0000-C000-000000000046"
$pp = [IntPtr]::Zero
# dwId MUST be decimal 4294967292. Writing 0xFFFFFFFC would parse as Int32 -4,
# then overflow when cast to [uint32].
$hr = [TraceMsaa]::AccessibleObjectFromWindow($chromeHwnd, ([uint32]4294967292), [ref]$iid, [ref]$pp)
if ($hr -ne 0 -or $pp -eq [IntPtr]::Zero) { exit 0 }

# Late-binding: no C# COM interface needed. GetObjectForIUnknown wraps pp
# into an RCW that exposes accName/accRole/accChild/accChildCount/accLocation.
$root = [Runtime.InteropServices.Marshal]::GetObjectForIUnknown($pp)

function Walk($acc, $depth) {
    if ($depth -gt $maxDepth) { return }
    if ($script:nodeCount -ge $maxNodes) { return }

    # Emit this node at childId=0 (the object itself).
    try {
        $nm = "$($acc.accName(0))"
        $rl = 0 + $acc.accRole(0)
        $x = 0; $y = 0; $w = 0; $h = 0
        $acc.accLocation([ref]$x, [ref]$y, [ref]$w, [ref]$h, 0)
        if ($w -gt 0 -and $h -gt 0) {
            $script:nodeCount++
            "NODE`t$depth`t$rl`t$x`t$y`t$w`t$h`t$nm" | Add-Content -Path $OUT -Encoding UTF8
        }
    } catch {}

    try {
        $cc = 0 + $acc.accChildCount
        for ($i = 1; $i -le $cc; $i++) {
            if ($script:nodeCount -ge $maxNodes) { return }
            $child = $null
            try { $child = $acc.accChild($i) } catch {}
            if ($null -ne $child -and $child -is [System.__ComObject]) {
                # Real IAccessible child - recurse.
                Walk $child ($depth + 1)
                try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($child) } catch {}
            } else {
                # Simple child (VT_I4 child id) - emit via parent at childId=i.
                try {
                    $nm = "$($acc.accName($i))"
                    $rl = 0 + $acc.accRole($i)
                    $x = 0; $y = 0; $w = 0; $h = 0
                    $acc.accLocation([ref]$x, [ref]$y, [ref]$w, [ref]$h, $i)
                    if ($w -gt 0 -and $h -gt 0) {
                        $script:nodeCount++
                        "NODE`t$($depth + 1)`t$rl`t$x`t$y`t$w`t$h`t$nm" | Add-Content -Path $OUT -Encoding UTF8
                    }
                } catch {}
            }
        }
    } catch {}
}

Walk $root 0
try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($root) } catch {}
"""

# Remote paths (inside the sandbox)
_PS_SCRIPT_PATH = r"C:\Users\Administrator\_trace_msaa.ps1"
_PS_OUTPUT_PATH = r"C:\Users\Administrator\_trace_msaa.txt"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _read_file_text(session: Any, path: str) -> str:
    """Read a text file from the sandbox, defensive about SDK return shape.

    `session.filesystem.read_file` may return str, bytes, or an object with a
    `.data` / `.content` attribute. We accept all of them and decode UTF-8.
    Chinese characters in the file survive the round trip (verified).
    """
    try:
        r = session.filesystem.read_file(path)
    except Exception:
        return ""
    if isinstance(r, str):
        return r
    if isinstance(r, (bytes, bytearray)):
        return r.decode("utf-8", errors="replace")
    # SDK object form
    for attr in ("data", "content", "text"):
        v = getattr(r, attr, None)
        if isinstance(v, str):
            return v
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", errors="replace")
    return str(r) if r is not None else ""


def dump_tree(
    session: Any,
    window_title_contains: str,
    max_depth: int = 14,
    max_nodes: int = 400,
) -> list[dict]:
    """Grab the MSAA control tree of a window whose title contains the marker.

    Returns a list of dicts:
        [{"depth": int, "role": int, "name": str,
          "x": int, "y": int, "w": int, "h": int, "cx": int, "cy": int}, ...]
    Coordinates are absolute screen pixels (physical), ready to feed into
    `session.computer.click_mouse`. Returns [] on any failure.

    Pipeline:
      1. Substitute placeholders in the PS1 script (title / depth / max_nodes).
      2. `session.filesystem.write_file` the PS1 (pure ASCII).
      3. `session.command.execute_command` powershell -File to run it.
      4. `session.filesystem.read_file` back the tab-separated output.
         (Do NOT trust execute_command stdout — real-device runs showed it drops
         output; read_file is reliable.)
    """
    # 1. Substitute placeholders
    #    Title is placed inside PS single quotes, so escape embedded single
    #    quotes by doubling them (PS single-quote escape rule).
    safe_title = window_title_contains.replace("'", "''")
    ps = _PS_SCRIPT.replace("'__TITLE_PLACEHOLDER__'", f"'{safe_title}'")
    ps = ps.replace("__DEPTH_PLACEHOLDER__", str(int(max_depth)))
    ps = ps.replace("__MAXNODES_PLACEHOLDER__", str(int(max_nodes)))

    # 2. Write the PS1 script (pure ASCII — no Chinese literals)
    try:
        session.filesystem.write_file(_PS_SCRIPT_PATH, ps)
    except Exception as e:
        sys.stderr.write(f"[TRACE] MSAA: write ps1 failed: {e}\n")
        return []

    # 3. Execute. Don't depend on stdout — read the output file instead.
    try:
        session.command.execute_command(
            f'powershell -ExecutionPolicy Bypass -File "{_PS_SCRIPT_PATH}"',
            timeout_ms=60000,
        )
    except Exception as e:
        sys.stderr.write(f"[TRACE] MSAA: execute failed: {e}\n")
        # Continue anyway — try reading the output file (it might still exist)

    # 4. Read back the result file
    content = _read_file_text(session, _PS_OUTPUT_PATH)
    if not content:
        return []

    elements: list[dict] = []
    for line in content.splitlines():
        if not line.startswith("NODE\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        # name is last (may contain spaces / non-ASCII); keep the rest intact.
        try:
            depth = int(parts[1])
            role = int(parts[2])
            x = int(parts[3])
            y = int(parts[4])
            w = int(parts[5])
            h = int(parts[6])
        except (ValueError, IndexError):
            continue
        name = "\t".join(parts[7:])  # rejoin in case name itself had tabs
        elements.append({
            "depth": depth,
            "role": role,
            "name": name,
            "x": x, "y": y, "w": w, "h": h,
            "cx": x + w // 2,
            "cy": y + h // 2,
        })
    return elements


def find(
    elements: list[dict],
    *,
    role: int | None = None,
    name: str | None = None,
    name_contains: str | None = None,
    screen_w: int | None = None,
    screen_h: int | None = None,
) -> dict | None:
    """Pick one element by criteria. If multiple match, pick the largest by area.

    `screen_w` / `screen_h`: when given, filter out elements whose center
    falls outside the screen (real-device carousel elements can report
    x up to 2550, well past a 1920-wide screen).
    """
    matches: list[dict] = []
    for e in elements:
        if role is not None and e.get("role") != role:
            continue
        if name is not None and e.get("name") != name:
            continue
        if name_contains is not None and name_contains not in (e.get("name") or ""):
            continue
        cx = e.get("cx", 0)
        cy = e.get("cy", 0)
        if screen_w is not None and (cx < 0 or cx >= screen_w):
            continue
        if screen_h is not None and (cy < 0 or cy >= screen_h):
            continue
        matches.append(e)
    if not matches:
        return None
    return max(matches, key=lambda e: e.get("w", 0) * e.get("h", 0))
