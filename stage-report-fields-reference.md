# Stage Report 字段说明手册

`scripts/analyze_by_tps_stage.py` 输出的 `stage_report*.txt` 里每一项的含义、数据来源、判断好坏的标准。配合 [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) 阅读。

> 数据样例见：[`stage_reports/`](./stage_reports/) 目录下的三份 stage report 实测文件。

---

## 报告结构总览

```
stage_report.txt
├── per-stage 段（每个 TPS 档位重复一次，stage_01_<=200tps ... stage_10_>=2600tps）
│   ├── §1  [workload]                              ← 这一档的负载特征
│   ├── §2  [build deadline vs 450ms slot]          ← TPS 上限的直接信号
│   ├── §3  [state root breakdown (ms)]             ← state root 7 个子步骤
│   ├── §4  [triedb intermediate_and_commit]        ← triedb_calc 内部三段
│   ├── §5  [DiffLayer filter]                      ← DiffLayer 256 层命中
│   ├── §6  [moka cache (below DiffLayer)]          ← moka 命中率
│   ├── §7  [per-phase misses]                      ← cache miss 按阶段细分
│   ├── §8  [intermediate_inner — update_account_trie is serial]
│   ├── §9  [commit_inner]                          ← state root 写入
│   ├── §10 [moka admission]                        ← cache 容量行为
│   ├── §11 [per-tx exec breakdown (microseconds)]  ← 单 tx EVM 子步骤
│   ├── §12 [per-tx exec totals per block (ms)]     ← 上面 ×tx_count
│   └── §13 [prefetch storage coverage]             ← prefetcher 覆盖度
│
└── 全局段（所有 stage 之外，跑一次）
    └── §14 persistence thread   ← 持久化线程，跨 stage 聚合
```

---

## 公共维度：每行末尾的统计列

每个数值字段一行，结尾都是 `avg / p50 / p95 / p99 / p999 / max` 六个统计量：

| 列 | 含义 | 实战价值 |
|---|---|---|
| `avg` | 算术平均 | 反映"典型负载下的开销"。容易被尾巴拉偏 |
| `p50`（中位数） | 一半的块在这个值以下 | 和 avg 偏离大说明分布严重不对称 |
| `p95` | 95% 分位 | 5% 块的最差表现 |
| `p99` | 99% 分位 | **直接关系到链稳定性**，TPS 上限的关键指标 |
| `p999` | 99.9% 分位 | 在大数据量（>1000 块）下才有意义；反映极端 tail |
| `max` | 单次最差 | 看是不是离群（vs `p999` 是否接近） |

**注意**：分布字段的 p50 和 avg 偏离很大时，说明分布有重尾——这种情况下 avg 的参考价值低，应该看 p50 和 p99。

---

## §1 [workload] — 负载特征

| 字段 | 含义 | 数据源 |
|---|---|---|
| `tx_count` | 这块**实际打包进区块**的 user tx 数（不含 system txs） | `payload_builder` target，`Block payload built successfully` 的 `tx_count` |
| `build_duration_ms` | 从 `build_payload` 入口到完成的总挂钟时间 | 同上，`build_duration_ms` |
| `prepare_duration_ms` | tx 循环开始之前的准备时间（state provider 初始化、blob 参数等） | 同上 |
| `trie_root_duration_ms` | `finish_with_difflayer` 全程时间，覆盖 state root + 区块组装 | 同上 |
| `avg_tx_duration_micros (misleading!)` | ⚠️ **误导字段** = `build_duration / tx_count`，**不是**真实每笔 tx 时间 | 同上 |

**怎么用**：
- `build_duration_ms p99 > 450ms` = 出块爆预算，链稳定性受影响
- `trie_root_duration_ms / build_duration_ms` 反映 state root 占总成本
- ⚠️ **`avg_tx_duration_micros` 不要拿来做单 tx 性能对比**，看 §11 的 `evm_transact_us` 才是真的

---

## §2 [build deadline vs 450ms slot] — TPS 上限直接信号

| 字段 | 含义 | 数据源 |
|---|---|---|
| `blocks over 450ms` | "X / Y (Z%)" — 超 450ms 预算的块数 / 总块数 / 比例 | `bsc::builder::deadline`，`build deadline snapshot` |
| `deadline_used_pct` | `build_duration / 450ms × 100`。100% 即正好踩线 | 同上 |
| `overrun_ms` | 超出 450ms 的毫秒数（≤450ms 时为 0） | 同上 |

**怎么用**：
- 这是判断**该档 TPS 是否已经撑不住**的最直接指标
- ✅ 健康标准：`blocks over 450ms ≤ 5%` 且 `overrun_ms p99 < 100ms`
- ⚠️ 50%+ 块超预算 = 这个 TPS 已经撑不住，链节奏被拖垮

**实测基线**（来自 [`stage_reports/stage_report_2700tps.txt`](./stage_reports/stage_report_2700tps.txt) stage_07）：

```
blocks over 450ms : 403/648 (62.2%)
overrun_ms        : avg=87.6ms  p99=274ms  p999=317ms
```

---

## §3 [state root breakdown (ms)] — state root 7 子步骤

`finish_with_difflayer` 内部状态根计算的拆解。所有字段都是 microsecond 探针、显示成 ms（带小数）。

| 字段 | 含义 |
|---|---|
| `state_root_total` | 从 `state.hashed_post_state(...)` 开始到 `triedb_calc` 结束的总时间 |
| `executor_finish` | `executor.finish()` 收尾（gas 累计、最后系统 tx 等）|
| `merge_transitions` | `db.merge_transitions(BundleRetention::Reverts)` 把 state diff 合到 BundleState |
| `hashed_post_state` | `state.hashed_post_state(&db.bundle_state)` 把账户/槽 keccak256 一遍 |
| `prefetcher_finish` | 等待 TrieDB prefetcher 完成 + 收 `prefetch_state` |
| `to_triedb_state` | `hashed_state.to_triedb_hashed_post_state()` 数据格式转换 |
| `triedb_calc` | **核心耗时**：`triedb.intermediate_and_commit_hashed_post_state(...)` |

**数据源**：`bsc::builder::timing`，`state root breakdown`（reth-bsc 探针）

**怎么用**：
- 高 TPS 下 `triedb_calc` 通常占 90%+，其它都是常量级 (<5ms)
- 如果 `prefetcher_finish` p99 突然飙高（>50ms），说明 prefetcher 没跟上 tx 执行节奏

---

## §4 [triedb intermediate_and_commit] — triedb_calc 三段

`triedb_calc` 的内部拆解，由 triedb 侧探针记录。

| 字段 | 含义 |
|---|---|
| `total_ms` | 整个 `intermediate_and_commit` 函数挂钟时间，应≈ §3 的 `triedb_calc` |
| `state_at_ms` | 拿到当前 state 视图的时间（通常 <1ms）|
| `intermediate_inner_ms` | 状态根计算的核心：构建 trie + 哈希。**最大头** |
| `commit_ms` | 状态根算完后写入 DiffLayer 的时间 |

**数据源**：`triedb::timing`，`intermediate_and_commit breakdown`（triedb 侧探针）

**怎么用**：
- `total_ms = state_at + intermediate_inner + commit`
- `intermediate_inner` 占 70-85% 是正常的
- `commit_ms p99 > 100ms` 通常意味着 DiffLayer 写入瓶颈（HashMap insert 慢）

---

## §5 [DiffLayer filter] — DiffLayer 256 层命中

DiffLayer 是 reth-bsc 的内存层，缓存最近 256 块的 dirty trie 节点。

| 字段 | 含义 |
|---|---|
| `resolve_total` | 这块 trie 节点总查询次数（每次 `resolve_and_track` 触发）|
| `resolve_difflayer_hit` | 在 DiffLayer 链里命中的次数（不下钻 PathDB）|
| `resolve_fallthrough` | 256 层全扫完没找到，落到 PathDB（moka + RocksDB）的次数 |
| `difflayer_filter_pct` | `resolve_difflayer_hit / resolve_total × 100` |
| `difflayer_chain_depth` | 当前 DiffLayer 链长度（应 ≈ `persistence_threshold`）|
| `difflayer_total_nodes` | 整个链里所有 dirty 节点总数 |

**数据源**：`triedb::timing`，`intermediate_and_commit breakdown`

**怎么用**：
- `difflayer_filter_pct ~30%` 是 BSC 压测 workload 的正常值（地址池决定）
- `difflayer_chain_depth` 应该稳定在 `persistence_threshold`（例如 256）；如果飘到 300+ 说明 persistence 跟不上
- `resolve_fallthrough × 平均 RocksDB 读延迟` ≈ 这块 RocksDB 读总耗时

---

## §6 [moka cache (below DiffLayer)] — moka 命中

DiffLayer 没命中后，moka cache 的命中情况。

| 字段 | 含义 |
|---|---|
| `overall hit rate` | `sum(cache_hits) / sum(cache_hits + cache_misses)`，**只统计 fallthrough 部分** |
| `cache_hits per block` | 每块在 moka 命中的次数 |
| `cache_misses per block` | 每块 moka 也 miss、最终打 RocksDB 的次数 |
| `acct_misses` | `cache_misses` 里属于**账户 trie**的部分 |
| `stor_misses` | `cache_misses` 里属于**存储 trie**的部分 |

**数据源**：`triedb::timing`，`intermediate_and_commit breakdown`

**⚠️ 重要陷阱**：

`overall hit rate` 是 **moka 自己的命中率**（分母 = fallthrough 数），**不是全局命中率**。

```
全局命中率 = DiffLayer 命中率 + (1 - DiffLayer 命中率) × moka 命中率
         ≈ 31% + 69% × 55%
         = 31% + 38%
         = 69%

不是 31% + 55% = 86%！
```

详细推导见 [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §4.3.2。

**怎么用**：
- BSC 压测 stage_07 健康基线：`acct_misses + stor_misses ≈ 1100/块`，其中 `stor_misses` 占大头
- `stor_misses` 偏高意味着每笔 swap 触发多个未缓存的 SLOAD

---

## §7 [per-phase misses] — cache miss 按阶段细分

把每块的 cache_misses 按"发生在哪个阶段"细分。

| 字段 | 含义 |
|---|---|
| `state_at_misses` | 拿初始 state 视图阶段的 miss（通常 <100）|
| `intermediate_misses` | 构建 trie / 哈希阶段的 miss（**最大头**）|
| `commit_misses` | 写 DiffLayer 阶段的 miss |

**怎么用**：
- 通常 `intermediate_misses` 占 cache_misses 的 90%+
- 如果 `commit_misses` 偶发高，可能 DiffLayer 写入路径有非预期的 RocksDB 读

---

## §8 [intermediate_inner — update_account_trie is serial]

`intermediate_inner_ms` 的内部拆解。**只有 `update_account_trie` 是串行的**。

| 字段 | 含义 |
|---|---|
| `update_state_objects_ms` | rayon 并行更新所有账户的 storage trie + hash |
| `update_account_trie_ms (SERIAL)` | **串行**更新账户 trie（每账户一次 insert/delete）|
| `account_hash_ms` | 账户 trie root 的 16-child 并行哈希 |
| `account_count` | 这块修改的账户总数 |

**数据源**：`triedb::timing`，`intermediate_inner breakdown`

**怎么用**：
- `update_account_trie_ms` 是**唯一的串行阶段**，它的 p99 直接限制 TPS 上限
- p99 > 200ms 在 1500+ tx 块下是尾巴问题
- `account_count × 每账户摊销时间 ≈ update_account_trie_ms`

---

## §9 [commit_inner]

`commit_ms` 的细节。

| 字段 | 含义 |
|---|---|
| `commit_state_objects_ms` | 并行 commit 所有 storage tries + 单 commit 账户 trie |
| `storage_tries_count` | 这块涉及的 storage trie 数量（≈ unique 合约数）|

**数据源**：`triedb::timing`，`commit_inner breakdown`

---

## §10 [moka admission] — cache 容量行为

| 字段 | 含义 | 数据源 |
|---|---|---|
| `node_admit_pct` | 尝试 insert 的 entry 中真正进入 cache 的比例。100% = TinyLFU 入口不拒收 | `pathdb::admission`，`commit_difflayer moka admission` |
| `trie_cache_entries` | cache 当前 entry 数。和 `cap` 比较看是否容量瓶颈 | 同上 |
| `node_insert_attempted` | 这块尝试插入 cache 的 entry 数 | 同上 |

**怎么用**：
- `node_admit_pct < 95%` 说明 TinyLFU 在拒收新写入（少见）
- `trie_cache_entries / cap < 60%` 长期不变 = 容量绝对不是瓶颈，加大 cap **无效**（详见 [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §4.3.4）
- `node_insert_attempted ~23k/块` 说明每秒写入 ~50k entry，是 commit 阶段的写入压力

---

## §11 [per-tx exec breakdown (microseconds)] — EVM 性能真相

**单笔 tx** 在 EVM 执行路径上的子步骤平均耗时。**这是判断 EVM 性能的真正字段**（不是 §1 的 `avg_tx_duration_micros`！）

| 字段 | 含义 |
|---|---|
| `pre_exec_us` | gas 校验 + spec 查找 + precompile context 准备 |
| `evm_transact_us` | **revm 真正执行 tx**（包含所有 SLOAD/SSTORE/CALL）|
| `state_clone_us` | 给 prefetcher hook 做 `state.clone()` |
| `prefetcher_hook_us` | `system_caller.on_state(...)` 触发 prefetcher |
| `receipt_build_us` | 构建交易 receipt |
| `commit_us` | `evm.db_mut().commit(state)` 把 state 合到 BundleState |

**数据源**：`bsc::builder::timing`，`per-tx exec breakdown`（reth-bsc 探针）

**健康基线**（BSC qanet 压测）：
- `evm_transact_us`: 150-250 μs（revm 本身），与 tx 复杂度相关
- 其它项：每项 <5 μs

**异常排查**：
- `evm_transact_us > 400 μs`：tx 内容变了？更多 SLOAD？
- 关闭 prewarming 后 `evm_transact_us` 会从 ~180 涨到 ~220 μs（失去 prefetcher 顺带 warm 的 state cache，参见 [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §3.2）

---

## §12 [per-tx exec totals per block (ms)]

把 §11 的 per-tx 微秒值乘以 tx_count 得到每块累计毫秒数。

| 字段 | 含义 |
|---|---|
| `total_evm_transact_ms` | 这块 EVM 执行总耗时（所有 tx）|
| `total_prefetcher_hook_ms` | prefetcher hook 总耗时 |
| `total_commit_ms` | BundleState commit 总耗时 |
| `exec_duration_ms (loop total)` | **整个 tx 循环**的挂钟时间，含调度 + iterator 取 tx 等 |

**怎么用**：
- `exec_duration_ms` 是 `build_duration_ms` 的最大组成部分（除 trie_root 外）
- `exec_duration - total_evm_transact` 反映"循环开销"（pool iterator、check 等）。健康下 <30 ms
- 健康下：`build_duration ≈ exec_duration + trie_root_duration + ~10ms (prepare/finalize)`

---

## §13 [prefetch storage coverage]

| 字段 | 含义 |
|---|---|
| `coverage_pct` | `prefetched_storage_tries / needed_storage_accounts × 100` |
| `needed_storage_accounts` | 这块状态根计算需要哪些账户的 storage trie |
| `prefetched_storage_tries` | prefetcher 实际预热好的 storage trie 数 |

**数据源**：`bsc::builder::timing`，`prefetch storage coverage`

**怎么用**：
- ✅ `coverage_pct = 100%` 是健康
- `coverage_pct < 90%` 说明 prefetcher 没跟上 tx 执行
- 偶尔 >100% 是因为 prefetcher 预热了"以为要的但实际没用"的合约——浪费但无害

---

## §14 persistence thread (P-1)

持久化线程指标。**全局聚合**，不属于任何 stage。

| 字段 | 含义 | 数据源 |
|---|---|---|
| `save_blocks_us (per event)` | 每次 `save_blocks` 调用的耗时（μs）| `engine::tree`，`Finished persisting, calling finish` 的 `elapsed` |
| `lag_blocks` | 持久化完成时刻"未持久化的块数"（= canonical_head - last_persisted）| 由 `last_built_block_number` 和 `last_persisted_block_number` 推算 |
| `save_blocks batch size` | 每次 save 的块数 | `engine::persistence`，`Saving range of blocks` 的 `block_count` |

**怎么用**：
- ✅ 健康：`lag_blocks ≈ persistence_threshold`（典型 256），稳定不发散
- `save_blocks_us p999` 飙到几秒不是直接问题（miner 异步），但说明 RocksDB compaction stall
- ⚠️ `lag_blocks max` 持续 >300，说明 persistence 跟不上 miner，会触发 back-pressure 影响 TPS

**前置**：要看到这一节，启动 `RUST_LOG` 必须包含 `engine::tree=debug,engine::persistence=debug`。

---

## 快速诊断流程

按这个顺序看 stage 报告，能在 2 分钟内定位 TPS 上限的根因：

```
1. §2 blocks over 450ms 比例
   ├─ <5% → 这一档稳定，看更高 TPS 档位
   └─ >50% → 这一档已撑不住，进入 step 2

2. §1 build_duration p99 vs §11 exec_duration_ms / §3 trie_root_duration
   ├─ exec 占主导（>60%）→ EVM/tx loop 瓶颈，看 step 3
   └─ trie_root 占主导（>50%）→ state root 瓶颈，看 step 4

3. EVM/tx loop 瓶颈
   ├─ §11 evm_transact_us 异常高（>300μs）→ tx 内容变化或 cache 失效
   ├─ §11 其它 sub_us 异常 → 周边开销，关掉对应 hook
   └─ §12 exec_duration - total_evm_transact 大 → loop 调度开销

4. state root 瓶颈
   ├─ §8 update_state_objects p99 高 → DiffLayer 查找慢，看 §5
   ├─ §8 update_account_trie p99 高 → 串行步骤受限于 account_count
   ├─ §4 commit_ms p99 高 → DiffLayer 写入慢，看 §10
   └─ §3 prefetcher_finish p99 高 → prefetcher 没跟上

5. cache 表现（独立检查）
   ├─ §6 hit rate < 50% → 看 §10 容量、§4.3 文档讨论的优化方向
   ├─ §13 coverage_pct < 90% → prefetcher 改进
   └─ §5 difflayer_filter_pct 异常 → DiffLayer 配置或 workload 变化

6. 持久化健康（独立）
   ├─ §14 lag_blocks 发散 → persistence 跟不上，调 RocksDB
   └─ §14 save_blocks_us p999 几秒 → compaction stall（间接问题）
```

---

## 与文档其他部分的交叉引用

| 想深入理解 | 参考 |
|---|---|
| 哪些字段值得攻、怎么改 | [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §4 未来方向 |
| Cache hit rate 计算细节 | [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §4.3 |
| 实测 stage_07 数字基线 | [`stage_reports/stage_report_2700tps.txt`](./stage_reports/stage_report_2700tps.txt) |
| flamegraph 上每个热点对应到哪个字段 | [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §2.2.1-2.2.4 |
| 探针的代码位置 | [`reth-bsc-2700tps-summary.md`](./reth-bsc-2700tps-summary.md) §1.1 commit 列表 |

---

## 字段索引（按字段名排序）

为方便从某个具体字段反查含义：

| 字段名 | 所在 § |
|---|---|
| `account_count` | §8 |
| `account_hash_ms` | §8 |
| `acct_misses` | §6 |
| `avg_tx_duration_micros` | §1 ⚠️ |
| `blocks over 450ms` | §2 |
| `build_duration_ms` | §1 |
| `cache_hits per block` | §6 |
| `cache_misses per block` | §6 |
| `commit_misses` | §7 |
| `commit_ms` | §4 |
| `commit_state_objects_ms` | §9 |
| `commit_us` | §11 |
| `coverage_pct` | §13 |
| `deadline_used_pct` | §2 |
| `difflayer_chain_depth` | §5 |
| `difflayer_filter_pct` | §5 |
| `difflayer_total_nodes` | §5 |
| `evm_transact_us` | §11 |
| `exec_duration_ms` | §12 |
| `executor_finish` | §3 |
| `hashed_post_state` | §3 |
| `intermediate_inner_ms` | §4 |
| `intermediate_misses` | §7 |
| `lag_blocks` | §14 |
| `merge_transitions` | §3 |
| `needed_storage_accounts` | §13 |
| `node_admit_pct` | §10 |
| `node_insert_attempted` | §10 |
| `overall hit rate` | §6 |
| `overrun_ms` | §2 |
| `pre_exec_us` | §11 |
| `prefetched_storage_tries` | §13 |
| `prefetcher_finish` | §3 |
| `prefetcher_hook_us` | §11 |
| `prepare_duration_ms` | §1 |
| `receipt_build_us` | §11 |
| `resolve_difflayer_hit` | §5 |
| `resolve_fallthrough` | §5 |
| `resolve_total` | §5 |
| `save_blocks batch size` | §14 |
| `save_blocks_us` | §14 |
| `state_at_misses` | §7 |
| `state_at_ms` | §4 |
| `state_clone_us` | §11 |
| `state_root_total` | §3 |
| `stor_misses` | §6 |
| `storage_tries_count` | §9 |
| `to_triedb_state` | §3 |
| `total_commit_ms` | §12 |
| `total_evm_transact_ms` | §12 |
| `total_ms` | §4 |
| `total_prefetcher_hook_ms` | §12 |
| `triedb_calc` | §3 |
| `trie_cache_entries` | §10 |
| `trie_root_duration_ms` | §1 |
| `tx_count` | §1 |
| `update_account_trie_ms` | §8 |
| `update_state_objects_ms` | §8 |
