"""TRACE — 智能体行为层注入评测工具（供测评平台调用）。

与已有平台的分工：
  已有平台 = LLM 接口/文本层注入测试（看模型"说"什么）
  TRACE    = 沙箱内 Agent 行为层注入测试（看 agent "做"什么，行为证据判定）

调用方式：CLI + JSON
  python -m trace.cli run --case case.json --out result.json
"""
__version__ = "0.1.0"
