# reth-bsc 2700 TPS 性能分析 release

本目录是一份自包含的 reth-bsc 性能调优复盘，记录了 **从 2000 TPS 稳态推到 2700 TPS 稳态** 全过程的代码改动、启动参数、实测数据、flamegraph，以及下一阶段（3000 TPS 目标）的优化路线图。

## 目录结构

```
.
├── README.md                              ← 本文件（入口）
├── reth-bsc-2700tps-summary.md            ← ⭐ 主文档，从这里读
├── stage-report-fields-reference.md       ← Stage 报告字段说明手册（看 stage_report*.txt 时配套）
│
├── flamegraphs/                           ← perf 采样生成的带符号 SVG
│   ├── reth-bsc.stage07.svg               ← 2500 TPS 负载，B-1/B-2.1 修复前
│   ├── reth-bsc.2700-symbolized.svg       ← 2700 TPS 稳态(修复后，正常期)
│   └── reth-bsc.2700-1-symbolized.svg     ← 2700 TPS 稳态(修复后，compaction 期)
│
├── stage_reports/                         ← analyze_by_tps_stage.py 的输出
│   ├── stage_report1_2000tps.txt          ← 2000 TPS 基线
│   ├── stage_report_2500tps.txt           ← 2500 TPS(首次符号化 SVG 对应)
│   └── stage_report_2700tps.txt           ← 2700 TPS(B-1 + B-2.1 全部生效)
│
├── scripts/                               ← 实测数据采集 + 分析工具
│   ├── README.md                          ← 各脚本用法 + 完整压测流程
│   ├── capture_flamegraph.sh              ← 对运行中的 miner 采符号化 flamegraph SVG
│   └── analyze_by_tps_stage.py            ← 把 reth.log 聚合成分段 stage 报告
│
└── related/                               ← 本轮之前的历史分析文档
    ├── reth-bsc-2000tps-gap-classification.md
    ├── reth-bsc-2500tps-flamegraph-findings.md
    └── reth-bsc-vs-geth-bsc-final-summary.md
```

## 快速阅读路径

| 你的目标 | 读这几页 |
|---|---|
| 想知道 2000 → 2700 TPS 做了什么 | `reth-bsc-2700tps-summary.md` §1-§3 |
| 想知道下一步怎么推到 3000 TPS | `reth-bsc-2700tps-summary.md` §4-§5 |
| **看 stage 报告时不知道某字段什么意思** | **`stage-report-fields-reference.md`**（按字段名查）|
| 想看 flamegraph 证据 | `flamegraphs/` 下三张 SVG，浏览器直接打开，交互式 |
| **想自己采一张 flamegraph** | `scripts/capture_flamegraph.sh` + `scripts/README.md` |
| 想复现本文档的数据 | `reth-bsc-2700tps-summary.md` §D 附录 + `scripts/README.md` 末尾"完整流程" |
| 想知道之前的弯路 | `related/` 下的历史文档 |

## 涉及的代码仓库

| 仓库 | 分支 | 角色 |
|---|---|---|
| [`bnb-chain/reth-bsc`](https://github.com/bnb-chain/reth-bsc) | `fix/timestamp-drift` | BSC 节点应用层 |
| [`bnb-chain/reth`](https://github.com/bnb-chain/reth) | `feat/logs-on-develop` | reth 核心库（含 engine tree / persistence） |
| [`bnb-chain/reth-bsc-triedb`](https://github.com/bnb-chain/reth-bsc-triedb) | `feat/logs-on-develop` | TrieDB 后端 |

具体的 commit 列表见主文档 §1。

## 核心数字

| 指标 | `develop` 基线 | 当前 `fix/timestamp-drift` + 新参数 |
|---|---|---|
| 稳态 TPS | ~2000 | **~2700** |
| stage_07 `build_duration_ms` p999 | 1538ms | **767ms** |
| stage_07 超 450ms 比例 | 51.8% | 62.2%（但 p999 腰斩两次） |
| `PrewarmContext` CPU 占比 | 9.0% | 0% |
| tx pool sort CPU 占比 | 21.7% | 0% |

## 下一阶段路线图摘要

| 阶段 | 改动 | 预期 TPS | 工作量 |
|---|---|---|---|
| Phase 0.5 | moka cache 加到 80M + 启动预热热合约 | ~2850 | 半天 |
| Phase 1 | DiffLayers Bloom filter | ~2950 | 1-2 天 |
| Phase 2 | asm-keccak + Batch DiffLayer 写入 | ~3050 | 1 周 |
| Phase 3 | 流式 RLP + 增量 keccak | ~3180 | 1 周 |
| Phase 4 | moka weigher 按节点类型分权 | ~3280 | 1 周 |
| Phase 5 | Committer Arc::make_mut | ~3380 | 2 周 |

**目标 3000 TPS**：Phase 0.5 + Phase 1 + Phase 2 即可达到（累计约 1 周工作量）。

详细的每项改动做什么、风险、验收标准，见 `reth-bsc-2700tps-summary.md` §4-§6。
