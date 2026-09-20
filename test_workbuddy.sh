#!/bin/bash
# 一键测试 WorkBuddy（半自动化：需要人工登录）

# ========== 配置 ==========
export AGENTBAY_API_KEY="${AGENTBAY_API_KEY:-你的密钥}"  # 从环境变量读取，或直接写死
TARGET="workbuddy"
CASE_FILE="/TRACE/trace/cases/wb_inj_001.json"
OUTPUT_DIR="/TRACE/trace/results"

# ========== 自动执行部分 ==========
echo "🚀 开始 TRACE 评测流程..."

# 创建会话
echo "📦 创建沙箱会话..."
SESSION_JSON=$(python3 -m trace.cli session create --json 2>/dev/null)
SESSION_ID=$(echo "$SESSION_JSON" | jq -r '.session_id')
DESKTOP_URL=$(echo "$SESSION_JSON" | jq -r '.desktop_url')

if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "null" ]; then
    echo "❌ 会话创建失败"
    exit 1
fi

echo "✅ 会话已创建：$SESSION_ID"
echo ""

# 安装 WorkBuddy
echo "🔧 安装 $TARGET（可能需要几分钟）..."
python3 -m trace.cli provision --target $TARGET --session $SESSION_ID

echo ""
echo "=========================================="
echo "⚠️  需要人工登录"
echo "=========================================="
echo "请打开浏览器访问："
echo "$DESKTOP_URL"
echo ""
echo "登录完成后，回到这里按回车继续..."
read -p "> "

echo ""
echo "🧪 运行测试用例..."
mkdir -p "$OUTPUT_DIR"
python3 -m trace.cli run \
    --case "$CASE_FILE" \
    --out "$OUTPUT_DIR/result_$(date +%Y%m%d_%H%M%S).json" \
    --session $SESSION_ID \
    --report "$OUTPUT_DIR/report_$(date +%Y%m%d_%H%M%S).html"

echo ""
echo "🧹 清理会话..."
python3 -m trace.cli session rm $SESSION_ID

echo ""
echo "✅ 完成！结果保存在 $OUTPUT_DIR"
