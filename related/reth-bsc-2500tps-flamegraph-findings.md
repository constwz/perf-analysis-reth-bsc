# reth-bsc 2500 TPS 带符号 Flamegraph 分析结论

> 2026-04-22
> 分支：`reth-bsc@fix/timestamp-drift (c9ae872)` + `reth@feat/logs-on-develop (4240cf7e)` + `reth-bsc-triedb@feat/logs-on-develop (07c59aa)`
> 数据源：
> - `stage_report_2500tps.txt`（stage_06 34 块、stage_07 338 块）
> - `reth-bsc.stage07.svg`（stage_07 期间 60 秒 perf record，带 Rust 符号）
>
> 启动参数包含：`RETHBSC_ROCKSDB_WRITE_BUFFER_SIZE_MB=1024`、`MAX_BACKGROUND_JOBS=8`、`TRIE_NODE_CACHE_ENTRIES=40000000`
>
> **本文档的目的**：把这一轮第一次有"符号化 flamegraph"的实测结果沉淀下来，作为后续 (b) 阶段优化方案的基线。

---

## 1. TPS 能力的重新锚定

| Stage | tx/块 avg | 块数 | build avg | build p99 | build p999 | 超 450ms 比例 |
|---|---|---|---|---|---|---|
| stage_06（2000 TPS） | 885→？ | 34 | —* | — | — | — |
| **stage_07（>1000 tx/块，约 2500-3500 TPS 负载）** | **1567** | **338** | **499ms** | **1324ms** | **1538ms** | **51.8%** |

\* 本轮 stage_06 样本太少（34 块，且数据不重要），主要看 stage_07。

上一轮（`stage_report1_2000tps.txt`）的 stage_06：60 块，**0/60 超预算**。所以 **2000 TPS 是稳定的**，真正的天花板在 2500 TPS 左右。

---

## 2. 符号化 Flamegraph 的新发现

前几轮因为 `[profile.maxperf]` 继承了 `debug="none" strip="symbols"`，所有 reth-bsc/triedb/revm 函数名塌缩成单一的 `[reth-bsc]` 黑盒，flamegraph 没法用。本次修好 profile（commit `c9ae872`）后，14033 个符号化栈帧覆盖了完整调用链。

### 2.1 stage_07 的 CPU 分布（实测）

| 热点 | CPU 占比 | 直接上游 | 能否快速缓解 |
|---|---|---|---|
| **tx pool 排序**（`PendingPool::remove_to_limit` → `driftsort_main` + `quicksort`） | **21.7%** | `TxPool::discard_worst` | ✅ 调大 pool 容量 / 改成 BinaryHeap |
| `Arc::drop_slow` | 12.4% | 分散于 trie 节点释放 | ⚠️ Rust 语言选型代价，`Arc::make_mut` 在部分路径有帮助 |
| **`DiffLayers::get_trie_nodes` 线性扫 256 层** | **12.1%** | `Trie::resolve_and_track` | ⚠️ 需要重做 `1108780` O(1) 合并索引，有 bad block 陷阱 |
| `TransactionsManager`（网络接收 tx） | 11.6% | 外部 p2p 流入 | 与 pool 压力相关，B-2 治理后自然缓解 |
| `revm_handler::ExecuteEvm::transact` | 10.4% | build_payload 的 tx loop | ✅ 正常开销 |
| **`save_blocks` + `PersistenceService::run`** | **10.0%** | persistence 线程 | ⚠️ RocksDB compaction stall 主导，需要暴露更多 tuning |
| **`prewarm::PrewarmContext::transact_batch`** | **9.0%** | tokio-rt-worker | ✅ **冗余**，应关闭 |
| `TrieDBPrefetchAccountTask::run` | 3.1% | 正常 prefetcher | ✅ 合理 |
| `revm_interpreter::instructions::host::sload` | 4.7% | EVM 内部读 slot | 正常 |

以上覆盖大约 100% CPU（部分重复在嵌套栈帧上）。

### 2.2 Stage_07 state root 详细拆解

```
state root 总计                  avg 135ms   p99 827ms   p999 1225ms
├── triedb_calc (= intermediate_and_commit)  avg 124ms  p99 818ms  p999 1219ms
│   ├── intermediate_inner      avg 112ms   p999 1091ms
│   │   ├── update_state_objects  avg 60.5ms  p999 770ms
│   │   │   （rayon par_iter，但 tail 严重）
│   │   ├── update_account_trie   avg 28.8ms  p999 406ms  ← SERIAL
│   │   └── account_hash          avg  9.5ms  p999  73ms
│   └── commit_inner            avg 54.6ms  p999 288ms
└── prefetcher_finish           avg  8.6ms  p999  93ms
```

**关键认知**：上一轮报告看到 `update_state_objects` p999 跑到 751ms，怀疑是单账户 fat trie 或 rayon 池争用。本轮 flamegraph 证明**其实是 `resolve_and_track` → `DiffLayers::get_trie_nodes` 的 256 层扫慢**（12.1% CPU 都在这里）。par_iter 本身没问题。

### 2.3 per-tx 执行路径

Stage_07 per-tx sub-step：

```
evm_transact      avg 184us  p999 277us   ← revm 很健康
pre_exec              0us
state_clone           0us
prefetcher_hook       2us    p999 7us
receipt_build         0us
commit                2us    p999 9us
```

**revm 完全不是瓶颈**。1500 tx/块 × 200us = 300ms，恰好是实测的 `total_evm_transact_ms avg 281ms`。

---

## 3. 已发现但之前看不到的问题清单

### F-1：tx pool `remove_to_limit` 全量排序（21.7% CPU，最严重）

现象：2500 TPS 压力下 tx 入池速率 > 出块消化速率，pending pool 反复溢出，`TxPool::discard_worst` 每次都触发 `PendingPool::remove_to_limit` → `driftsort_main` 对整个 pending 列表排序。

在 2000 TPS 下看不到这条问题，因为 pool 没持续溢出。**这是 2500+ TPS 才暴露的瓶颈**。

### F-2：reth-engine-tree prewarm 重复执行（9.0% CPU）

`PrewarmContext::transact_batch` 在 tokio-rt-worker 上与 miner 的 build_payload 并发跑一份**重复的 EVM 执行**来预热 cached state。对 BSC miner 路径是冗余的——我们已经有：
- `TrieDBPrefetchAccountTask` 每账户异步 prefetcher（3.1% CPU，这是对的）
- prewarm 又在重新执行一遍 tx 来warm state provider cache

净效应：大约 2x 的 EVM CPU 浪费，对 state root 计算无帮助（state root 路径走 triedb 不走 state provider cache）。

### F-3：DiffLayers 256 层线性扫（12.1% CPU，已知但量化更准了）

之前只模糊估计 5-7%，本轮实测 12.1%。`Trie::resolve_and_track` 每次 miss DiffLayer 都要把 256 个 HashMap 依次查一遍。上次 `1108780` 合并索引尝试产 bad block 被回退。

### F-4：Arc::drop_slow（12.4% CPU）

Rust 多线程共享 trie 节点必然要 clone Arc，drop 时原子递减→释放。在 trie 的深递归路径上这部分开销展不开，分散在整个栈。`Arc::make_mut` 在 refcount=1 的单持有路径能消掉大半，但我们之前的场景几乎都 refcount≥2（prefetcher + main + DiffLayer 同时持有）。

### F-5：persistence tail 极端（save_blocks p999 12.9 秒）

```
save_blocks_us   avg=678ms   p50=432ms   p95=2.3s   p99=6.2s   p999=12.9s
save_blocks batch size   avg=1.8   p95=6   max=26
```

即使 `MAX_BACKGROUND_JOBS=8` + `WRITE_BUFFER_SIZE_MB=1024`，尾巴还是跑到 13 秒。说明 RocksDB compaction 的 L0→L1 stall 仍然在触发。需要进一步暴露：
- `level0_slowdown_writes_trigger`
- `level0_stop_writes_trigger`
- `soft_pending_compaction_bytes_limit`

但 miner 有 channel 解耦，`lag_blocks avg=257`（= `persistence_threshold`），没有发散，**所以不影响当前 TPS 上限**，是独立问题。

---

## 4. 可以做 (b) 的优化方案（按投入/产出比排序）

### B-1：关闭 PrewarmContext（半天工作量 · +9% CPU 立即）

`reth`'s `TreeConfig` 有 `without_prewarming(bool)` 方法。需要确认：
- 是否有对应 CLI 参数（`--engine.prewarming-disabled`？）
- 如果没有，需要在 reth-bsc 的 EngineArgs 包装层显式设置

**预期**：stage_07 下至少回来 ~9% CPU，直接给 exec loop / state root，预计 build p99 从 1324ms 降到 ~1100ms，超预算块比例从 51.8% 降到 ~35%。

### B-2.1：调大 tx pool 容量（启动参数即可）

`reth-transaction-pool` 的 `PoolConfig`：
- `pending_pool_capacity`（默认多少？）
- `total_transaction_cost_limit`
- `max_account_slots`

**预期**：若调大 2-4x，`remove_to_limit` 的触发频率下降 3-5x，直接释放大头 CPU。

### B-2.2：用 partial sort 或 BinaryHeap 替代全排序（reth-transaction-pool 侧改动）

`remove_to_limit` 当前实现：拿所有 pending，按 score 排序，砍掉底部 N 个。改法：
- 维护一个按分数排序的 `BinaryHeap<Reverse<_>>`（最小堆）
- 插入时维护，超限时 pop 最小
- O(log n) 插入 vs 现在 O(n log n) 每次 discard

中等工作量（3-5 天 reth 侧改动 + tests）。

### B-2.3：节流 discard 频率

现在每次 add 都检查超限并 discard。改成攒 N 次 add 或 X 秒再 discard 一次。最容易实现，但只是把尖峰平均掉，不减总工作量。

### B-3：DiffLayers O(1) 合并索引（长期）

重做 `1108780` 的合并索引，先排查 bad block 根因。2-3 周工作量。等 B-1/B-2 做完再评估是否还需要。

### B-4（独立）：RocksDB compaction stall 调优

独立于 TPS 天花板，但持久化 p999 12.9s 是个隐患。需要在 triedb 的 `PathProviderConfig::apply_env_overrides` 里追加：
- `RETHBSC_ROCKSDB_LEVEL0_SLOWDOWN_WRITES_TRIGGER`
- `RETHBSC_ROCKSDB_LEVEL0_STOP_WRITES_TRIGGER`
- `RETHBSC_ROCKSDB_SOFT_PENDING_COMPACTION_BYTES_LIMIT`

---

## 5. 判定表更新

结合 §4 优化可达的 TPS 天花板估算：

| 场景 | build p99 估计 | 超预算比例估计 | 可实现 TPS |
|---|---|---|---|
| 当前（fix/timestamp-drift） | 400ms (stage_06) / 1324ms (stage_07) | 0% (2000 TPS) / 52% (2500+) | **2000 TPS 稳定** |
| + B-1（prewarm 关闭） | 降 ~200ms | 35%（stage_07） | ~2300 TPS |
| + B-2.1（pool 容量加大） | 降 ~150ms | ~20% | ~2600 TPS |
| + B-2.2（pool 改 heap） | 再降 ~50-100ms | ~10% | ~2800 TPS |
| + B-3（DiffLayers O(1)） | 降 ~100-150ms | <5% | ~3000 TPS |
| + 细节调优（B-4、jemalloc 等） | — | — | ~3000-3200 TPS |

**目标 3000 TPS 不需要架构级改动**，只需要 B-1 + B-2.1 + B-2.2 + B-3 四条组合。

---

## 6. 下一步

1. 查 reth 的 `TreeConfig` / `EngineArgs` 看 prewarming 怎么关，txpool pending 容量怎么调
2. 在 2500 TPS 下跑一轮 `--engine.prewarming-disabled`（如果有 flag），对比 build p99
3. 根据实测结果决定是否需要 B-2.2 / B-3

本文档作为基线，后续每轮新的 flamegraph + stage_report 数据对照本文档即可判断优化生效。

---

## 附：数据/SVG 对应文件

- `../stage_reports/stage_report_2500tps.txt` — 本轮 stage 报告
- `../flamegraphs/reth-bsc.stage07.svg` — 带 Rust 符号的 flamegraph（stage_07 采样）
- `../stage_reports/stage_report1_2000tps.txt` — 上一轮参考（stage_06 0/60 超预算）

## 附：被本次推翻/佐证的历史结论

| 旧结论 | 新结论（本次） |
|---|---|
| update_state_objects p999 = 751ms 是 rayon 池争用或单账户 fat trie | **是 DiffLayers 256 层扫慢**，占 12% CPU |
| DiffLayers 线性扫 ~5-7% CPU | **实测 12.1%**，比之前估的大 |
| MDBX 双写是隐患 | 本轮 persistence lag 稳在 threshold，背压无；问题是 RocksDB compaction stall，不是 MDBX |
| per-tx exec 慢可能是瓶颈 | revm 完全不是瓶颈（184us avg） |
| PrewarmContext 存在但不重要 | **9% CPU 纯浪费**，对 BSC 路径冗余 |
| tx pool 开销忽略不计 | **21.7% CPU 是最大热点**，2500 TPS 才暴露 |
