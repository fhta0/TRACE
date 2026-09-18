"""TRACE HTML 报告渲染。

纯函数：只读 case/result dict 与本地截图文件，不碰网络、不导入 agentbay、不读环境变量。
"""
from __future__ import annotations

import base64
import html
import os
from datetime import datetime, timezone


_PILL_CLASS = {
    "PASS": "pill pass",
    "FAIL": "pill fail",
    "NONE": "pill fail",
    "N/A": "pill na",
}


def _b64_png(path: str) -> str | None:
    """读取 PNG 文件并返回 base64 字符串；文件不存在或不可读时返回 None。"""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    return base64.b64encode(data).decode("ascii")


def _escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _fmt_ts(result: dict) -> str:
    ev = result.get("evidence") or []
    if ev and isinstance(ev[0], dict) and ev[0].get("ts"):
        return str(ev[0]["ts"])
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _render_evidence_rows(evidence: list[dict]) -> str:
    if not evidence:
        return '<tr><td colspan="4" class="muted">（无证据）</td></tr>'
    rows = []
    for ev in evidence:
        run = ev.get("run", "")
        canary = ev.get("canary", "")
        ts = ev.get("ts", "")
        canary_cls = "canary-created" if canary == "CREATED" else "canary-clean"
        canary_label = _escape(str(canary))

        b64 = _b64_png(str(ev.get("screenshot") or ""))
        if b64:
            shot_html = (
                f'<img src="data:image/png;base64,{b64}" alt="run {run} 截图" '
                f'class="screenshot" loading="lazy">'
            )
        else:
            shot_html = '<span class="muted">（截图缺失）</span>'

        rows.append(
            '<tr>'
            f'<td class="num">{_escape(str(run))}</td>'
            f'<td><span class="{canary_cls}">{canary_label}</span></td>'
            f'<td class="mono">{_escape(str(ts))}</td>'
            f'<td>{shot_html}</td>'
            '</tr>'
        )
    return "\n".join(rows)


def render_html(case: dict, result: dict) -> str:
    """把一个 case + 它的 result 渲染成自包含的 HTML 字符串。"""
    case = case or {}
    result = result or {}

    case_id = _escape(str(case.get("id") or result.get("id") or "(unknown)"))
    target = _escape(str(case.get("target") or ""))
    permission_mode = _escape(str(case.get("permission_mode") or "default"))
    ts = _escape(_fmt_ts(result))

    agent_sec = str(result.get("agent_security") or "N/A")
    sys_prot = str(result.get("system_protection") or "N/A")
    failure_rate = _escape(str(result.get("failure_rate") or "—"))
    verdict_basis = _escape(str(result.get("verdict_basis") or ""))
    root_cause = result.get("root_cause")
    root_cause_html = (
        _escape(str(root_cause)) if root_cause else '<span class="muted">—</span>'
    )

    doc_filename = _escape(str(case.get("doc_filename") or ""))
    doc_content = _escape(str(case.get("doc_content") or ""))
    benign_task = _escape(str(case.get("benign_task") or ""))

    evidence_rows = _render_evidence_rows(result.get("evidence") or [])

    page_title = f"TRACE 报告 {case_id}"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
  :root {{
    --bg: #fafbfc;
    --fg: #1a1a1a;
    --muted: #6b7280;
    --border: #e5e7eb;
    --card: #ffffff;
    --code-bg: #f3f4f6;
    --pass: #15803d;
    --pass-bg: #dcfce7;
    --pass-border: #86efac;
    --fail: #b91c1c;
    --fail-bg: #fee2e2;
    --fail-border: #fca5a5;
    --na: #6b7280;
    --na-bg: #f3f4f6;
    --na-border: #d1d5db;
    --accent: #1e40af;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1419;
      --fg: #e5e7eb;
      --muted: #9ca3af;
      --border: #374151;
      --card: #1a1f26;
      --code-bg: #11161d;
      --pass: #4ade80;
      --pass-bg: #14532d;
      --pass-border: #166534;
      --fail: #f87171;
      --fail-bg: #7f1d1d;
      --fail-border: #991b1b;
      --na: #9ca3af;
      --na-bg: #1f2937;
      --na-border: #374151;
      --accent: #60a5fa;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, "Segoe UI", "Noto Sans SC", "PingFang SC",
                 "Microsoft YaHei", sans-serif;
    font-size: 15px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  main {{
    max-width: 840px;
    margin: 0 auto;
    padding: 32px 24px 64px;
  }}
  h1, h2 {{ font-weight: 600; letter-spacing: 0.01em; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  h2 {{
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin: 28px 0 12px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 6px;
  }}
  code, pre, .mono {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", "Menlo", "Consolas",
                 monospace;
    font-size: 13px;
  }}
  code {{
    background: var(--code-bg);
    padding: 1px 6px;
    border-radius: 4px;
  }}
  .muted {{ color: var(--muted); }}
  .meta {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 6px 16px;
    color: var(--muted);
    font-size: 13px;
    padding: 8px 0 4px;
  }}
  .meta span.label {{ color: var(--fg); font-weight: 500; margin-right: 6px; }}

  .verdict {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    margin: 20px 0 8px;
  }}
  .pill {{
    display: inline-flex;
    align-items: center;
    padding: 8px 16px;
    border-radius: 999px;
    font-weight: 600;
    font-size: 14px;
    border: 1px solid transparent;
  }}
  .pill.pass {{
    color: var(--pass);
    background: var(--pass-bg);
    border-color: var(--pass-border);
  }}
  .pill.fail {{
    color: var(--fail);
    background: var(--fail-bg);
    border-color: var(--fail-border);
  }}
  .pill.na {{
    color: var(--na);
    background: var(--na-bg);
    border-color: var(--na-border);
  }}
  .rate {{
    margin-left: auto;
    font-family: ui-monospace, monospace;
    color: var(--muted);
    font-size: 13px;
  }}

  section.card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    margin: 12px 0;
    box-shadow: var(--shadow);
  }}
  dl {{ margin: 0; }}
  dt {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-top: 10px;
  }}
  dt:first-child {{ margin-top: 0; }}
  dd {{ margin: 4px 0 0; font-size: 14px; }}

  pre {{
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    margin: 8px 0 0;
    color: var(--fg);
    font-size: 13px;
    line-height: 1.5;
  }}

  .filename {{ font-size: 13px; color: var(--muted); margin-bottom: 4px; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }}
  th, td {{
    text-align: left;
    vertical-align: top;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }}
  th {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
  }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .canary-created {{
    color: var(--fail);
    font-weight: 600;
    font-family: ui-monospace, monospace;
  }}
  .canary-clean {{
    color: var(--pass);
    font-weight: 600;
    font-family: ui-monospace, monospace;
  }}
  img.screenshot {{
    display: block;
    max-width: 100%;
    height: auto;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--code-bg);
  }}

  footer {{
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
    line-height: 1.6;
  }}
  footer p {{ margin: 4px 0; }}

  @media (max-width: 480px) {{
    main {{ padding: 20px 14px 48px; }}
    .rate {{ margin-left: 0; width: 100%; }}
    th, td {{ padding: 6px 6px; font-size: 13px; }}
    pre {{ font-size: 12px; padding: 10px; }}
  }}
</style>
</head>
<body>
<main>
  <header>
    <h1>TRACE 评测报告</h1>
    <div class="meta">
      <div><span class="label">用例</span><code>{case_id}</code></div>
      <div><span class="label">目标</span>{target or '<span class="muted">—</span>'}</div>
      <div><span class="label">权限模式</span><code>{permission_mode}</code></div>
      <div><span class="label">时间</span><span class="mono">{ts}</span></div>
    </div>
  </header>

  <div class="verdict" role="group" aria-label="结论">
    <span class="{_PILL_CLASS.get(agent_sec, 'pill na')}">Agent Security：{agent_sec}</span>
    <span class="{_PILL_CLASS.get(sys_prot, 'pill na')}">System Protection：{sys_prot}</span>
    <span class="rate">failure_rate: {failure_rate}</span>
  </div>

  <section class="card">
    <h2>判定依据</h2>
    <dl>
      <dt>verdict_basis</dt>
      <dd>{verdict_basis or '<span class="muted">—</span>'}</dd>
      <dt>root_cause</dt>
      <dd>{root_cause_html}</dd>
    </dl>
  </section>

  <section class="card">
    <h2>注入内容</h2>
    <div class="filename">文件名：<code>{doc_filename or '—'}</code></div>
    <pre>{doc_content or ''}</pre>
  </section>

  <section class="card">
    <h2>正常任务</h2>
    <p>{benign_task or '<span class="muted">—</span>'}</p>
  </section>

  <section class="card">
    <h2>逐次证据</h2>
    <table>
      <thead>
        <tr>
          <th style="width:60px">Run</th>
          <th style="width:110px">Canary</th>
          <th style="width:180px">时间戳</th>
          <th>截图</th>
        </tr>
      </thead>
      <tbody>
        {evidence_rows}
      </tbody>
    </table>
  </section>

  <footer>
    <p>判定基于确定性行为证据（canary 文件存在性），非智能体自述。</p>
    <p>v1 边界：<code>system_protection</code> 基于 <code>permission_mode</code> 推断；HITL 弹窗检测在 v2 加入。</p>
  </footer>
</main>
</body>
</html>
"""
