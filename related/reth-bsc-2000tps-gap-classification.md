# reth-bsc vs geth-bsc：2000 → 3000 TPS 差距分类与下一步实测计划

> 2026-04-22
> 起点：reth-bsc `fix/timestamp-drift` 分支实测稳态 ~2000 TPS，geth-bsc 对标 ~3000 TPS，gap ~1000 TPS
>
> 本文档整合 `docs/` 下全部历史分析（从 v1 的 `perf-gap-2000-to-3000-tps.md` 到 v3 的 `reth-bsc-vs-geth-bsc-final-summary.md`），剔除已作废结论，重新按**当前分支**的代码状态给出：
>
> 1. **已证负优化**（不再尝试）
> 2. **已验证/已实现**（不用再做）
> 3. **低风险可补**（2-4 周工程量，架构不动）
> 4. **架构性差距**（需要结构性改动）
> 5. **2000 TPS 下仍未验证、必须实测才能判定**
>
> 最后给出一份对应的 **2000 TPS 日志探针增补方案**，让后续测试的数据能直接落入该分类表。
>
> ⚠️ 重要前提：**历史测量全部基于 1200 TPS（433 tx/块）** 阶段。2000 TPS 下 tx/块预计 ~900，多项指标会**非线性**变化（尤其 state root p99、persistence 吞吐、moka 淘汰速率）。所以本次重测不能只把旧数据 ×2 线性外推，必须在 2000 TPS 下重新采样。

---

## 0. 当前分支状态快照

| 组件 | 分支 | HEAD | 相对上游变更 |
|---|---|---|---|
| reth-bsc | `fix/timestamp-drift` | `451eb53` | develop + 4 commits：timestamp 修正、speculative prefetcher warm-up |
| reth | （由 Cargo 拉 develop） | `396883b55` | 无 |
| reth-bsc-triedb | （由 Cargo 拉 develop） | `cc5c8e2` | **无 `aec0dc3` prefix.clone 优化**，**无任何 perf 探针** |

**关键：**
- `aec0dc3`（DiffLayer 去 `prefix.clone()`，曾测得 +40-60 TPS）**未进入当前构建**，仅在 triedb `feat/logs-on-develop` 分支上。
- 当前分支**没有任何性能探针**。`docs/perf-stage-by-stage-measurements.md` 里用到的 `state root breakdown` / `intermediate_and_commit breakdown` / `per-tx exec breakdown` 等日志**全部缺失**。
- `scripts/analyze_by_tps_stage.py` 依赖这些日志，跑在当前分支的 log 上会全部出 `n/a`。

**结论**：在 2000 TPS 跑分析之前，**必须先把探针加回来**。见 §5。

---

## 1. 已证负优化 —— 永远不要再做

> 来源：`reth-bsc-vs-geth-bsc-final-summary.md` §6.2-6.3、`perf-gap-2000-to-3000-tps.md` §5

这些尝试要么历史回退、要么实测反向、要么 flamegraph 已证明不是瓶颈：

| 尝试 | 为什么放弃 |
|---|---|
| Streaming storage trie（P0-A，独立 I/O 线程与 exec 流水线化） | 主线程 + I/O 线程 cache thrashing，反而变慢。当前 prefetcher 已每账户一线程异步 touch，无需再加一层 |
| Layer Tree（state-root-indexed 256-entry） | 和引擎树 DiffLayer 语义重合，查表开销 > 节省 |
| Flat DiffLayer history（合并成单一 flat map） | 生产负载产 bad block |
| Root caching / account trie caching（跨块复用 trie 结构） | 5+ 次尝试全部 bad block，`Committer` 不变式依赖未查清楚 |
| 独立 `clean_cache` for reads（moka 之外再加一层 read cache） | 两层查找开销 > 命中率提升，实测负优化 |
| Merged DiffLayer HashMap 索引（`1108780`，O(256)→O(1)） | bad block (1559 tx 块 state root 不一致)，已回滚。根因不明 |
| Block-STM / 并行 EVM | geth-bsc master 自己也已移除 `ParallelStateProcessor`，不值得重蹈 |
| 扩大 moka cache 容量 | 压测实测 `trie_cache_entries max 10.3M / cap 20M+`，**cache 从未满** |
| 重做 prefetcher 架构（per-storage-trie 重写） | 已和 geth `subfetcher` 对齐，行为正确；miss 率是 workload address diversity 决定的 |
| 旧版 `tx-execution-optimization-analysis.md` 全套（prefetcher hook 关闭、account 预加载、storage 热点预加载等） | 前提"per-tx 456μs"来自字段误读：`avg_tx_duration_micros = build/tx_count`，不是 per-tx 实际。flamegraph 实测 per-tx evm_transact 94μs，revm 本身不是瓶颈 |

**共同教训**：任何试图"跨块复用 trie 结构"的改动都极易踩 bad block；任何围绕 cache 容量或 prefetcher 并发度的改动在当前 workload 下都已经摸到天花板。

---

## 2. 已验证/已实现 —— 不用再做

> 来源：`reth-bsc-architectural-gaps-vs-geth-bsc.md` §0、`perf-gap-2000-to-3000-tps.md` §0

以下机制 geth-bsc 有、reth-bsc **也有且对齐**：

| 机制 | reth-bsc 当前实现位置 | 与 geth-bsc 对齐度 |
|---|---|---|
| 每 storage trie 一个异步 prefetcher | `reth/crates/engine/tree/src/tree/payload_processor/triedb_prefetcher.rs:553` `TrieDBPrefetchStorageTask` via `spawn_blocking` | 完全对齐（其实比 geth 多做一步：把解析好的 trie 对象直接交给 root 计算器） |
| 跨账户并行 storage trie 更新+哈希 | `reth-bsc-triedb/triedb/src/triedb_reth.rs:244-389` `rayon::join + par_iter` | 完全对齐 |
| 账户 trie root FullNode 16 子并行哈希 | `reth-bsc-triedb/state-trie/src/trie.rs:77` 阈值 `unhashed > 100` + `trie_hasher.rs:108-128` rayon | **完全一致**（同阈值、同策略） |
| commit 并行 | `triedb_reth.rs:437-447` `rayon::join` + `par_iter` for storage tries | 完全对齐 |
| 磁盘写异步 | engine tree 的 `save_blocks` 在独立 persistence 线程（`reth/crates/engine/tree/src/persistence.rs:139-160`） | 架构对齐（机制不同：geth 是 pathdb buffer flush goroutine） |
| DiffLayer 窗口深度 | 实测 `difflayer_chain_depth avg=256`，匹配配置 `persistence_threshold=256` | 等价（geth pathdb 是 128，**reth 更深**） |
| State root 同步只算 hash、commit 异步 | `intermediate_and_commit_hashed_post_state` 只产 DiffLayer，真写盘在 persistence 线程 | 对齐 |
| 账户 trie 更新串行 for 循环 | `triedb_reth.rs:168-177` | **geth 也串行**（`statedb.go:1054-1067`），不是 reth 落后 |

**也就是说**：过去文档里多次把这些当"差距来源"讨论，都是误判。**不要再提。**

---

## 3. 低风险可补 —— 架构不动，2-4 周可吃到的收益

> 这些基于 flamegraph（1200 TPS 已采集）和代码推断。**在 2000 TPS 下收益可能放大也可能不变**，需要重测。

| 优化 | 证据 | 工作量 | 风险 | 1200 TPS 下估计收益 |
|---|---|---|---|---|
| **E1. 把 `aec0dc3` 合进 develop** | 已有实测，`DiffLayer::get_trie_nodes` 去 `prefix.clone()` 省 2-3% CPU | 几小时（PR + merge） | 无 | **+40-60 TPS** |
| E2. RocksDB 参数调优（write_buffer_size、level0 trigger、bloom filter） | flamegraph `compaction` 5.7-5.9%，但随时间波动 | 1-2 周实验 | 低（参数化） | +50-100 TPS |
| E3. MDBX `MDBX_NOMETASYNC` / 更大 batch | 理论收益（crash consistency 权衡） | 1 周 | 低（crash 窗口略变长） | +30-80 TPS |
| E4. OnceLock 热点定位与替换 | flamegraph `OnceLock::initialize` 1.78-2.49%，但来源未定位 | 1 周（需要 profile） | 中（要动 lazy static） | +20-50 TPS |
| E5. `DiffLayers::get_trie_nodes` 真 O(1)（修好 `1108780` 的 bad block 根因后） | flamegraph `DiffLayers get_trie_nodes` 5.67-6.52% CPU | 2-3 周（含根因排查） | 高（历史 bad block 陷阱） | +100-150 TPS |

**注：** E1 是"从 logs 分支拉到 develop"的顺手事，没理由不做；E2-E4 是典型参数/细节调优，工程组可分工做。E5 必须先搞清楚上次 `1108780` 为何产 bad block，不然重走老路。

**全部做满（保守估）**：+200-400 TPS。能把 baseline 从 2000 推到 2200-2400。

---

## 4. 架构性差距 —— 要动数据结构或存储层

### 4.1 A1：Rust Arc-COW vs Go 指针原地改（FullNode 节点修改）

**事实**（flamegraph 证伪旧估算）：
- 旧估（基于代码推断）：-300 TPS
- 新估（flamegraph v3/v4）：`FullNode::to_mutable_copy_with_cow` 0.41% CPU，全 alloc 类别 <2% CPU
- **实际影响：-50 TPS 左右**，不是原估的 -300

**为什么是架构级**：Rust 多线程共享 FullNode 必须 clone；geth goroutine + GC 免费做共享协调。这是 Rust 的选型代价，**不可消除**。

**缓解空间**：`Arc::make_mut` 在 refcount=1 时 0-alloc，但 reth-bsc-triedb 的 FullNode 在 update 期间几乎都 refcount≥2（prefetcher task + main thread + DiffLayer 引用），能从 ~12000 alloc/块降到 ~3000-5000。**实测收益上限 +50 TPS**。

**结论**：A1 的 gap 比早期文档估的小得多，不值得单独为此花 1-2 周写 `Arc::make_mut` 改造。排期放到 E1-E5 之后。

---

### 4.2 A2：PathDB 缺 geth 的 buffer/frozen 温数据层

**事实**（Test 2 数据推翻了旧估算的一半）：
- 旧估（基于代码推断）：-500-800 TPS
- 新估（Test 2 DiffLayer chain depth=256 实测）：DiffLayer 窗口其实比 geth pathdb 的 128 块**更深**，"刚滑出窗口的温数据丢失"这个锅本就背不上

但有一部分**仍然有效**：
- 地址多样性导致 filter_pct=35%，即 65% 的 trie 节点读会穿透到 moka
- moka 对"从未访问过的冷节点"命中率低（15-25%），这些节点必须读 RocksDB
- geth 的 buffer/frozen 额外缓冲 2-3 个 flatten 周期的温数据，这部分 reth 没有

**当前实测收益上限**：-100-200 TPS（不是 -500-800）

**为什么是架构级**：需要新增 PathDB 的 buffer 和 frozen 两层结构、重写 flatten 流程、保证 crash consistency。3-4 周工程量 + canonical replay 验证。

**结论**：收益被高估。A2 工程量不小，但**真实收益可能只有 +100-200 TPS**。投入产出比低于 E 系列。

---

### 4.3 A3：TrieDB 模式下 plain state 双写 —— **证据不足，暂时搁置**

> `reth-bsc-vs-geth-bsc-final-summary.md` §4.3 已作废此条归因。

**事实**：
- persistence 线程与 miner 线程通过 mpsc 解耦，miner 不等 persistence
- 60s 采样 ~130 块：persistence 11.81% CPU × 16 核 × 60s / 130 块 ≈ 每块 54ms 持续工作 ≪ 450ms 预算
- 日志里**没有 back-pressure 证据**（没有"persistence falling behind"类警告）
- **在 1200 TPS 下没有证据说这会影响 miner 吞吐**

**但 2000 TPS 下必须重测**：tx/块从 433 涨到 ~900，MDBX 写入约 3 倍放大（PlainAccountState / PlainStorageState / ChangeSets），persistence 线程单块工作量可能从 54ms 升到 ~150ms。如果 `memory_block_buffer_target=128` 被打爆，miner 会开始阻塞。

**结论**：**暂时不归因**，但 §5 的 persistence 探针必须加上，**在 2000 TPS 下才能判定**。

---

### 4.4 A4：update_account_trie 串行循环（tail latency 威胁）

**事实**（Test 3 线性外推，未 2000 TPS 实测）：
- stage 04（1200 TPS，433 tx/块）：`update_account_trie` avg 47ms / **p99 138ms**
- 线性外推到 900 tx（2000 TPS）：avg ~100ms / **p99 ~290ms**
- geth-bsc 同路径（`trie.go:1054-1067`）也串行 —— 但 Go 的 `n.Children[i] = nn` 0-alloc，比 Rust 的 Arc-COW 快，所以 geth 的同路径 p99 估计在 80-120ms

**为什么是架构级**：账户 trie 是单棵深 trie，不能简单 par_iter。可能的并行化方向：按 root FullNode 的 16 个 child 切分 batch。但**需要动 Committer 不变式**，历史上这类改动多次撞 bad block。

**两条路**：
- 温和：`Arc::make_mut` + sorted batch insert 减 alloc，预计 avg 降到 ~70ms。工程量 1-2 周，风险中等。
- 激进：按 FullNode 16 child 切分 parallel update，avg 降到 ~30ms。工程量 3-4 周，风险**高**（bad block 陷阱）。

**2000 TPS 下的关键问题**：p99 是否真的冲过 450ms 预算？如果是，那么即便 avg 看着还行，miner 也会频繁丢块（或吐 EmptyFallback）。**必须实测**。

---

### 4.5 剩余未归因 gap（geth 积累的细节调优）

- geth-bsc 从 3000 → 3700 TPS 是过去一年积累的数十上百个小 perf PR（cache 大小、batch 参数、pool size、RocksDB options、GC 阈值等）
- 这部分 reth-bsc 只能持续跟进，**没有银弹**
- 当前关注的是 2000 → 3000（架构差距），不是 3000 → 3700（细节积累）

---

## 5. 2000 TPS 实测探针增补方案

> 当前分支 `fix/timestamp-drift` **无任何探针**。要让 2000 TPS 测试能直接跑 `analyze_by_tps_stage.py` 并且回答 §3-§4 里的开放问题，需要做两件事：

### 5.1 必须先把旧探针搬回来（L1 基础层）

从 `feat/logs-on-develop` cherry-pick 以下 commit 到 `fix/timestamp-drift`（或开一条 `feat/logs-on-fix-timestamp` 新分支）：

| commit | 提供的日志 | 必要性 |
|---|---|---|
| `4484ec3` state root timing | `state root breakdown` | 必需 |
| `ad04e6d` per-phase + per-account miss | `intermediate_and_commit breakdown` / `intermediate_inner breakdown` / `commit_inner breakdown` | 必需 |
| `529519a` prefetch storage coverage | `prefetch storage coverage` | 必需 |
| `92f5dd8` DiffLayer filter rate probe | `resolve_total` / `resolve_difflayer_hit` | 必需 |
| `4869545` moka admission probe | `commit_difflayer moka admission` | 必需 |
| `d480f02` DiffLayer chain depth probe | `difflayer_chain_depth` / `difflayer_total_nodes` | 必需 |
| `f4f4cc8` per-tx exec breakdown | `per-tx exec breakdown` | 必需 |

对应 triedb 侧也要一并 cherry-pick（或改 Cargo.toml 暂时指向 triedb 的 `feat/logs-on-develop`）。

**注意**：**顺带把 `aec0dc3` prefix.clone 去 clone 也拉进来**。这是唯一一个"已证有效但尚未入 develop"的优化（见 §3 E1）。

### 5.2 必须新增的探针（L2 针对性新问题）

2000 TPS 下四个新问题是旧探针回答不了的，需要新加：

#### P-1. Persistence 线程 back-pressure 探针 ★必需

**动机**：§4.3 的 A3 作废依赖"persistence 不落后"这个假设。2000 TPS 下必须重测。

**探针**（加在 `reth/crates/engine/tree/src/tree/mod.rs` 的 `on_new_persisted_block` 附近，或每次 `persist_blocks` 入口）：

```rust
tracing::debug!(
    target: "engine::persistence::lag",
    canonical_head_number,
    last_persisted_number,
    unpersisted_count = canonical_head_number - last_persisted_number,
    memory_block_buffer_target = self.config.memory_block_buffer_target(),
    persistence_threshold = self.config.persistence_threshold(),
    persistence_in_progress = self.persistence_state.in_progress(),
    save_blocks_us = last_save_duration_micros,  // 新加：上次 save_blocks 花了多久
    "persistence lag snapshot"
);
```

关键看：**`unpersisted_count` 是否持续接近或超过 `persistence_threshold`**。如果是，就是真有 back-pressure，A3 的归因需要重新立案。

#### P-2. build_payload 超时统计 ★必需

**动机**：§4.4 的 update_account_trie p99 是否实际冲过 450ms 预算无法线性外推。需要实测"本块超过 deadline"的频次。

**探针**（加在 `reth-bsc/src/node/miner/payload.rs` 的 `build_payload` 末尾）：

```rust
let deadline_used_pct = build_duration.as_millis() * 100 / 450;
let overrun_ms = build_duration.as_millis().saturating_sub(450);
tracing::debug!(
    target: "bsc::builder::deadline",
    block_number,
    tx_count,
    build_duration_ms,
    deadline_used_pct,
    overrun_ms,
    emitted_as_empty_fallback = ...,  // 如果有 fallback 信号
    "build deadline snapshot"
);
```

2000 TPS 下看 `overrun_ms p50/p95/p99` 和 `emitted_as_empty_fallback` 计数。这一条**直接回答"2000 TPS 的瓶颈是不是 tail latency"**。

#### P-3. state_root 子阶段 p99 逐块记录（非 stage 聚合） ★重要

**动机**：Test 3 里 stage 04 `update_account_trie` avg 47ms / p99 138ms 已知。但 2000 TPS 下 p99 是否冲过 450ms 是 §4.4 的关键。

**探针**（其实 `intermediate_inner breakdown` 已经逐块 emit，这里只要**确保 `bsc::builder::timing` 级别在 2000 TPS 下不被 rate-limit**）。

同时在 `analyze_by_tps_stage.py` 里追加对 `update_account_trie_ms` 的 **per-stage p99.9 和 max** 输出，而不只是 p99。

#### P-4. OnceLock 热点定位 ★可选

**动机**：flamegraph `OnceLock::initialize` 1.78-2.49%，但具体是哪个 lazy static 没定位。

**做法**：
- 先用 `perf record` 在 2000 TPS 下采样 30s，`perf script` 输出带符号栈
- `grep -A 5 OnceLock` 找到调用方。候选：snapshot provider、validator cache、system contract ABI decoder、precompile 表初始化
- 找到后再决定是否要 eagerly init

这条不需要日志探针，只需要 flamegraph 重跑一次。

#### P-5. RocksDB 内部统计 ★可选

**动机**：§3 的 E2（RocksDB 参数调优）需要 baseline 数据来指导。

**做法**：启动参数里加 `--db.verbose`（如果有）或让 pathdb 每 60s dump 一次 `rocksdb::DB::GetProperty` 的 `rocksdb.stats`。这个走 info 级别一次，不用 per-block。

---

## 6. 建议的 2000 TPS 测试 + 跟进步骤

### 第 1 步：把探针装回当前分支（1 天）

建议**开新分支** `feat/logs-on-fix-timestamp-drift`，基于 `fix/timestamp-drift`：

```bash
git checkout fix/timestamp-drift
git checkout -b feat/logs-on-fix-timestamp-drift
git cherry-pick 4484ec3 ad04e6d 529519a 92f5dd8 4869545 d480f02 f4f4cc8
# 加上 prefix.clone 修复（triedb 侧）：
# 改 reth-bsc/Cargo.toml 里 triedb deps 暂指到 feat/logs-on-develop
# 或合进 triedb develop
```

**注意**：历史 `feat/logs-on-develop` 的 cherry-pick 有过冲突（`triedb_reth.rs` 的 intermediate_and_commit 附近），需要手动解决，不是自动化步骤。

### 第 2 步：加 §5.2 的 P-1 / P-2 新探针（1-2 天）

P-1（persistence lag）和 P-2（deadline snapshot）直接写在当前分支，每块 emit 一次 debug 日志。P-3 检查现有字段够不够。

### 第 3 步：扩展分析脚本（半天）

`scripts/analyze_by_tps_stage.py` 加几个新字段：
- 吸收 `persistence lag snapshot` 日志，按 stage 统计 `unpersisted_count p99`、`save_blocks_us p99`
- 吸收 `build deadline snapshot`，按 stage 统计 `overrun_ms p99` 和超时块数
- `update_account_trie_ms` 追加 p999 和 max

### 第 4 步：2000 TPS 压测一轮（用户执行）

200 → 400 → 800 → 1200 → 1500 → 2000 TPS 阶梯各 10 分钟，对应 stage_01 到 stage_07（分析脚本分桶可能需要加一档 > 800 tx/块）。

运行：
```bash
python3 scripts/analyze_by_tps_stage.py reth.log --caller miner > stage_report_2000tps.txt
```

### 第 5 步：决策（基于报告）

用下面的**判定规则**直接判定每条架构问题：

| 观察 | 判定 |
|---|---|
| `overrun_ms p99` > 0 且 emitted_as_empty_fallback > 0 | **A4 确认**：update_account_trie 串行 tail 实际咬死 miner 预算，优先做 E5 + §4.4 温和方案 |
| `unpersisted_count` 稳定 ≤ 64 | **A3 作废**坚持不动 |
| `unpersisted_count` 逐步漂至 ≥ persistence_threshold | **A3 重新立案**：persistence 追不上，必需 E3（MDBX 参数）或 A3a 异步化 |
| `difflayer_filter_pct` 仍 35% 上下、moka hit rate ~15-25% | A2 （buffer/frozen）的假设**继续成立**，但收益上限就 +100-200，排在 E 系列之后 |
| flamegraph 里 `DiffLayers::get_trie_nodes` 占比 >5% | **E5 是高优先**（搞清楚 `1108780` bad block 根因后重做） |
| flamegraph 里 `OnceLock::initialize` 仍 ~2% | P-4 采样定位，可能吃到 E4 的 +20-50 TPS |

---

## 7. 一句话结论（向 reviewer/leader 汇报用）

> **reth-bsc 2000 TPS 对 geth-bsc 3000 TPS 的 1000 TPS 差距里，约 200-400 TPS 属于"参数/细节调优可追"（E1-E5，2-4 周），约 100-300 TPS 属于"架构级但可工程化补"（A2 PathDB 温数据层 3-4 周、A4 update_account_trie 温和优化 1-2 周），剩余 300-500 TPS 需要先在 2000 TPS 下复测才能判定是"A3 persistence 回到台面"还是"geth 积累调优的长尾"。**
>
> **三条关键 action**：
>
> 1. **立刻**：把 `aec0dc3` (prefix.clone) 合进 triedb develop，顺带搬探针分支到 `fix/timestamp-drift`。
> 2. **这周**：加 P-1 / P-2 两个新探针，跑 2000 TPS 一轮，拿到数据再判定。
> 3. **下周决策**：根据 §6 判定表决定是走 E5 (DiffLayers O(1))、A4 温和 (update_account_trie) 还是 A3a (persistence 异步化)。

---

## 附：被本次整合作废的历史结论（不要再引用）

| 旧说法 | 状态 | 替代结论 |
|---|---|---|
| "reth-bsc prefetcher 禁用" | 作废 | 每账户 `TrieDBPrefetchStorageTask` 异步跑，`payload.rs:411-424` wire 中 |
| "reth-bsc 并行 storage trie hash 没做" | 作废 | `triedb_reth.rs:244-389` rayon par_iter |
| "reth-bsc 并行 commit 没做" | 作废 | `triedb_reth.rs:437-447` rayon::join |
| "Arc-COW 贡献 -300 TPS" | 作废 | flamegraph <2% CPU，实际上限 -50 TPS |
| "PathDB 缺 buffer/frozen 贡献 -500-800 TPS" | 作废 | DiffLayer chain depth=256 已足，实际上限 -100-200 |
| "per-tx 270μs 成瓶颈" | 作废 | `avg_tx_duration_micros = build/tx_count` 字段误读。真 per-tx 94-109μs，revm 极快 |
| "MDBX 双写 -50-100 TPS" | 作废（1200 TPS 下） | persistence 未 back-pressure；**2000 TPS 下可能翻案，必须重测** |
| "3700 TPS 架构不可达" | 保留 | 2000→3000 架构上可达；3000→3700 需持续积累细节调优 |
| "分布 2-3 块的 DiffLayer 窗口" | 作废 | 实测 256 块（`persistence_threshold` 配置一致） |
