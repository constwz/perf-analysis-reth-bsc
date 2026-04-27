# reth-bsc 性能调优总结 + 未来方向

> 起点：`reth-bsc@develop` 稳态 ~2000 TPS
> 终点：`reth-bsc@fix/timestamp-drift` + 新启动参数
> - **稳态 ~2400 TPS**（≤0.4% 块超 450ms 预算）
> - **峰值可达 ~2700-3000 TPS**（块大时约半数超预算，链节奏被拖慢）
>
> 架构未动，走的是"代码小改 + 参数调优 + 探针实证"三步路径。
>
> 本文档目的：列清楚每一项改动做了什么、带来了什么，以及接下来最值得攻的两个热点（`DiffLayers::get_trie_nodes` 和 `Committer::commit_internal`）应该怎么做。
>
> **2026-04-27 修订**：基于细分 TPS bucket（1800/2000/2200/2400/≥2600）的新一轮压测数据（[`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt)），修订了三条旧结论：
> - "稳态 2700 TPS" → 实际稳态是 **2400 TPS**（详见 §3.0）
> - "cache 容量不是瓶颈" → 长跑后 cache 填满到 39.94M / cap 40M，**容量重新成为瓶颈**（详见 §4.3.4）
> - "evm_transact +20% 是 prewarm 副作用" → 实测稳定 +10% 左右，原估计偏高（详见 §3.2）

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
| `stage_report_2700tps.txt` | 本轮 2700 TPS 稳态数据（B-1 + B-2.1 全部生效） | [查看](./stage_reports/stage_report_2700tps.txt) |
| **`stage_report_v3_finegrained.txt`** | **细分 bucket（1800/2000/2200/2400/≥2600）的长跑数据，定位 TPS 拐点 + 揭示 cache 满载** | [查看](./stage_reports/stage_report_v3_finegrained.txt) |

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

前一轮 1200 TPS v4 flamegraph（仅存历史数据）：`DiffLayer::get_trie_nodes` 占 **6.52% CPU**，其中线性扫 HashMap 本身 4-5%，prefix.clone + Vec allocation 2-3%。应用 `aec0dc3` 后再采样（stage_07 SVG），同函数占比降到 **12.1%**——看似上升其实是**因为 update path 整体压缩了，这项的占比被抬高**。实际 CPU 时间绝对值下降见 `reth-bsc.1200tps-v4.svg` vs `reth-bsc.stage07.svg`。

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

`stage_report_2700tps.txt` 的 moka 段：

```
[moka cache (below DiffLayer)]
  overall hit rate : 54.8%   ← 之前默认 20M 条时 15-25%
  trie_cache_entries : avg=24M  max=26M  ← cap 扩大到 40M 后没满
```

hit rate 从 15-25% 跳到 54.8%。cache 在 stage_07 下稳定在 **24-26M / cap 40M**（见 §4.3.4 详细数据），**远未占满**。

**重要纠正**：直觉"没满就继续加大 cap"在这里是错的——容量没满却 hit rate 只有 54.8%，说明 entries 不是因为**容量淘汰**被踢出，而是被 **TinyLFU 频次淘汰** 或 **`commit_difflayer` 主动 invalidate** 提前清走。再把 cap 从 40M 加到 80M 也**无效**（cache 根本触不到 40M 上限）。真正的杠杆见 §4.3.6。

---

## 3. 实测效果

### 3.0 TPS 健康分级（基于细分 bucket 的 v3 数据）

[`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt) 把 2000+ 区间细分到 1800/2000/2200/2400/≥2600 五档，**首次让我们能精确定位 TPS 拐点**：

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

之前文档里说的"稳态 2700 TPS"实际上是 stage_07 (>1000 tx) 一桶里所有 1500+ tx 块的笼统平均——粒度太粗，掩盖了真正的 2400/2600 拐点。**生产环境建议负载控制在 ≤ 2400 TPS**。

#### 3.0.1 vs `develop` 基线对比

| 指标 | `develop` 基线 | `fix/timestamp-drift` + 新参数 | 数据来源 |
|---|---|---|---|
| **稳态 TPS** | ~2000 | **~2400**（4/220 块超预算） | v3 stage_09 |
| **峰值能力** | 不稳定 | **~2700-3000**（块大时半数超预算） | v3 stage_10 |
| 2000 TPS 超预算率 | n/a（旧 bucket 没分到这一档） | **0.3%** ✅ | v3 stage_07 |
| 2400 TPS 超预算率 | n/a | **1.8%** ✅ | v3 stage_09 |
| 2600+ TPS 超预算率 | 50%+（旧 bucket 已观察） | **50.8%** ❌（基本同 develop） | v3 stage_10 |
| `build_duration_ms` p99（高压） | 820-1324ms | **740ms**（v3 stage_10）| v3 |
| `build_duration_ms` p999（高压） | 1538-1631ms | **844ms** | v3 |
| `update_state_objects_ms` p99（高压） | 751-770ms | **104ms** | v3 stage_10 |
| `update_account_trie_ms` p99（高压） | 348-406ms | **51ms** | v3 stage_10 |
| moka cache hit rate (stage_07 / 2000 TPS bucket) | 15-25% | **54.4%** | v3 |
| `PrewarmContext` CPU | 9.0% | 0% | flamegraph |
| tx pool sort CPU | 21.7% | 0% | flamegraph |

灾难 tail（1.5 秒块）已彻底消失，所有改进**仍然成立**。**真正的修订只是"稳态 TPS 数字"——不是 2700，是 2400**。

### 3.1 数据来源（逐项）

以下是每一个数字的原始出处，便于交叉验证。

**基线（`develop` 对应 `stage_report_2500tps.txt`）**：

```
stage_07_>2000tps   (338 blocks, caller=miner)
[workload]
  build_duration_ms   : avg=499.4ms  p95=675ms  p99=1324ms  p999=1538ms  max=1538ms
  trie_root_duration_ms : avg=199.3ms  p95=409ms  p99=964ms  p999=1234ms
[build deadline vs 450ms slot]
  blocks over 450ms : 175/338 (51.8%)
[intermediate_inner — update_account_trie is serial]
  update_state_objects_ms : avg=60.5ms  p99=280ms  p999=770ms
  update_account_trie_ms (SERIAL) : avg=28.8ms  p99=180ms  p999=406ms
[moka cache (below DiffLayer)]
  overall hit rate : 75.6% (而高压 stage_07 内部 bucket 只有 54.8%)
```

**现状（`stage_report_2700tps.txt`）**：

```
stage_07_>2000tps   (648 blocks, caller=miner)
[workload]
  build_duration_ms   : avg=523.4ms  p95=671ms  p99=724ms  p999=767ms  max=767ms
  trie_root_duration_ms : avg=157.1ms  p95=257ms  p99=302ms  p999=436ms
[build deadline vs 450ms slot]
  blocks over 450ms : 403/648 (62.2%)
  overrun_ms : avg=87.6ms  p99=274ms  p999=317ms
[intermediate_inner — update_account_trie is serial]
  update_state_objects_ms : avg=30.0ms  p99=92ms  p999=170ms
  update_account_trie_ms (SERIAL) : avg=9.6ms  p99=47ms  p999=73ms
[per-tx exec breakdown (microseconds)]
  evm_transact_us : avg=222.6us  p99=293us  p999=301us   ← revm 很稳定
[moka cache (below DiffLayer)]
  overall hit rate : 54.8% (stage_07 内部 bucket; 总体 75.6%)
```

### 3.2 "超预算比例反而上升"的真实解释

注意 `blocks over 450ms` 从 **51.8% 变成 62.2%**，看起来退化。但 p999 build 从 **1538ms 降到 767ms**——尾巴腰斩了。这里有两件互相拉扯的事情发生。

#### 先对齐数据

两份 report 的 stage_07（tx/块 avg 都在 ~1560）build_duration 分布：

| stage_07 | 2500 报告 | 2700 报告 |
|---|---|---|
| tx_count avg | 1567 | 1557 |
| tx_count p50 | 1635 | 1611 |
| build_duration avg | 499ms | 523ms |
| **build_duration p50** | **455ms** | **536ms**（涨了 80ms）|
| build_duration p99 | 1324ms | **724ms**（降 600ms）|
| build_duration p999 | 1538ms | **767ms**（降 770ms）|
| per-tx `evm_transact` avg | **183us** | **222us**（涨 20%）|

tx/块是**实际打包进块的数量**（`payload.rs:812` 的 `transactions.len()`），**两轮几乎一样**。

#### 真正发生了什么

两件事同时发生：

**1. 消除了灾难性 tail（好）**
  - 2500 报告：p99 = 1324ms，p999 = 1538ms。长尾是 PrewarmContext（9% CPU）+ PendingPool sort（21.7% CPU）在偶发高冲突时把某些块拖到 1-2 秒
  - 2700 报告：p99 = 724ms，p999 = 767ms。长尾完全消失

**2. median 反而涨了 80ms（小坏）**
  - per-tx `evm_transact` 从 183us → 222us（看似 +20%）
  - 原因：`--engine.disable-prewarming` 省掉的不只是"重复 EVM 计算"，还**副作用地**在 warm state provider cache。miner 自己的 EVM 执行原本能蹭到这份热 cache，现在蹭不到，per-tx 变慢
  - 1557 tx × (+40us/tx) = 62ms 的 per-块增量，和 median build 涨的 80ms 对得上

> **2026-04-27 修订**：v3 数据显示 evm_transact 跨多个 stage 稳定在 **197-215us**（跨 stage_06 到 stage_10 都差不多），不再是 222us。原本 +20% 估计可能含**workload 偶发噪声**。**真实稳态副作用约 +10%**（约 +20us / per-tx），不是 +20%。修订后的算账：1500 tx × 20us = 30ms / 块 增量，比之前估的 62ms 小一半。

#### 净效应

超预算比例 51.8% → 62.2% 上升，是因为 **median 从 455ms（刚好踩 450 线）平移到 536ms（稳定超 86ms）**。但这是好事，因为：

**链节奏视角**（sustained TPS 的真实决定因素）：

| | 2500 报告 | 2700 报告 |
|---|---|---|
| 是否有 1-2 秒灾难块 | 有（p999 = 1.5s）| 无（p999 = 0.77s）|
| 实际出块速率 | ~1.5 块/sec（偶发长块阻断链）| ~1.91 块/sec（稳定 500-700ms）|
| 实测 sustained TPS | ~2350 TPS | **~2700 TPS** ✓ |

**不是"每块塞更多 tx"让 TPS 上升，而是"链节奏稳定"让 TPS 上升**。偶发的 1.5s 块在上一轮里阻断整个链，一块卡壳三四个 slot；消除它之后每块稍慢但链不卡，总吞吐反而高。

#### 要不要把 median 也拉回去

per-tx `evm_transact` 的 +20% 来自失去 prewarm cache warming 的副作用。理论上有三条路：

1. **接受这个代价**（当前做法）：换来 9% CPU 可用 + 消除 tail，净赚
2. **只关 prewarm 的 EVM 执行，保留它的 state cache 预热**：需要改 reth-engine-tree 里 `PrewarmContext` 让它只做 state read 不做 EVM call
3. **在 BSC miner 侧补上等价的 state cache 预热**：在 build_payload 前对 pending 的 top-N tx 预读 sender/to 的账户状态，相当于自己做一份 prewarm 但不跑 EVM

路线图 Phase 0.5（启动预热骨干）部分能对冲这个副作用，但主要针对 moka cache 里的 trie node，不是 state provider cache。如果压测显示 +20% per-tx 真的把天花板压住了，可以再考虑路线 2 或 3。

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

### 4.3 Cache 命中率（moka hit rate 54.8% → 可达 90%+，中等收益）

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

**Hit rate 在 50-57% 区间稳定**，没有更早数据里 92.3% 那种异常高的 stage——这是因为 v3 数据是**长跑**（总 ~2 万块、跨多次 stage 切换），cache 早已被填满到稳态，不再有"刚启动还没填满时的虚高"。

#### 4.3.4 为什么 hit rate 卡在 ~55%（修订）

> ⚠️ **2026-04-27 重大修订**：原文档说"cache 远未占满，容量不是瓶颈"。v3 数据**翻案**——cache 已经满了。

查 [`stage_reports/stage_report_v3_finegrained.txt`](./stage_reports/stage_report_v3_finegrained.txt) 的 moka 段（stage_10）：

```
[moka admission]
  node_admit_pct       : avg=99.9%       p99=100%       ← admission 入口几乎全通过
  trie_cache_entries   : avg=39.94M / cap=40M           ← ⚠️ 已经填满！
  node_insert_attempted: avg=19161/块                    ← 每秒 ~38k 新 entry 涌入
```

`trie_cache_entries` 从 stage_06 到 stage_10 **全部稳定在 39.94M**——cap=40M 被打满，每个新 entry 进入都会触发一次旧 entry 淘汰。

**这是和之前 2700 报告（24M/40M）完全不同的情况**：

| 数据来源 | trie_cache_entries | 含义 |
|---|---|---|
| `stage_report_2700tps.txt`（短跑，~10 分钟）| avg=24M / cap=40M | cache 还在填充期，没到稳态 |
| `stage_report_v3_finegrained.txt`（长跑，~2 万块）| avg=39.94M / cap=40M | cache **完全填满**，进入稳态淘汰 |

**修订后的归因**：

1. ✅ **容量是瓶颈之一**：cache 已满到 cap，每新 insert 必淘汰一个老 entry。**加大 cap 直接有效**
2. **moka 的 TinyLFU 频次淘汰**（仍然成立）：决定淘汰**谁**的策略
3. **`commit_difflayer` 的主动 invalidate**（仍然成立）：每块 ~19k 次 invalidate
4. **workload 长尾**（仍然成立）：300k 地址池，单 entry 平均 ~600 块才重访

**推论**：

- ✅ **加大 `RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES` 从 40M 到 80M 现在是有效的**——直接把"被容量挤掉"那部分 entry 的命中率收回来
- 加上 §4.3.6 选项 B（精简 invalidate）和选项 C（启动预热），能进一步把 hit rate 推上去

**推论**：**容量不是瓶颈**。单纯把 cap 从 40M 加到 80M **几乎不会有效果**——cache 连 40M 都填不满，多出来的 40M 是空的。正确的杠杆在 §4.3.6 选项 B（减少 `commit_difflayer` invalidate）、选项 C（启动预热骨干）和选项 D（按节点类型 weigher）。

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

##### 选项 A：加大 `RETHBSC_ROCKSDB_TRIE_NODE_CACHE_ENTRIES`（**修订后：建议先做**）

> ⚠️ **2026-04-27 重新立案**：原文档把这条标"已证无效"，依据是 2700 报告里 `trie_cache_entries=24M/cap=40M` 远未占满。v3 长跑数据（§4.3.4）显示 cache 已经填满到 39.94M / cap 40M——**容量重新成为瓶颈**。

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

#### 4.3.7 推荐顺序（v3 数据修订后）

```
Phase 0.5（最高优先，零代码，0.5-1 天）：
  ├─ 选项 A（加大 cap 到 80M）   ← v3 显示 cache 已满，加大直接有效
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

## 5. 路线图汇总（v3 数据修订）

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

**目标稳态 3000 TPS**：Phase 0.5a + 0.5b + 0.8 + 1 + 2 组合（累计约 1-1.5 周工作量）。Phase 0.5a 是**零代码**，**下一轮压测建议立即上**——v3 数据已经证明 cache 满载（39.94M / cap 40M），加大 cap 直接收一波。

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
| Phase 0.5a（cache cap 80M） | `trie_cache_entries avg` 从 39.94M 涨到 60M+；stage_07-09 hit rate 涨 5-15 个百分点；`PathDB::get_trie_node` 从 6.3% → ~5% | §4.3 选项 A（v3 修订）|
| Phase 0.5b（启动预热骨干） | 第一块 `triedb_calc_ms` 从 50-60 → 20-30ms；stage hit rate 再涨 3-5% | §4.3 选项 C |
| Phase 0.8（精简 invalidate） | hit rate stage_07-09 → 70%+ | §4.3 选项 B |
| Phase 1（Bloom） | `DiffLayers::get_trie_nodes` 从 13-16% → 2-3% | §4.1 |
| Phase 2（asm-keccak + batch） | `Hasher::hash` 从 32-42% → 25-35% | §4.2 |
| Phase 3（流式 RLP） | `Hasher::hash` 再下降 5-8%，`Arc::drop_slow` 可能也降 | §4.2 选项 2 |
| Phase 4（moka weigher） | stage_07-09 hit rate → 80-90%，`PathDB::get_trie_node` → <2% | §4.3 选项 D |
| Phase 5（Arc::make_mut） | `Arc::drop_slow` 从 12% → <5% | §4.2 选项 3 |

同时 stage_report 应满足：
- **stage_09（2400 TPS）超 450ms 比例 ≤ 1%**（v3 起步 1.8%，每 Phase 应再降）
- **stage_10（≥2600 TPS）超预算比例每 Phase 至少下降 5 个百分点**（v3 起步 50.8%）

### 6.2 当前持久化问题的独立追踪

#### 短跑数据（`stage_report_2700tps.txt`）

```
save_blocks_us : avg=684ms  p95=2.7s   p99=15.9s   p999=20.0s
```

#### 长跑数据（`stage_report_v3_finegrained.txt`）

```
save_blocks_us : avg=116ms  p95=448ms  p99=1.76s   p999=9.6s   max=30.6s ⚠️
lag_blocks     : avg=257    max=325（之前 max=300，略有恶化）
```

**对比 + 趋势**：
- avg 大幅下降（684ms → 116ms）：大多数 save_blocks 现在很快
- 但极端 max 翻倍（20s → 30.6s）：偶发的 RocksDB compaction stall **更极端了**
- `lag_blocks max` 从 300 涨到 325：**接近触发 back-pressure**，但还没真正影响 miner

**结论**：
- **优先级提升**：从原来"独立问题，不阻塞 TPS"升级到"接近 back-pressure 边缘，建议尽快治"
- 治理方向（不变）：在 triedb 里暴露更多 RocksDB 参数：
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
