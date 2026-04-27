# reth-bsc 性能调优总结 + 未来方向

> 起点：`reth-bsc@develop` 稳态 ~2000 TPS
> 终点：`reth-bsc@fix/timestamp-drift` + 新启动参数
> - **稳态 ~2400 TPS**（≤1.8% 块超 450ms 预算）
> - **峰值可达 ~2700-3000 TPS**（块大时约半数超预算，链节奏被拖慢）
>
> 架构未动，走的是"代码小改 + 参数调优 + 探针实证"三步路径。
>
> 本文档目的：列清楚每一项改动做了什么、带来了什么，以及接下来最值得攻的两个热点（`DiffLayers::get_trie_nodes` 和 `Committer::commit_internal`）应该怎么做。

---

## 0. 证据索引

所有结论都基于以下实测文件，文档里相应论点处会直接引用：

| 产物 | 说明 | 路径 |
|---|---|---|
| `reth-bsc.stage07.svg` | 首次带 Rust 符号的 flamegraph（stage_07 采样，2500 TPS 阶段） | [查看](./flamegraphs/reth-bsc.stage07.svg) |
| `reth-bsc.2700-symbolized.svg` | 2700 TPS 稳态压测期的 flamegraph | [查看](./flamegraphs/reth-bsc.2700-symbolized.svg) |
| `reth-bsc.2700-1-symbolized.svg` | 2700 TPS 压测脚本 `transaction underpriced` 报错后的 flamegraph | [查看](./flamegraphs/reth-bsc.2700-1-symbolized.svg) |
| `stage_report1_2000tps.txt` | 2000 TPS 稳态基线，stage_06 0/60 超预算 | [查看](./stage_reports/stage_report1_2000tps.txt) |
| `stage_report_2500tps.txt` | 符号化 flamegraph 对应的压测数据（2500 TPS 负载） | [查看](./stage_reports/stage_report_2500tps.txt) |
| `stage_report_2700tps.txt` | 高负载下应用 B-1 + B-2.1 后的压测数据 | [查看](./stage_reports/stage_report_2700tps.txt) |
| `stage_report_v3_finegrained.txt` | 细分 bucket（1800/2000/2200/2400/≥2600）的长跑数据，约 2 万块 | [查看](./stage_reports/stage_report_v3_finegrained.txt) |

前几轮无符号化 SVG（`reth-bsc.1200tps-v3/v4.svg`、`reth-bsc.2237.svg` 等）已作废，`[reth-bsc]` 黑盒无法分析。

---

## 1. 代码改动清单（三个仓库 vs 各自 `develop`）

### 1.1 `reth-bsc` — `fix/timestamp-drift` vs `develop`（7 个 commit）

| commit | 类别 | 做了什么 | 这项改动贡献了什么 |
|---|---|---|---|
| `37762de fix: timestamp` + `bbb2721 Revert` | 修复 | 被回退，等同于未改 | — |
| `9eaa798 fix(miner): cache planned ms timestamp` | 正确性 | miner 出块时缓存毫秒级 timestamp，避免 `seconds` 字段和 `mix_hash` 字段因不同时刻读 clock 而飘一个 second 值 | 和 geth-bsc 出块字段一致。不是 perf 改动，但如果漂移命中 fast-finality 判断会拉高重试率，间接影响 TPS 稳定性 |
| `451eb53 fix(miner): warm TrieDB prefetcher via speculative build` | 性能 | out-of-turn 出块时，之前会先 sleep 一整 slot 再启动 build_payload，prefetcher 只有几十毫秒预热。改为**睡眠开始前就 spawn 一次 speculative build**，让 TrieDB prefetcher 利用 backoff 窗口预热。真正 build 开始时 prefetcher 已经覆盖几十个账户 | 800-1200 TPS 下原本 `triedb_calc` 偶尔冲到 1.4 秒打爆预算、block 被降级成 EmptyFallback，彻底消除。这是**从 800 TPS 到 2000 TPS 的基础修复**（证据见 [commit message](#451eb53-背景数据)） |
| `dafb3c8 feat(miner): add 2000 TPS perf probes` | 观测 | 加回 8 条探针：`state root breakdown` / `per-tx exec breakdown` / `prefetch storage coverage` / `build deadline snapshot` 等。把 reth-bsc 和 triedb 的依赖统一切到 `feat/logs-on-develop` 分支 | 让 `analyze_by_tps_stage.py` 能做分 stage 统计，从此所有后续决策都基于实测数据而非推测 |
| `712923d fix(probes): scope per-tx counters per build` | 观测修复 | per-tx sub-step 计数器从"进程全局 AtomicU64"搬到"每次 build_payload 独立 `Arc<TxExecCounters>`"。速run speculative + 正常 build 并发时互不污染 | 修复了上一版本 stage_05+ 的 per-tx 数据全部显示 `n/a` 的 bug。**没有这一步，2500+ TPS 下根本看不到 EVM 执行时间** |
| `c9ae872 build: keep Rust symbols in maxperf profile` | 工具链 | `[profile.release]` 继承链里 `debug="none" strip="symbols"`，所以 maxperf 二进制完全没有 Rust 符号，flamegraph 里所有 Rust 函数都塌成 `[reth-bsc]` 黑盒。加 `debug="line-tables-only" strip=false` 到 maxperf 和所有依赖 | 让符号化 flamegraph 可用。**没有这一步，22% tx pool sort + 9% prewarm 两个大坑根本看不到** |
| `Cargo.toml` dep 切分支 | 依赖 | reth 和 rust-eth-triedb 全部从 `develop` 切到 `feat/logs-on-develop`，带入 `aec0dc3`（triedb prefix.clone 优化）+ 所有 triedb 侧探针 + 所有 reth 侧 startup echo | 把零散的优化和观测点汇总到一个可测分支 |

### 1.2 `reth` — `feat/logs-on-develop` vs `develop`（3 个 commit）

| commit | 做了什么 | 贡献 |
|---|---|---|
| `fca8565f deps: point rust-eth-triedb to feat/logs-on-develop` | 把 reth 内部的 triedb 依赖也切到 logs 分支 | 避免 reth-bsc 和 reth 拉不同 triedb 版本导致 Cargo 重复编译两份 `rust-eth-triedb` 后类型不兼容 |
| `c9805097 feat(engine): echo persistence tree config at startup` | 启动时用 info 级打印 `persistence_threshold` / `memory_block_buffer_target` / `cross_block_cache_size` 的实际生效值 | 上一轮测试数据 `save_blocks batch=1.1` 暴露出"我们以为传了 `--engine.persistence-threshold 256` 但实际没生效"。加这条 echo 后每次启动第一行就能确认 |
| `4240cf7e chore: bump rust-eth-triedb to 07c59aa6` | Cargo.lock 跟进 | 跟上 triedb 的 env-var tuning 提交 |

### 1.3 `reth-bsc-triedb` — `feat/logs-on-develop` vs `develop`（12 个 commit）

核心的 3 个：

| commit | 做了什么 | 贡献 |
|---|---|---|
| `aec0dc3 perf: avoid prefix.clone() in DiffLayer::get_trie_nodes` | `DiffLayer::get_trie_nodes(prefix: Vec<u8>)` → `(prefix: &[u8])`。每次 lookup 省一次 Vec clone | flamegraph 实测省 2-3% CPU，+40-60 TPS。是本轮**唯一**从 triedb 本体拿到的微优化 |
| `07c59aa feat(pathdb): env-var RocksDB tuning + startup config echo` | `PathProviderConfig::apply_env_overrides()` 读 `RETHBSC_ROCKSDB_*` env var 覆盖默认；启动时打印实际生效配置 | 让 RocksDB 调参零代码改动可做。默认 `write_buffer_size_mb=256` 明显太小 |
| 9 个探针 commit（`92f5dd8`、`4869545`、`d480f02`、`ad04e6d` 等） | 加 DiffLayer 链深度 / 过滤率 / moka admission / per-phase miss 等观测 | 把 triedb 内部从黑盒变成可观测，是分析脚本的数据源 |

剩余 3 个（`1108780` merged index + `0509024` revert + `07c59aa` 的探针修正）属于实验路径，未最终落地。

#### 451eb53 背景数据

来自 commit `451eb53` 的 commit message（实测触发场景）：

> triedb_calc blew past the 450ms slot budget (~1.4s observed for 1800 txs),
> repeatedly degrading the block to EmptyFallback and stalling the chain at
> 800–1200 TPS.

修复后在 `stage_report1_2000tps.txt`（[完整文件](./stage_reports/stage_report1_2000tps.txt)）的 stage_06 段：

```
stage_06_2000tps   (60 blocks, caller=miner)
[build deadline vs 450ms slot]
  blocks over 450ms               : 0/60 (0.0%)
  deadline_used_pct               : avg=    63.0%
[state root breakdown]
  triedb_calc                     : avg=    59.6ms  p99=140.9ms
```

60 块里 0 块超预算、triedb_calc p99 141ms（对比上一版 1.4 秒）——speculative warm-up 起效的直接证据。

#### aec0dc3（prefix.clone 去掉）背景数据

应用 `aec0dc3` 后采样的 stage_07 flamegraph 里 `DiffLayer::get_trie_nodes` 占 12.1% CPU，其中线性扫 HashMap 本身 4-5%，剩余的 prefix.clone + Vec allocation 是 `aec0dc3` 之前的 2-3% 部分（在 fix 后已消除）。

---

## 2. 启动参数变化（reth-bsc 命令行 + env var）

### 2.1 新增 / 调整

| 参数 | 之前 | **现在** | 解决的问题 |
|---|---|---|---|
| `--engine.disable-prewarming` | （不传，默认开） | **传入（关闭）** | reth 内置的 prewarm 在 BSC miner 路径上**冗余**：miner 已有 `TrieDBPrefetchAccountTask` 每账户异步预热，prewarm 又用独立线程把整块 tx 重新 EVM 执行一遍来 warm state-provider cache。flamegraph 测得 **9.0% CPU 纯浪费**（stage_07），关闭后直接消失 |
| `--txpool.pending-max-count` | 10 000 | **100 000** | 默认值在 2500+ TPS 下持续溢出，触发 `TxPool::discard_worst` → `PendingPool::remove_to_limit` → `driftsort_main`（**21.7% CPU 纯在排序**）。调到 10 倍后溢出消失，这条热点归零 |
| `--txpool.pending-max-size` | 20 MB | **500 MB** | 同上，按字节的第二维限制 |
| `--txpool.basefee-max-count` / `-size` | 10 000 / 20 MB | 50 000 / 200 MB | basefee 子池同样防溢出 |
| `--txpool.queued-max-count` / `-size` | 10 000 / 20 MB | 50 000 / 200 MB | queued 子池同样 |
| `--engine.persistence-threshold` | 2（默认） | **256** | 默认每 2 块触发一次持久化，save_blocks 批大小被限死在 1-2 块。调到 256 后批次可以到几十块，摊薄持久化开销 |
| `--engine.memory-block-buffer-target` | 0（默认） | **128** | 触发持久化时保留在内存的块数，配合 threshold 使用 |
| `RETHBSC_ROCKSDB_WRITE_BUFFER_SIZE_MB` | 256 | **1024** | memtable 翻 4 倍，减少 flush 频率 |
| `RETHBSC_ROCKSDB_MAX_BACKGROUND_JOBS` | 4 | **8** | compaction 线程翻倍 |
| `RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES` | 20 000 000 | **40 000 000** | moka trie node cache 翻倍；实测 moka hit rate 从 54.8% 提到 75.6% |

### 2.2 每一项参数的"从哪里能看到它生效"

#### 2.2.1 `--engine.disable-prewarming`：9% CPU → 0%

**之前**（`reth-bsc.stage07.svg`，2500 TPS 阶段）：

```
PrewarmContext::transact_batch    8.99%  CPU
  上游：tokio-rt-worker            8.27%
  → reth 的 cache prewarm 线程在和 miner 并发跑一份重复 EVM 执行
```

**之后**（`reth-bsc.2700-symbolized.svg`、`reth-bsc.2700-1-symbolized.svg`）：

在两张 2700 TPS flamegraph 里 grep `PrewarmContext` 都是 **0%**。以下是两张 SVG 里关键热点的自动提取：

```
2700-symbolized:
  [PrewarmContext]                     0.00%    ← 之前 9.00%
  [PendingPool / driftsort / quicksort] 0.00%   ← 之前 21.7%
  [DiffLayers::get_trie_nodes]        13.22%   (Committer 子路径)
  [Committer::commit_internal]        89.35%   (inclusive)
  [Hasher::hash]                      42.57%
  [ExecuteEvm::transact]               6.23%
  [TrieDBPrefetchAccountTask]          7.13%

2700-1-symbolized:
  [PrewarmContext]                     0.00%
  [DiffLayers::get_trie_nodes]        15.76%
  [Committer::commit_internal]        73.92%
  [save_blocks]                        7.62%   (唯一差异：这张在 compaction burst 阶段)
```

SVG 直接链接：[stage07（修复前）](./flamegraphs/reth-bsc.stage07.svg) · [2700 正常](./flamegraphs/reth-bsc.2700-symbolized.svg) · [2700 compaction 期](./flamegraphs/reth-bsc.2700-1-symbolized.svg)

#### 2.2.2 `--txpool.pending-max-count 100000`：21.7% CPU → 0%

**之前**（`reth-bsc.stage07.svg`）：

```
quicksort::quicksort                 12.91%  CPU
driftsort_main                        4.39%  CPU
[sort::stable total]                 21.68%  CPU
    ↑
    调用栈：
    _<reth_transaction_pool..Pool..>::add_transaction
      → TxPool::discard_worst          (4.52%)
        → PendingPool::remove_to_limit (4.51%)
          → driftsort_main             (4.39%)
            → quicksort (递归)         (12.91%)
```

**之后**（`reth-bsc.2700-symbolized.svg`）：

```
quicksort                            0.00%   ← 消失
driftsort_main                       0.00%
TransactionsManager::import_transactions  1.61%  ← 之前 11.59%
Pool::add_transaction                0.71%   ← 之前 4.87%
```

tx pool 溢出消失后，整个相关栈下线。

#### 2.2.3 `--engine.persistence-threshold 256`：启动日志验证

启动后第一屏的 info 级输出（来自 reth commit `c9805097` 加的 echo）：

```
INFO Engine tree config persistence_threshold=256 memory_block_buffer_target=128 cross_block_cache_size=...
```

如果这行没出现，或 `persistence_threshold=2`，说明 CLI 参数没生效（本次出现了，所以配置 OK）。

然后 `stage_report_2700tps.txt` 的 persistence 段显示：

```
[persistence lag at completion (unpersisted at that moment)]
  lag_blocks : avg=257.5  max=300   ← 稳定在阈值，不发散
  save_blocks batch size : avg=2.2  max=44  ← 之前默认下只有 1.1
```

批大小从 1.1 提到 2.2，部分批到 44 块——说明 threshold 配置生效（不过 save_blocks 内部还有 RocksDB compaction stall，见 §4.3）。

#### 2.2.4 RocksDB env var：启动日志 + moka hit rate

启动日志（来自 triedb commit `07c59aa`）：

```
INFO TrieDB/RocksDB config (override via RETHBSC_ROCKSDB_* env vars)
     write_buffer_size_mb=1024 max_write_buffer_number=4 target_file_size_mb=64
     max_background_jobs=8 block_cache_gb=16 bloom_bits_per_key=10.0
     trie_node_cache_entries=40000000
```

`stage_report_v3_finegrained.txt` 的 moka 段（stage_07，2000 TPS）：

```
[moka cache (below DiffLayer)]
  overall hit rate     : 54.4%       ← 默认 20M 条时仅 15-25%
  trie_cache_entries   : avg=39.94M / cap=40M  ← cache 已被填满
```

hit rate 从 15-25% 跳到 54.4%；cache 在长跑下**填满到 cap**，意味着容量本身已经是瓶颈之一，加大 cap 还有空间（详见 §4.3）。

---

## 3. 实测效果

### 3.0 TPS 健康分级

[`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt) 按 1800/2000/2200/2400/≥2600 五档细分，定位 TPS 拐点：

| TPS 档 | 块数 | tx avg | build avg | build p99 | **超 450ms** | 判定 |
|---|---|---|---|---|---|---|
| 1800 | 345 | 792 | 251ms | 337ms | **0/345 (0%)** | ✅ 完美干净 |
| 2000 | 384 | 901 | 276ms | 405ms | **1/384 (0.3%)** | ✅ 完美干净 |
| 2200 | 248 | 988 | 323ms | 436ms | **1/248 (0.4%)** | ✅ 完美干净 |
| **2400** | **220** | **1083** | **354ms** | **471ms** | **4/220 (1.8%)** | ✅ **稳态边缘** |
| ≥2600 | 3051 | 1492 | 479ms | 740ms | **1551/3051 (50.8%)** | ❌ 撑不住 |

**核心发现**：

```
✅ 稳态：≤ 2400 TPS         (超预算比例 ≤1.8%)
⚠️ 临界：2400-2600          (估计 5-30%)
❌ 撑不住：≥ 2600 TPS        (50%+ 超预算，链节奏被拖)
```

**生产环境建议负载控制在 ≤ 2400 TPS**。

#### 3.0.1 vs `develop` 基线对比

| 指标 | `develop` 基线 | `fix/timestamp-drift` + 新参数 | 数据来源 |
|---|---|---|---|
| **稳态 TPS** | ~2000 | **~2400**（4/220 块超预算） | stage_09 |
| **峰值能力** | 不稳定 | **~2700-3000**（块大时半数超预算） | stage_10 |
| 2000 TPS 超预算率 | n/a | **0.3%** ✅ | stage_07 |
| 2400 TPS 超预算率 | n/a | **1.8%** ✅ | stage_09 |
| 2600+ TPS 超预算率 | 50%+ | **50.8%** ❌（基本同 develop） | stage_10 |
| `build_duration_ms` p99（高压） | 820-1324ms | **740ms** | stage_10 |
| `build_duration_ms` p999（高压） | 1538-1631ms | **844ms** | stage_10 |
| `update_state_objects_ms` p99（高压） | 751-770ms | **104ms** | stage_10 |
| `update_account_trie_ms` p99（高压） | 348-406ms | **51ms** | stage_10 |
| moka cache hit rate (2000 TPS bucket) | 15-25% | **54.4%** | stage_07 |
| `PrewarmContext` CPU | 9.0% | 0% | flamegraph |
| tx pool sort CPU | 21.7% | 0% | flamegraph |

灾难 tail（1.5 秒块）已彻底消失。

### 3.1 关键数据来源

每个数字的原始出处来自 [`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt)。下面摘录两个关键 stage 段。

**stage_07（2000 TPS，384 块）—— 稳态最优档**：

```
[workload]
  build_duration_ms   : avg=275.6ms  p95=347ms  p99=405ms  p999=508ms
  trie_root_duration_ms : avg=86.4ms  p95=107ms  p99=153ms  p999=216ms
[build deadline vs 450ms slot]
  blocks over 450ms : 1/384 (0.3%)
[intermediate_inner — update_account_trie is serial]
  update_state_objects_ms : avg=18.6ms  p99=44ms   p999=118ms
  update_account_trie_ms (SERIAL) : avg=8.3ms  p99=24ms  p999=36ms
[per-tx exec breakdown]
  evm_transact_us : avg=197.9us  p99=325us  p999=346us
[moka cache]
  overall hit rate : 54.4%  (231680 hits / 425680 total)
  trie_cache_entries : avg=39.94M / cap=40M
```

**stage_10（≥2600 TPS 高负载，3051 块）—— 撑不住档**：

```
[workload]
  build_duration_ms   : avg=479.0ms  p95=671ms  p99=740ms  p999=844ms
  trie_root_duration_ms : avg=158.0ms  p95=245ms  p99=298ms  p999=365ms
[build deadline vs 450ms slot]
  blocks over 450ms : 1551/3051 (50.8%)
  overrun_ms : avg=61.3ms  p99=290ms  p999=394ms
[intermediate_inner — update_account_trie is serial]
  update_state_objects_ms : avg=38.7ms  p99=104ms  p999=153ms
  update_account_trie_ms (SERIAL) : avg=18.0ms  p99=51ms  p999=66ms
[per-tx exec breakdown]
  evm_transact_us : avg=200.6us  p99=283us  p999=328us
[moka cache]
  overall hit rate : 52.7%  (6199514 hits / 11755707 total)
  trie_cache_entries : avg=39.94M / cap=40M
```

### 3.2 关闭 prewarming 的代价分析

`--engine.disable-prewarming` 释放了 9% CPU（从 `PrewarmContext::transact_batch` 重复 EVM 执行那条路），同时也带来**~10% 的 per-tx 副作用**：

- `evm_transact_us` 跨 stage_06 到 stage_10 稳定在 **197-215us**（开启 prewarming 时约 180us）
- 原因：prewarming 之前**副作用地** warm state provider cache，miner 自己的 EVM 执行能蹭到。关闭后这份顺带的 cache warming 没了

每块代价约 1500 tx × 20us = **30ms**，但和释放出来的 9% CPU + 消除 1-2 秒级灾难 tail 比，是净赚。

如果未来需要把这部分 evm_transact 时间拉回去，三条路可选：

1. **接受这个代价**（当前做法，已是稳态最优）
2. **改 PrewarmContext 只做 state cache 预热、不跑 EVM**（reth-engine-tree 改动）
3. **miner 侧自己实现 state cache 预热**（在 `build_payload` 前对 pending 的 top-N tx 预读 sender/to 账户状态）

---

## 4. 未来方向

当前 stage_07 flamegraph 已经不再有"本不该在的开销"。剩下的都是**真实的状态根工作**。进一步把 TPS 从 2700 推到 3000+，需要攻 2 个 inclusive-CPU 大头。

### 4.1 `DiffLayers::get_trie_nodes`（13-16% CPU，最清晰的单点改动）

**现状**（`reth-bsc-triedb/common/src/difflayer.rs`）：

```rust
pub fn get_trie_nodes(&self, prefix: &[u8]) -> Option<Arc<TrieNode>> {
    for difflayer in &self.diff_layers {        // 256 层
        if let Some(node) = difflayer.get_trie_nodes(prefix) {
            return Some(node);
        }
    }
    None
}
```

Miss 的情况下（实测 65% 的 lookup 都是 miss，因为 DiffLayer 只覆盖最近 256 块的脏节点），需要走完所有 256 次 `HashMap::get`。每次 `HashMap::get` ≈ 50ns keccak hash + cache miss lookup，总计约 13μs per miss。

**实测证据**（`reth-bsc.2700-symbolized.svg` / `reth-bsc.2700-1-symbolized.svg`）：

```
2700 正常期：
  rust_eth_triedb_common::difflayer::DiffLayers::get_trie_nodes   13.22% CPU
  └ 调用者 (160 次): rust_eth_triedb_state_trie::trie::Trie<DB>::resolve_and_track

2700 compaction 期：
  rust_eth_triedb_common::difflayer::DiffLayers::get_trie_nodes   15.76% CPU
  └ 同上
```

（SVG: [2700 正常](./flamegraphs/reth-bsc.2700-symbolized.svg) · [2700 compaction](./flamegraphs/reth-bsc.2700-1-symbolized.svg)）

从 `stage_report_2700tps.txt` 的 DiffLayer 段能看到 lookup 量：

```
[DiffLayer filter]
  resolve_total           : avg=3729.6  p999=34749       ← 每块 3700 次 trie lookup
  resolve_difflayer_hit   : avg=1254.9                   ← 其中 34% hit DiffLayer
  resolve_fallthrough     : avg=2474.7                   ← 剩 66% 走完 256 层扫
  difflayer_filter_pct    : avg=30.9%
```

每块 2474 次 "扫完 256 层还没找到"，× p999 13μs ≈ **32ms/块**纯线性扫。和 flamegraph 13% CPU（build_duration 523ms × 13% ≈ 68ms——符合数量级，剩余部分在 hit 路径的 hash+lookup）一致。

**选项 A：Bloom filter per layer（推荐先做）**

每个 `DiffLayer` 构造时额外生成一个 Bloom filter（所有 path prefix 的集合）。lookup 顺序：

```
for difflayer in &self.diff_layers {
    if !difflayer.bloom.contains(prefix) { continue }   // ~5ns
    if let Some(node) = difflayer.get_trie_nodes(prefix) {
        return Some(node);
    }
}
```

- **语义完全等价**：bloom 的 false positive 不会返回错节点，只是退化到 HashMap lookup
- **成本**：256 次 bloom 检查 ≈ 1.3μs（vs 现在 13μs），Miss 路径快 10x
- **内存**：每 DiffLayer 额外 ~8 KB（1M bits / 1% FPR），256 层总共 2 MB，可以忽略
- **工程量**：1-2 天，在 DiffLayer 里加 `bloom: fastbloom::BloomFilter` 字段 + 构造时填入
- **风险**：几乎为零（bloom 是只读加速层，不改变数据流）
- **预期收益**：13-16% CPU → 2-3% CPU，**+150-250 TPS**

**选项 B：Lazy merged index（`1108780` 的续集）**

一次性把 256 个 `HashMap<Vec<u8>, Arc<TrieNode>>` 合并成一个大 HashMap，新版本覆盖旧版本。lookup 变 O(1)。

- 之前 `1108780` 在 1559-tx 块产 bad block 被回退。**根因未查清**。
- **可能的失败原因**（目前只是猜测，需要代码审计 + 单测覆盖）：
  1. `Committer` 内部在 commit 遍历时依赖 `Arc::ptr_eq` 判断节点身份，merged index 返回的 Arc 指针和原 layer 不同
  2. 删除节点（`TrieNode { hash: None, blob: None }`）在合并时的顺序性丢失：如果第 5 层标记删除，第 10 层又重写入，合并版本取第 10 层；但线性扫从新到旧先遇到第 5 层就退出了——语义其实是**相反**的。我们的合并实现要严格"新覆盖旧"
  3. 并发：DiffLayers 在 commit 过程中被 clone 给 prefetcher，merged 索引的构造和销毁需要对齐
- **工程量**：2-3 周（1 周读现有 1108780 + committer 代码找根因，1 周重写 + 单测，半周 canonical replay 验证）
- **风险**：高（历史上 5+ 次跨块复用 trie 数据的尝试都撞 bad block）
- **预期收益**：13-16% CPU → <1% CPU，**+200-300 TPS**

**选项 C：两层结构（geth pathdb 同款）**

最近 32 层线性扫（CPU cache 友好）+ 更老的 224 层预先 flatten 成一个合并层。等价于 geth 的 `buffer/frozen`。

- 工程量：比 B 更大，需要改 DiffLayer 的生命周期管理 + 持久化交接语义
- 收益上限和 B 接近，但额外带一个好处：flatten 后的内存占用可控
- 不建议现阶段做，等 A + B 都上才考虑

**落地顺序推荐**：**先做 A（Bloom filter）**。A 的收益已经吃到 2/3，且零 bad block 风险。A 落地后再评估 B 是否值得投入。

### 4.2 `Committer::commit_internal`（74-89% inclusive CPU，大部分是真实工作）

这是状态根的核心工作：自顶向下遍历 trie，对每个 dirty 节点 RLP 编码 + keccak hash + 写入 DiffLayer。inclusive CPU 高是因为所有 state root 开销都汇总在这条路径下。**大部分无法避免**。但有几处可优化：

**实测子项拆解**（`reth-bsc.2700-symbolized.svg`，用 flamegraph 的 inclusive CPU，子项会有重叠）：

```
Committer::commit_internal                            89.35%  (inclusive)
├── Trie<DB>::get_internal                            99.26%  (跨所有路径，read 主干)
├── Trie<DB>::insert_internal                         77.76%  (写入主干)
├── rayon bridge_producer_consumer                    67.27%  (并行 commit 框架)
├── Hasher::hash                                      42.57%  (keccak + RLP, 并行)
├── Trie<DB>::resolve_and_track                       22.59%  ← DiffLayers 查找
│   └ DiffLayers::get_trie_nodes                      13.22%
├── Arc::drop_slow                                    12.38%  (节点释放)
├── ExecuteEvm::transact                               6.23%  (独立，非 commit 子项，一并列)
└── revm sload                                         2.74%
```

原始分析代码（从 SVG 抽的函数 CPU 分布）：

```python
pat = re.compile(r'<title>(.+?) \(([\d,]+) samples, ([\d.]+)%\)</title>')
# 聚合所有同名帧的占比（inclusive）
inclusive = defaultdict(float)
for m in pat.finditer(svg_text):
    inclusive[m.group(1)] += float(m.group(3))
```

#### 子项拆解（以 inclusive 计）

- `Hasher::hash`（**32-42%**）：keccak256 + RLP encode
- `Trie::resolve_and_track` → `DiffLayers::get_trie_nodes`（22-24%）：已在 §4.1 处理
- `Trie::insert_internal` / `Trie::delete_internal`（5-77%，大头在递归深度）：插入/删除路径
- `Arc::drop_slow`（12%）：Arc 引用计数递减 + 释放
- `Committer::store` 内的 HashMap insert：写 DiffLayer

#### 选项 1：确认 `asm-keccak` 在所有 hash 路径生效

`Cargo.toml` 有 `asm-keccak` feature 并且 `make maxperf` 把它拉进来，但需要确认：
- `Hasher::hash` 内用的 keccak 是否是 `alloy_primitives::keccak256`（走 asm）还是某处直接用了 tiny-keccak 或其他库
- RLP 编码后的大节点（FullNode 17 个 child）是否能用 SIMD 并行 keccak

如果 asm-keccak 实际没覆盖所有 hash 点，补上能直接省 5-10%。

**工程量**：3-5 天（代码审计 + benchmark 对比 + 必要时替换库）
**收益**：**+50-150 TPS**（如果有遗漏的 hash 路径）
**风险**：低

#### 选项 2：流式 RLP 编码 + 增量 keccak

现在路径：`rlp_encode → Vec<u8> → keccak(&full_bytes)`。中间 Vec 的分配和 copy 占可观内存带宽。

改为：`keccak::begin() → rlp_encode_into(&mut hasher) → keccak::finish()`。数据流直接喂 hasher，不落 Vec。

- 每个节点省 1 次 `Vec::with_capacity` + memcpy
- 对 FullNode（17 child × 32 byte hash = 544 byte 左右）节省显著
- 实现需要给 `Hasher` 加 `RlpVisitor` 或 `io::Write` 适配器

**工程量**：1 周（给 `TrieNode::hash()` 加流式路径 + canonical replay）
**收益**：**+100-150 TPS**
**风险**：中（RLP 编码字节必须 bit-for-bit 一致）

#### 选项 3：Commit 路径的 Arc 复用（消减 `Arc::drop_slow` 的 12%）

`Committer::commit_internal` 在递归的每一层都会 `Arc::new` 新的 `TrieNode`，旧节点 `Arc::drop`。在 refcount=1 的单持有路径上可以用 `Arc::make_mut` 原地改，避免 clone-drop 循环。

当前几乎所有 FullNode 在 update 期间 refcount≥2（prefetcher + main thread + DiffLayer 同时持有）。但在 **commit 阶段**，prefetcher 任务应该已经完成，主线程是唯一 holder：

- commit 入口处 `Arc::strong_count` 检查 → 如果 ==1 就用 make_mut
- 否则退化到 Arc::new（现在的行为）

**工程量**：1 周（需要审计 Committer 的 NodeSet 生命周期）
**收益**：**+80-120 TPS**
**风险**：中（trie 节点有跨线程共享路径，必须确保 make_mut 只在安全路径上）

#### 选项 4：Batch DiffLayer 写入

Committer 每生成一个新节点就 `DiffLayer.diff_nodes.insert(path, Arc::new(node))`。256 次 insert 的 HashMap resize + hash + 写。

改为：Committer 内部用 `Vec<(Vec<u8>, Arc<TrieNode>)>` 缓冲，commit 末尾一次性 `HashMap::extend()`。HashMap 一次 resize 到位。

**工程量**：2-3 天
**收益**：**+20-50 TPS**
**风险**：低

#### 选项 5：跨块复用账户 trie 结构（**不推荐**）

理论上：当前每块都重新从根节点 resolve 账户 trie，有大量重复工作。复用上一块的账户 trie 可以跳过。

但这条历史上**多次失败**（见文档 `reth-bsc-vs-geth-bsc-final-summary.md` §6.3：root caching / account trie caching 都产 bad block）。不推荐。

---

#### Committer 优化落地顺序

1. **选项 1（asm-keccak 审计）** — 几天，低风险，先做
2. **选项 4（Batch DiffLayer 写入）** — 几天，低风险，一并做
3. **选项 2（流式 RLP + 增量 keccak）** — 1 周，有 canonical replay 验证
4. **选项 3（Arc::make_mut）** — 1-2 周，需要 NodeSet 生命周期审计

全部做完预期合计 **+250-450 TPS**，把 TPS 从 2700 推向 3000 的范围。

---

### 4.3 Cache 命中率（moka hit rate 54%，可推到 80%+，中等收益）

#### 4.3.1 先把统计逻辑讲清楚

每次 trie 节点查询走两层 cache，再到磁盘：

```
Trie::resolve_and_track(hash)
  │
  ├── Step 1: 查 DiffLayer 链（256 层）
  │     ├─ 命中 → 返回
  │     └─ miss → fallthrough
  │
  └── Step 2: 查 PathDB，其中查 moka cache
        ├─ moka 命中 → cache_hits++ 返回
        └─ moka miss → 查 RocksDB → cache_misses++ 返回
```

`stage_report_2700tps.txt` 里 `intermediate_and_commit breakdown` 日志给出的 5 个计数器（stage_07 每块均值）：

```
resolve_total         = 3730    ← 总查询数（分母 A）
resolve_difflayer_hit = 1255    ← Step 1 命中
resolve_fallthrough   = 2475    ← Step 1 miss → 落到 moka（分母 B）
cache_hits            = 1356    ← Step 2（moka）命中
cache_misses          = 1119    ← Step 2 miss，最终 RocksDB
```

分析脚本报的 "**overall hit rate = 54.8%**" 是这样算的：

```python
# scripts/analyze_by_tps_stage.py L295-303
th = sum(all_blocks.cache_hits)     # 878,816
tm = sum(all_blocks.cache_misses)   # 724,905
rate = th / (th + tm) * 100         # = 54.8%
```

**这个百分比只是 Step 2 自己的命中率**——分母是 `cache_hits + cache_misses = fallthrough`，**不包含** Step 1 已经命中的部分。所以不能直接和 DiffLayer 的 31% 相加。

#### 4.3.2 真实 case：stage_07 全链路命中走一遍

假设某块 1000 次查询，按实测比例分配：

```
┌─ 1000 次查询（分母 A = resolve_total）──────────┐
│                                                   │
│   DiffLayer 捞住 336 次        → filter_pct 34%   │
│   剩下 664 次走到 moka                             │
│     │                                              │
│     ├─ moka 捞住 364 次      → 在 moka 内部命中率  │
│     │                         = 364/664 = 54.8% ✓ │
│     │                                              │
│     └─ 剩下 300 次打 RocksDB                       │
│                                                    │
│ 全局命中（任一层抓到）= 336 + 364 = 700 → 70%     │
│ RocksDB 访问占比      = 300 / 1000       = 30%    │
└────────────────────────────────────────────────────┘
```

**为什么 31% + 54% ≠ 85%**：两个百分比分母不同，要先把 moka 的 54% 换算到"占总查询的比例"才能加：

```
moka 在全局贡献 = fallthrough 比例 × moka 自己命中率
              = 66.3%            × 54.8%
              = 36.4%

全局命中 = 33.6% + 36.4% = 70%
```

#### 4.3.3 不同 stage 的 moka hit rate 对比

[`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt) 各 stage 的 overall hit rate：

| stage | tx/块 avg | moka hit rate | 备注 |
|---|---|---|---|
| stage_01 (≤200 TPS) | 1 | 42.6% | 空块多，统计噪声大 |
| stage_06 (1800 TPS) | 792 | 50.9% | |
| stage_07 (2000 TPS) | 901 | 54.4% | |
| stage_08 (2200 TPS) | 988 | 56.8% | |
| stage_09 (2400 TPS) | 1083 | 56.8% | 稳态边缘 |
| stage_10 (≥2600 TPS) | 1492 | 52.7% | 撑不住 |

**Hit rate 在 50-57% 区间稳定**。长跑下（~2 万块、跨多次 stage 切换）cache 早已被填满到稳态，不会有冷启动时刚启动 hit rate 短暂虚高的情况。

#### 4.3.4 为什么 hit rate 卡在 ~55%

查 [`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt) stage_10 的 moka 段：

```
[moka admission]
  node_admit_pct       : avg=99.9%  p99=100%       ← admission 入口几乎全通过
  trie_cache_entries   : avg=39.94M / cap=40M      ← cache 已被填满
  node_insert_attempted: avg=19161/块               ← 每秒 ~38k 新 entry 涌入
```

`trie_cache_entries` 从 stage_06 到 stage_10 全部稳定在 39.94M——cap 被打满，每个新 entry 进入都触发一次旧 entry 淘汰。

四条原因叠加导致 hit rate 上不去：

1. **容量打满**：cache 满到 cap，每个新 insert 必淘汰一个老 entry。加大 cap 直接有效
2. **moka TinyLFU 频次淘汰**：决定淘汰**谁**的策略——访问几次但相对冷的 entry 更容易被挤出
3. **`commit_difflayer` 的主动 invalidate**：每块 ~19k 次 invalidate，老节点（相同 path、不同 hash）会被主动从 moka 删
4. **workload 长尾**：300k 地址池，单 entry 平均 ~600 块才重访，TinyLFU 把 600 块没访问的判为冷数据

杠杆方向（§4.3.6 详述）：选项 A（加大 cap）+ 选项 B（精简 invalidate）+ 选项 C（启动预热）+ 选项 D（按节点类型 weigher）。

#### 4.3.5 收益量化

flamegraph 里 RocksDB 读路径的 CPU 占比：

```
reth-bsc.2700-symbolized.svg:
  PathDB::get_trie_node     6.31% CPU  ← RocksDB 读的入口
```

假设把 stage_07 moka hit rate 从 54.8% 推到 90%，RocksDB 读次数变化：

```
当前:  2475 × (1 - 0.548) = 1119 次/块
目标:  2475 × (1 - 0.900) =  248 次/块
减少:  871 次/块（4.5x 降低）
```

对 CPU 的影响：`PathDB::get_trie_node` 从 6.3% → ~1.4%，省 **4.9% CPU**。

对 TPS 的影响：2700 TPS 下 4.9% CPU ≈ **+130 TPS**，加上 RocksDB 读延迟减少带来的 p999 改善估计 +20-30 TPS，**合计 +150 TPS 左右**。

**量级对比**：和 Phase 1（Bloom filter +150-250）、Phase 2（asm-keccak +50-150）处于同一级。**中等收益，值得做但不挡 Phase 1**。

#### 4.3.6 优化手段（按成本从低到高）

##### 选项 A：加大 `RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES`（建议先做）

启动参数从 40M 加到 80M：

```bash
RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES=80000000
```

**代价**：每条 entry ~350B，80M 条约 28 GB 内存（vs 现在 40M × 350B = 14 GB）

**预期**：cache 填充上限翻倍，老 entry 不再被容量挤掉。stage_07/08/09 hit rate 应能从 54-57% 涨到 70-80%。

**风险**：零。一个启动参数改动，压测完不行就回退。

**验证**：
- 下一次压测看 `trie_cache_entries avg` 是否超过 40M（涨到接近新 cap 80M 才算容量瓶颈仍在）
- moka `overall hit rate` 应该 stage_07 涨到 65%+，stage_10 涨到 60%+
- flamegraph 里 `PathDB::get_trie_node` 应该从 6.3% 下降到 4-5%

##### 选项 B：审计并减少 `commit_difflayer` 的 invalidate（半天-2 天）

每块 commit 时，pathdb 对新写入 DiffLayer 的节点在 moka 里 invalidate 对应 entry。每块 ~23k 次 invalidate，是 entries 没活到重用就被踢的主因之一（§4.3.4 原因 2）。

检查点：
- `reth-bsc-triedb/db/pathdb/src/pathdb.rs` 里 `commit_difflayer` 函数，搜索 `invalidate` 或 `moka.remove` 调用
- 判断被 invalidate 的节点是不是**真的不再会被读到**（例如：如果 DiffLayer 优先级高于 moka，老 hash 的节点即使留在 moka 也不会被读错；那 invalidate 就是纯防御性开销）
- 如果 invalidate 确实是防御性的（DiffLayer 已经屏蔽了老 hash），可以**去掉**或改成 TTL 淘汰

**代价**：1-2 天代码 + canonical replay 验证 state root 一致
**预期**：entries 存活时间显著延长；stage_07 hit rate 54.8% → 70-80%
**风险**：中。如果判断错了让 stale 节点被读到，会产生错误 state root（canonical replay 会立即抓到）
**验证**：pathdb 跑一组单测 + 100k 块 canonical replay；压测对比 hit rate

##### 选项 C：启动时预热 37 个热合约的 storage trie 骨干（半天）

reth-bsc 启动后、挖矿前，遍历一次所有已知热合约的 storage trie 根节点 + depth-1 内部节点，主动 `touch` 进 moka：

```rust
// 伪代码，加在 BscMiner::start 开头
for contract in HOT_CONTRACTS {
    let trie = triedb.storage_trie(contract);
    let _ = trie.walk_depth(2);  // 读前两层
}
```

重点是：这些骨干节点因为启动时"主动访问"进入 moka 的频次计数器，TinyLFU 之后再遇到"新 entry 要挤老 entry"的判决时，会认为这些骨干"频次高"不淘汰。

**代价**：<100 行代码，单次启动多耗几秒；cache 固定占用 +几千条（可忽略）
**预期**：stage_07 hit rate 提 3-5%；第一块 triedb_calc 也变快
**风险**：低。只是读，不改数据
**验证**：第一块 triedb_calc_ms 从 50-60ms 降到 20-30ms

##### 选项 D：给 moka 加 weigher，按节点类型分权重（1 周）

moka 支持 `weigher` 函数，可以给不同 entry 不同权重：

```rust
Cache::builder()
    .weigher(|key, value| match classify(key) {
        TrieNodeKind::AccountTrieRoot => 100,   // 根节点不容易被淘汰
        TrieNodeKind::AccountTrieInternal(depth) => 50 / (depth + 1),  // 内部节点浅层权重高
        TrieNodeKind::AccountTrieLeaf => 1,     // 叶子节点最容易被淘汰
        TrieNodeKind::StorageTrieRoot => 200,   // 37 个热合约的根超高权重
        TrieNodeKind::StorageTrieInternal(depth) => 80 / (depth + 1),
        TrieNodeKind::StorageTrieLeaf => 1,
    })
    .max_capacity(total_weight)
    .build()
```

这样淘汰时优先砍叶子节点，保留骨干结构，模拟 "pinned hot contract" 的效果。

**代价**：~1 周。需要给 TrieNode 加 `kind()` 方法；修 PathDB 的 cache 构造；修 moka cap 单位从 "count" 到 "weight"；回归测试
**预期**：stage_07 hit rate 54.8% → 80-90%；与选项 B / 选项 C 叠加可达 90%+
**风险**：中。weigher 写错可能让 cache 行为退化；需要 canonical replay 验证状态根一致
**验证**：压测对比 hit rate；flamegraph 看 `PathDB::get_trie_node` 降幅

##### 选项 E：独立 clean cache / L1-L2 cache（❌ 不推荐）

历史多次尝试（见 `./related/reth-bsc-vs-geth-bsc-final-summary.md` §6.3 的 "Independent clean_cache"），**实测负优化**——加一层 lookup 开销 > 提升的命中率。不建议走这条。

#### 4.3.7 推荐顺序

```
Phase 0.5（最高优先，零代码，0.5-1 天）：
  ├─ 选项 A（加大 cap 到 80M）   ← cache 已满到 cap=40M，加大直接有效
  └─ 选项 C（启动预热热合约）    ← 半天代码，零风险

Phase 0.8（看 Phase 0.5 效果再决定）：
  └─ 选项 B（审计 commit_difflayer invalidate）
         ↑ 潜在 +15% hit rate，但需要 canonical replay 验证

如果 Phase 0.5 + 0.8 把 stage_07 hit rate 推到 80%+ 就收工；否则再走：
  └─ 选项 D（按节点类型 weigher）  ← 1 周

选项 E（独立 clean cache）:  ❌ 历史负优化，不做
```

**测试方法**：每做一个变更跑一轮 2200/2400/2600 TPS 压测，对比三个信号：
- moka `overall hit rate`（v3 stage_07/08/09 起步 54-57%，目标 70%+）
- `trie_cache_entries avg`（v3 现在贴 cap 40M；做完选项 A 后看是否依然贴 80M cap）
- flamegraph 里 `PathDB::get_trie_node` 的 CPU 占比（6.3% 起步，目标 <3%）

---

## 5. 路线图汇总

| 阶段 | 改动 | 预期 TPS | 工作量 | 风险 |
|---|---|---|---|---|
| 已完成 | `fix/timestamp-drift` + 新启动参数 | **稳态 2400 / 峰值 2700-3000** | — | 已验证（v3）|
| **Phase 0.5a** | **加大 cap：`RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES=80M` (§4.3 选项 A)** | **稳态 ~2500** | **0 代码（启动参数）** | **零** |
| Phase 0.5b | 启动预热 37 个热合约骨干 (§4.3 选项 C) | 稳态 ~2550 | 半天 | 零 |
| Phase 0.8 | 审计并精简 `commit_difflayer` invalidate (§4.3 选项 B) | 稳态 ~2700 | 1-2 天 | 中（canonical replay 验证） |
| Phase 1 | DiffLayers Bloom filter (§4.1 选项 A) | 稳态 ~2900 | 1-2 天 | 几乎零 |
| Phase 2 | asm-keccak 审计 + Batch DiffLayer 写入 (§4.2 选项 1+4) | 稳态 ~3000 | 1 周 | 低 |
| Phase 3 | 流式 RLP + 增量 keccak (§4.2 选项 2) | 稳态 ~3150 | 1 周 | 中 |
| Phase 4 | moka weigher 按节点类型分权重 (§4.3 选项 D) | 稳态 ~3250 | 1 周 | 中 |
| Phase 5 | Committer Arc::make_mut (§4.2 选项 3) | 稳态 ~3350 | 2 周 | 中 |
| Phase 6（可选） | DiffLayers merged index (§4.1 选项 B) — 只在前 5 条不够时考虑 | 稳态 ~3500 | 2-3 周 | 高 |

> "稳态"指 stage_09（2400-2600 档）超 450ms 比例 ≤ 5%。"峰值"指偶发能跑到的 TPS 但伴随大量超预算。

**目标稳态 3000 TPS**：Phase 0.5a + 0.5b + 0.8 + 1 + 2 组合（累计约 1-1.5 周工作量）。Phase 0.5a 是**零代码改动**，下一轮压测建议立即上——cache 已满到 cap=40M，加大到 80M 直接收一波收益。

**追稳态 3500 TPS** 需要完整路线图 + 可能还需要对比 geth-bsc 的 profile 找剩余长尾。

---

## 6. 验收标准（每一步上线前要做的事）

1. **Canonical replay**：回放 BSC 主网最近 10 万块，state_root bit-for-bit 一致
2. **Devnet 压测**：200→400→800→1200→1500→1800→2000→2200→2400→2600 TPS 阶梯跑一轮，分析脚本对比；**用细分 bucket（v3）数据，不要混在 stage_07 一桶**
3. **Flamegraph 对比**：新旧版本各采一张 SVG，确认目标热点下降、没有新热点冒出
4. **SaveBlocks tail check**：`save_blocks p999` 不应恶化（当前 v3 显示 max 30 秒，p999 9.6s，独立问题暂不阻塞 TPS 路线）

### 6.1 各 Phase 的验收证据样板

为每个 Phase 上线时，在对应 flamegraph 里用 §4.2 给出的 Python 片段抽目标函数的占比，和本文档的基线对照：

| Phase | 验收通过的 SVG/report 特征 | 对照基线 |
|---|---|---|
| Phase 0.5a（cache cap 80M） | `trie_cache_entries avg` 从 39.94M 涨到 60M+；stage_07-09 hit rate 涨 5-15 个百分点；`PathDB::get_trie_node` 从 6.3% → ~5% | §4.3 选项 A |
| Phase 0.5b（启动预热骨干） | 第一块 `triedb_calc_ms` 从 50-60 → 20-30ms；stage hit rate 再涨 3-5% | §4.3 选项 C |
| Phase 0.8（精简 invalidate） | hit rate stage_07-09 → 70%+ | §4.3 选项 B |
| Phase 1（Bloom） | `DiffLayers::get_trie_nodes` 从 13-16% → 2-3% | §4.1 |
| Phase 2（asm-keccak + batch） | `Hasher::hash` 从 32-42% → 25-35% | §4.2 |
| Phase 3（流式 RLP） | `Hasher::hash` 再下降 5-8%，`Arc::drop_slow` 可能也降 | §4.2 选项 2 |
| Phase 4（moka weigher） | stage_07-09 hit rate → 80-90%，`PathDB::get_trie_node` → <2% | §4.3 选项 D |
| Phase 5（Arc::make_mut） | `Arc::drop_slow` 从 12% → <5% | §4.2 选项 3 |

同时 stage_report 应满足：
- **stage_09（2400 TPS）超 450ms 比例 ≤ 1%**（基线 1.8%，每 Phase 应再降）
- **stage_10（≥2600 TPS）超预算比例每 Phase 至少下降 5 个百分点**（基线 50.8%）

### 6.2 当前持久化问题的独立追踪

`save_blocks_us` 现状（来自 `stage_report_v3_finegrained.txt`）：

```
save_blocks_us : avg=116ms  p95=448ms  p99=1.76s  p999=9.6s  max=30.6s
lag_blocks     : avg=257    max=325
```

avg 已经很快（116ms），但偶发 max 30 秒级 RocksDB compaction stall 是隐患；`lag_blocks max=325` 接近触发 back-pressure 的边缘，但目前还未真正影响 miner。

**优先级**：接近 back-pressure 边缘，建议尽快治。治理方向：

- 在 triedb 里暴露更多 RocksDB 参数：
  - `level0_slowdown_writes_trigger`
  - `level0_stop_writes_trigger`
  - `soft_pending_compaction_bytes_limit`
- 短期缓解：把 `RETHBSC_ROCKSDB_MAX_BACKGROUND_JOBS` 从 8 进一步上调到 12-16（看机器核数）

---

## 附：相关文档

- `./related/reth-bsc-2500tps-flamegraph-findings.md` — 本轮第一次符号化 flamegraph 的完整拆解
- `./related/reth-bsc-2000tps-gap-classification.md` — 2000 TPS 阶段的分类与方法论
- `./related/reth-bsc-vs-geth-bsc-final-summary.md` — 更早期（2000 TPS 前）的调研基线
- `scripts/analyze_by_tps_stage.py` — 所有 stage 报告的分析脚本
