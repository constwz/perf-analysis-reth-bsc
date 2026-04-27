# `scripts/` — 实测数据采集与分析

## 工具一览

| 脚本 | 作用 | 何时用 |
|---|---|---|
| `capture_flamegraph.sh` | 对运行中的 reth-bsc miner 采 perf 数据，生成带 Rust 符号的 flamegraph SVG | 每次需要 CPU 热点分析时 |
| `analyze_by_tps_stage.py` | 把 reth.log 聚合成按 TPS 档分桶的 stage 报告 | 每次压测完，作为产出报告 |

---

## `capture_flamegraph.sh`

**用法**：

```bash
# 等压测稳定进入目标 stage 后（至少 30 秒），在服务器上跑：
./capture_flamegraph.sh stage_07_2000tps          # 默认 60 秒
./capture_flamegraph.sh stage_10_highload 90      # 自定义 90 秒
```

输出：当前目录下的 `reth-bsc.<LABEL>.svg`。浏览器打开即可交互式查看。

**前置条件**（一次性设置，详见脚本头部注释）：

1. 用 maxperf profile 编译 reth-bsc，保留 Rust 符号（`debug = "line-tables-only"` + `strip = false`）
2. `kernel.kptr_restrict=0` + `kernel.perf_event_paranoid=-1`
3. 装 `FlameGraph` 工具到 `/root/FlameGraph`

**何时该重新采**：
- 每次启动参数改了 → 采新的，对比看热点变化
- 每次代码改了 → 必须 `cargo clean -p reth_bsc` 后重建 + 采新 SVG（旧二进制可能 strip 掉了符号）
- TPS 等级变了 → 不同 stage 热点分布不一样，至少在 stage_07（2000 TPS）和 stage_10（≥2600 TPS）各采一张

---

## `analyze_by_tps_stage.py`

**用法**：

```bash
# 单文件
python3 analyze_by_tps_stage.py reth.log --caller miner > stage_report.txt

# 自动发现 rotated 兄弟（reth.log + reth.log.1..N，含 .gz）
python3 analyze_by_tps_stage.py reth.log --caller miner > stage_report.txt

# 显式多文件
python3 analyze_by_tps_stage.py reth.log.5 reth.log.4 reth.log --caller miner

# 只读 reth.log，不发现兄弟
python3 analyze_by_tps_stage.py reth.log --no-rotations
```

输出字段含义见 [`../stage-report-fields-reference.md`](../stage-report-fields-reference.md)。

**TPS 分桶规则**（450ms slot）：

```
≤ 90 tx/块  → stage_01_<=200tps
91-180      → stage_02_400tps
181-360     → stage_03_800tps
361-540     → stage_04_1200tps
541-700     → stage_05_1500tps
701-855     → stage_06_1800tps
856-945     → stage_07_2000tps
946-1035    → stage_08_2200tps
1036-1125   → stage_09_2400tps
> 1125      → stage_10_>=2600tps
```

边界用相邻 TPS 目标的中点，让每块自然落入最接近的目标值。

**所需的 reth-bsc 启动 RUST_LOG**：

```
RUST_LOG="info,bsc::builder::timing=debug,bsc::builder::deadline=debug,
triedb::timing=debug,payload_builder=debug,pathdb::admission=debug,
engine::tree=debug,engine::persistence=debug"
```

少了任何一段的话对应字段会是 `n/a`。

---

## 一次完整的"压测 → 数据 → 分析"流程

```bash
# 服务器 A：压测节点
cd reth-bsc
cargo clean -p reth_bsc
RUSTFLAGS="-C force-frame-pointers=yes" make maxperf

# 启动节点（启动参数见 reth-bsc-2700tps-summary.md §2.1）
RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES=80000000 \
RUST_LOG="info,bsc::builder::timing=debug,..." \
  ./target/maxperf/reth-bsc node --chain bsc-qanet \
    --engine.disable-prewarming \
    --engine.persistence-threshold 256 \
    --engine.memory-block-buffer-target 128 \
    --txpool.pending-max-count 100000 \
    ... 2>&1 | tee reth.log

# 服务器 B 或同台：开始压测，按目标 TPS 阶梯运行

# 等链稳定进入目标 stage（如 2000 TPS）至少 30 秒后：
./capture_flamegraph.sh stage_07_2000tps

# 再等高负载 stage：
./capture_flamegraph.sh stage_10_highload 90

# 压测完成后聚合分析：
python3 analyze_by_tps_stage.py reth.log --caller miner > stage_report.txt

# 把 svg + stage_report 拷回本地分析
scp server:/path/to/reth-bsc.*.svg ./
scp server:/path/to/stage_report.txt ./
```

之后比对本仓库 [`../reth-bsc-2700tps-summary.md`](../reth-bsc-2700tps-summary.md) §3 / §6 的 baseline 数字判断改进效果。
