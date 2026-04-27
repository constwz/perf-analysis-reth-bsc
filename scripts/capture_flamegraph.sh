#!/usr/bin/env bash
#
# 采样运行中的 reth-bsc miner，生成带 Rust 符号的 flamegraph SVG。
#
# ──────────────────────────────────────────────────────────────────────
# 前置条件（一次性设置，root 权限）
# ──────────────────────────────────────────────────────────────────────
#
# 1. 用 maxperf profile 编译 reth-bsc，确保保留 Rust 符号 + frame pointer。
#    fix/timestamp-drift 分支的 Cargo.toml 已配好（debug = "line-tables-only",
#    strip = false）。每次代码改动后必须 cargo clean 一次再重建：
#
#      cd reth-bsc
#      cargo clean -p reth_bsc
#      RUSTFLAGS="-C force-frame-pointers=yes" make maxperf
#
# 2. 让 perf 能读内核符号（一次性，root）：
#
#      sudo sysctl -w kernel.kptr_restrict=0
#      sudo sysctl -w kernel.perf_event_paranoid=-1
#
# 3. 装 FlameGraph 工具（一次性）：
#
#      sudo git clone https://github.com/brendangregg/FlameGraph.git /root/FlameGraph
#      # 拉不到时换镜像:
#      # sudo git clone https://gitee.com/mirrors/FlameGraph.git /root/FlameGraph
#
# ──────────────────────────────────────────────────────────────────────
# 使用
# ──────────────────────────────────────────────────────────────────────
#
#   ./capture_flamegraph.sh [LABEL] [DURATION_SECONDS]
#
# 例子：
#
#   # 等压测稳定进入 2000 TPS（stage_07）后采 60 秒
#   ./capture_flamegraph.sh stage_07_2000tps
#
#   # 采 90 秒（高负载峰值期适合用更长时间）
#   ./capture_flamegraph.sh stage_10_highload 90
#
# 输出：当前目录下的 reth-bsc.<LABEL>.svg。浏览器直接打开即可交互式查看
# （鼠标悬停看函数名 + 占比，点击放大子树，Ctrl-F 搜索）。
#
# ──────────────────────────────────────────────────────────────────────
# 采样时机建议
# ──────────────────────────────────────────────────────────────────────
#
# - 不要在压测 ramp-up 阶段采（数据不稳）
# - 等链稳定进入目标 stage 至少 30 秒后再开始
# - 默认 60 秒采样在 2000-2700 TPS 下能采到几千个 stack frame，足够分析
# - 高负载稳定性差时（stage_10）建议采 90-120 秒，平均掉偶发抖动

set -euo pipefail

LABEL="${1:-default}"
DURATION="${2:-60}"
FLAMEGRAPH_DIR="${FLAMEGRAPH_DIR:-/root/FlameGraph}"

# ── 1. 找运行中的 reth-bsc 进程 ──────────────────────────────────────
RETH_PID=$(pgrep -f 'target/maxperf/reth-bsc' | head -1 || true)
if [ -z "${RETH_PID:-}" ]; then
    echo "ERROR: 找不到运行中的 reth-bsc 进程（target/maxperf/reth-bsc）" >&2
    echo "       请先用 maxperf profile 启动 reth-bsc 后再来跑这个脚本。" >&2
    echo "       启动示例（参考 reth-bsc-2700tps-summary.md §2.1 启动参数）：" >&2
    echo "         ./target/maxperf/reth-bsc node --chain bsc-qanet ..." >&2
    exit 1
fi
echo "✓ Found reth-bsc PID: $RETH_PID"

# ── 2. 校验 FlameGraph 工具 ───────────────────────────────────────────
if [ ! -x "$FLAMEGRAPH_DIR/stackcollapse-perf.pl" ]; then
    echo "ERROR: FlameGraph 工具不在 $FLAMEGRAPH_DIR" >&2
    echo "       一次性安装：" >&2
    echo "         sudo git clone https://github.com/brendangregg/FlameGraph.git $FLAMEGRAPH_DIR" >&2
    exit 1
fi

# ── 3. 校验内核 perf 权限 ─────────────────────────────────────────────
KPTR=$(cat /proc/sys/kernel/kptr_restrict 2>/dev/null || echo "?")
PARANOID=$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo "?")
if [ "$KPTR" != "0" ] || [ "$PARANOID" != "-1" ]; then
    echo "WARN: 内核 perf 权限可能不够（kptr_restrict=$KPTR, perf_event_paranoid=$PARANOID）"
    echo "      如果采样后 SVG 全是 [unknown] 帧，跑这两条再重试："
    echo "        sudo sysctl -w kernel.kptr_restrict=0"
    echo "        sudo sysctl -w kernel.perf_event_paranoid=-1"
fi

OUTPUT="reth-bsc.${LABEL}.svg"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# ── 4. perf record ─────────────────────────────────────────────────────
echo "→ 采样 $DURATION 秒（PID=$RETH_PID, 99 Hz, dwarf 调用栈）..."
sudo perf record \
    -F 99 \
    -p "$RETH_PID" \
    --call-graph dwarf \
    -o "$TMPDIR/perf.data" \
    -- sleep "$DURATION"

PERF_SIZE=$(du -h "$TMPDIR/perf.data" | cut -f1)
echo "  perf.data: $PERF_SIZE"

# ── 5. perf script → folded → SVG ──────────────────────────────────────
echo "→ 解析符号（perf script）..."
sudo perf script -i "$TMPDIR/perf.data" > "$TMPDIR/out.perf"

echo "→ 折叠调用栈（stackcollapse-perf.pl）..."
"$FLAMEGRAPH_DIR/stackcollapse-perf.pl" "$TMPDIR/out.perf" > "$TMPDIR/out.folded"

FRAME_COUNT=$(wc -l < "$TMPDIR/out.folded")
echo "  独立调用栈数: $FRAME_COUNT"

echo "→ 生成 SVG（flamegraph.pl）..."
"$FLAMEGRAPH_DIR/flamegraph.pl" \
    --title "reth-bsc $LABEL ($DURATION s)" \
    --countname samples \
    "$TMPDIR/out.folded" > "$OUTPUT"

# ── 6. 健全性检查：确认 SVG 有 Rust 符号 ───────────────────────────────
RUST_FRAMES=$(grep -c "rust_eth_triedb\|revm_handler\|reth_bsc" "$OUTPUT" || true)
SVG_SIZE=$(du -h "$OUTPUT" | cut -f1)

echo
echo "══════════════════════════════════════════════════════════════════════"
echo "✅ 完成：$OUTPUT  ($SVG_SIZE, $RUST_FRAMES 个 Rust 符号帧)"
echo "══════════════════════════════════════════════════════════════════════"
echo

if [ "$RUST_FRAMES" -lt 100 ]; then
    echo "⚠️  Rust 符号帧太少（< 100），可能采样不到符号化数据："
    echo "    1. 确认编译用了 maxperf profile + RUSTFLAGS 'force-frame-pointers'"
    echo "    2. 确认 cargo clean 重建过，老的 strip 二进制不能用"
    echo "    3. SVG 里如果大块都是 [reth-bsc] 就是符号丢失"
    echo
fi

echo "用法："
echo "  • 浏览器打开 $OUTPUT 交互式查看（鼠标悬停 + 点击放大）"
echo "  • 或上传到 Google Doc / GitHub 作为分析证据"
echo "  • 用脚本提取 top-N 热点函数（参考 reth-bsc-2700tps-summary.md §4.2 的 Python 片段）"
