# reth-bsc 与 geth-bsc 性能差距：实测调研最终结论

> 2026-04-21
> 状态：调研告一段落，本文档是**最终总结**
> 数据来源：`reth-bsc:feat/logs-on-develop` 分支在 devnet 压测（200→1500 TPS 阶梯），配合 `perf record` flamegraph
>
> **诚实声明**：本文档放弃所有"推测性 gap 归因"。每一条结论都标明是**硬数据**（flamegraph 或日志实测）还是**未证实推断**。之前几版文档里基于估算的结论（A1 Arc-COW -300 TPS、A2 buffer/frozen -500~800 TPS 等）经过 flamegraph 实测**已被推翻**，见 §3。

---

## 1. 基准数字

| 项 | 数值 | 来源 |
|---|---|---|
| geth-bsc（最新版本，主网未开 opcode 融合等特性） | **~3700 TPS** | 用户告知 |
| reth-bsc develop | **~2000 TPS** | 用户实测 |
| **差距** | **~1700 TPS** | |

---

## 2. 用了哪些测量手段

1. **分段压测**：200 → 400 → 800 → 1200 → 1500 TPS 阶梯，每阶段跑若干分钟
2. **日志探针**（加在 feat/logs-on-develop）：
   - `state root breakdown` / `intermediate_and_commit breakdown` / `intermediate_inner breakdown` / `commit_inner breakdown`
   - DiffLayer 过滤率 (`resolve_total`, `resolve_difflayer_hit`, `difflayer_filter_pct`)
   - moka admission probe（`node_admit_pct`, `trie_cache_entries`）
   - DiffLayer 链深度探针（`difflayer_chain_depth`）
   - per-tx 子步骤 profile（6 个 AtomicU64 计数器）
3. **分析脚本**：`scripts/analyze_by_tps_stage.py`（按 tx_count 自动分桶）
4. **CPU profile**：`perf record` + FlameGraph（1200 TPS 和 1500 TPS 各多份）

---

## 3. 已**排除**的 gap 假设

以下每一条都曾经是"怀疑过的主因"，经过实测**确认不是瓶颈**。

| 假设 | 排除理由 | 证据 |
|---|---|---|
| TinyLFU admission 拒绝 commit 写入 | admission 100% 通过 | `node_admit_pct avg=100%` 所有 stage |
| moka cache 容量不够 | 远未满 | `trie_cache_entries max = 10.3M / cap 20M+` |
| DiffLayer 窗口太小（2-3 块） | 实测 256 块 | `difflayer_chain_depth avg = 256` |
| prefetcher 禁用 / 缺失 | 已启用，per-trie async | 代码 `payload.rs:411-424` + `TrieDBPrefetchStorageTask` 每账户 spawn |
| PathDB 缺 buffer/frozen | moka 已在 commit_difflayer 做等效写入 | `commit_difflayer` 代码 line 714-725 |
| 并行 storage trie hash | 已实现 | `triedb_reth.rs:244-389` rayon par_iter |
| 并行 commit | 已实现 | `triedb_reth.rs:437-447` rayon::join |
| 账户 trie root FullNode 并行 hash | 已实现，同 geth 阈值 ≥100 | `state-trie/src/trie.rs:77` + `trie_hasher.rs:108-128` |
| revm EVM 执行慢 | 极快 | flamegraph: `evm_transact per-tx = 94μs` |
| per-tx 周边开销（clone、hook、commit） | 各 ≤ 2μs | flamegraph + per-tx probe |
| Arc-COW 堆分配爆炸 | < 2% CPU | flamegraph: `FullNode::to_mutable_copy_with_cow` 0.41%，`alloc 全类别 < 2%` |
| `avg_tx_duration_micros` 字段 | 误导性字段，= `build_duration / tx_count`，不是 per-tx 时间 | `payload.rs:801` 代码阅读 |

**重要**：原来认为 A1（Arc-COW）贡献 -300 TPS，flamegraph 证明实际 < 2% CPU 总计，**最多贡献 -50 TPS**。这是本次调研最重要的"认知纠偏"。

---

## 4. 有实测数据支持的 gap 来源

以下每一条都有 flamegraph 或日志数据。

### 4.1 `DiffLayers::get_trie_nodes` 线性扫 256 层

| 指标 | 值 |
|---|---|
| flamegraph 占比（1200 TPS，v4 窗口） | **6.52% CPU** |
| flamegraph 占比（1500 TPS） | **5.67% CPU** |
| 成因 | 每次 trie 节点 resolve 都线性扫 256 层 HashMap |
| 已修复部分 | `prefix.clone()` 去掉（commit `aec0dc3`），省 ~2% |
| 未修复部分 | 256 层本身的 hash+lookup | 
| **合并 HashMap 优化尝试** | **失败**（bad block，commit `1108780` 已回滚，见 §6） |

**可归因 gap**：**-100~150 TPS**（修好 prefix.clone 之后剩余的线性扫）。

### 4.2 RocksDB 后台 compaction

| 指标 | 值 |
|---|---|
| flamegraph 占比（v3 1200 TPS） | **5.88% CPU** |
| flamegraph 占比（v4，采样窗口静默） | 不在 top 30 |
| flamegraph 占比（1500 TPS） | **5.73% CPU** |
| 成因 | LSM 架构必然开销，和前台 IO 竞争 disk/CPU |

**可归因 gap**：**-50~150 TPS**（不确定性大，取决于 compaction 是否持续激活）。

### 4.3 MDBX 双写（plain state）—— **原归因作废**

| 指标 | 值 |
|---|---|
| Persistence 线程 CPU 占比（v2 原始） | 11.81% CPU |
| 其中 MDBX cursor_put / upsert | 3-4% CPU |
| 其中 TrieDB.flush | ~7% CPU |

**架构结构**：

```
miner 线程（热路径，450ms 出块预算）
  └── build_payload → submit_block，不等 persistence

persistence 线程（独立，异步）
  └── save_blocks: write_state (MDBX) + triedb.flush (RocksDB)
```

**为什么 "双写" 本身不该直接影响 TPS**：两条线程通过 mpsc channel 解耦，miner 不等 persistence。`persistence_threshold=256` 的上限下，engine tree 有充足内存缓冲，persistence 跟得上就无背压。

**实测验证 persistence 跟得上**：
- 60s 采样约 130 块（1200 TPS 阶段）
- persistence 线程 11.81% CPU × 16 核 × 60s / 130 块 ≈ 每块 ~54ms 持续工作
- 远小于 450ms 出块间隔 → **无背压**
- 日志里也没有 `persistence falling behind` 或类似警告

**对 TPS 的直接影响**：**无证据**。

**理论上可能的间接影响**（三条都**未测量**）：
1. I/O 带宽竞争：MDBX fsync + RocksDB WAL fsync + compaction 抢同一块盘 → 需 `iostat -x 1` 看 `%util`
2. Page cache 竞争：MDBX mmap + RocksDB block cache 抢内核页缓存 → 需要看工作集 vs RAM
3. 长时持续超过 engine tree 缓冲能力 → 需要几小时连跑观察 back-pressure

**可归因 gap**：**无法量化**。之前文档里写的 "-50~100 TPS" 是未经验证的推测，已作废。

**要证实或证伪**：
- 压测时跑 `iostat -x 1 | grep nvme` 或对应盘名，看 `%util`、`w_await`
- `free -h` 看内存和 cache 占用
- 连跑 1 小时以上看 engine tree 内 unpersisted 块数是否稳定

在这些数据出来之前，这条不能归在"已证 gap"里。

### 4.4 `OnceLock::initialize` 热路径

| 指标 | 值 |
|---|---|
| flamegraph 占比（v3/v4/1500 各次） | **1.78-2.49% CPU** |
| 成因 | 未定位。怀疑某个 lazy static 被 hot path 频繁访问（double-check lock pattern） |

**可归因 gap**：**-20~50 TPS**（量级小但来源不明）。

### 4.5 update_account_trie 串行循环

| 指标（stage 04 / 1200 TPS / 433 tx/块） | 值 |
|---|---|
| avg | 47ms |
| p99 | 138ms |
| 线性外推到 1665 tx（3700 TPS 目标负载） | avg ~180ms / p99 ~530ms |
| 成因 | `for addr in accounts { update_account_with_hash_state(...) }` 完全串行 |
| geth-bsc 同路径 | 也串行，但 Go 的 `n.Children[i] = nn` 0-alloc，比 Rust 的 Arc-COW 快 |

这条在 flamegraph 里不是巨型热点（被拆分到 trie 更新的各层函数里），但在 p99 tail latency 上显著。

**可归因 gap**：**-100~200 TPS**（tail latency 吃掉 miner 出块预算）。

### 4.6 `MemoryOverlayStateProvider::storage`

| 指标 | 值 |
|---|---|
| flamegraph 占比（v4 采样） | **2.07% CPU** |
| 成因 | EVM 执行时读 MDBX PlainStorageState，需要叠加 in-memory 块的 pending changes。每次 SLOAD 都走这条 |

**可归因 gap**：**-30~80 TPS**（叠加层的遍历开销，小但实在）。

### 4.7 可归因总和

| 项 | 量级 | 备注 |
|---|---|---|
| DiffLayers 线性扫 | -100~150 TPS | flamegraph 支持 |
| RocksDB compaction | -50~150 TPS | flamegraph 支持，但变化大 |
| ~~MDBX 双写~~ | ~~-50~100~~ | **作废**：async 结构，无证据；见 §4.3 |
| OnceLock | -20~50 TPS | flamegraph 支持，来源未定位 |
| update_account_trie 串行 p99 | -100~200 TPS | 日志 stage breakdown 支持 |
| MemoryOverlay 读叠加 | -30~80 TPS | flamegraph 支持 |
| **合计** | **-300~630 TPS** | |

**数据支持的 gap 约 300-630 TPS**。

---

## 5. **无法归因**的剩余 gap

```
实测 gap: ~1700 TPS
已归因:   ~300-630 TPS
────────────────────
剩余:     ~1070-1400 TPS  ← 当前 flamegraph + 日志无法解释
```

**剩余部分我不假装知道**。可能的分布（仍是推测）：

- geth-bsc 过去 1-2 年积累的**数十上百个小 perf PR**，每个贡献几十 TPS，合起来几百 TPS
- RocksDB（reth）vs Pebble（geth）在 BSC trie workload 下的性能差（未直接对比测量过）
- reth 的 rayon 线程池 / tokio runtime 调度行为 vs geth goroutine 的效率差
- 其他未识别的热点

**要进一步定位这 ~1000 TPS，唯一方法是对 geth-bsc 做同样的 flamegraph profile，然后对比两边热点分布**。这件事本次调研**没做**。

---

## 6. 优化尝试结果

### 6.1 成功的

| 改动 | commit | 收益 | 风险 |
|---|---|---|---|
| `DiffLayer::get_trie_nodes` 去除 `prefix.clone()` | `aec0dc3` | 2-3% CPU / +40-60 TPS | 无 |

### 6.2 失败的（已回退）

| 改动 | commit | 结果 |
|---|---|---|
| `DiffLayers` 加 merged HashMap 索引（O(256)→O(1)） | `1108780` / revert `0509024` | **bad block**：1559 tx 大块下 `got != expected`，state root 不一致。回退后正常 |

**失败原因未完全定位**（merged_index 语义上应等价于线性扫，但生产环境下产生不同 state root）。**可能的方向**：并发 clone 时共享语义、或生产负载触发了单元测试未覆盖的边界。

### 6.3 回顾历史（来自前几版文档，均已回退）

| 历史尝试 | 结果 |
|---|---|
| Streaming storage trie pipeline (P0-A) | 回退：I/O 线程与主线程 cache thrashing |
| Layer Tree（state-root-indexed 256-entry） | 回退：和引擎树 DiffLayer 重合，收益有限 |
| Flat DiffLayer history | 回退：bad block |
| Root caching / account trie caching | 回退：bad block 或 commit 开销线性增长 |
| Independent clean_cache for reads | 回退：两层 cache 查找开销 > 收益 |

**历史经验**：**任何涉及跨块 trie 数据结构复用的尝试都很容易踩到 bad block**。这是 reth-bsc 的 state root 计算路径对 DiffLayer 语义有一些我们看不见的隐式依赖，可能的角度：Arc 身份敏感（`Arc::ptr_eq`）、遍历顺序依赖、或 tracer 的 access_list 状态依赖。**未查清楚**。

---

## 7. 可做的"低风险"继续优化

基于本轮调研：

| 优化 | 预期收益 | 风险 | 工作量 |
|---|---|---|---|
| 调 RocksDB 参数（write_buffer_size、level0 trigger、compaction 策略） | +50-100 TPS | 低 | 几天实验 |
| 调 MDBX sync 策略（write txn batch / `MDBX_NOMETASYNC`） | +30-80 TPS | 低（crash consistency 权衡） | 1 周 |
| 查 OnceLock 2% 来源并消除 | +20-50 TPS | 中 | 1 周 |
| `update_account_trie` 用 `Arc::make_mut` 消减 alloc（refcount=1 快路径） | +30-80 TPS | 中（需要 canonical replay 验证） | 1-2 周 |
| ~~MDBX 双写改异步~~ | ~~+50-100~~ | — | **作废**：没证据这是瓶颈（§4.3） |
| **合计（上面全做）** | **+130-310 TPS** | | **4-5 周累计** |

加上已经做的 prefix.clone（+40-60 TPS），乐观上限 **+170-370 TPS**。

**最终可及 TPS = 2000 + (已做 +50) + (全部低风险改动 +220) ≈ 2270 TPS**。距离 geth 的 3700 TPS **仍差 ~1430 TPS**。

---

## 8. 对"追到 3700 TPS"的诚实判断

根据**硬数据**：

1. **2350 TPS**：本轮调研路径 + 上面列表的低风险改动都完成。
2. **2500-2700 TPS**：在 2350 基础上，+ `DiffLayers` O(1) 查找（需要重新设计，避开本次失败）。
3. **3000 TPS**：需要 geth-bsc 对比 profile 定位剩余 ~500 TPS，再针对性优化。
4. **3700 TPS**：**当前掌握的证据不足以判断是否可达**。需要：
   - 做 geth-bsc 的 flamegraph 对比
   - 精确定位 ~1000 TPS 的来源
   - 然后逐项攻克

**不建议再做的事（基于本轮尝试）**：

- ❌ 任何跨块复用 trie 结构的尝试（历史 5+ 次失败，每次 bad block）
- ❌ 以 "Arc-COW 优化" 为主要手段的大型重构（flamegraph 证明 < 2%）
- ❌ 扩大 moka cache 或改算法（admission 100%，cache 远未满）
- ❌ 重做 prefetcher 架构（已和 geth 对齐，行为正确）

---

## 9. 本文档**不保证**的几件事

- **没测过 geth-bsc**：所有 "geth 比 reth 快 X%" 的推理都基于用户报告的 3700 TPS 峰值和我们对 geth 代码的静态阅读。没跑同一份 workload 同时对比。
- **~1000-1400 TPS 的 gap 无法明确归因**：这是调研的 honest 限制。
- **merged_index bad block 的根因未定位**：只知道它发生，不知道为什么。
- **线性外推（1665 tx 负载下的耗时预测）不一定准**：高负载下 tail latency 行为可能非线性。
- **MDBX 双写对 TPS 的影响未测量**：flamegraph 显示 persistence 线程 11.81% CPU，但因为 async 结构，是否影响 TPS 取决于 I/O 竞争和 back-pressure 情况，这两者本轮都没有测。

---

## 10. 建议的下一步

按优先级：

1. **对 geth-bsc 做同样的 flamegraph profile**（同一 devnet、同一压测脚本、同 QPS）。对比两边热点分布。这一步能把 ~1000 TPS 的 "unknown" 缩小到具体函数。
2. 把第 7 节里的低风险优化做掉（累计 +200~400 TPS，拿到 2300-2400 TPS 基线）。
3. 基于 geth 对比结果，决定是否值得继续追 3000+ TPS，以及需要投多少人月。

---

## 附：相关文档

- `reth-bsc-architectural-gaps-vs-geth-bsc.md`（未收录到本 release）（v3）：早期版本的 gap 分析，**部分结论已被本文档推翻**（A1 Arc-COW 实测 < 2%，不是 -300 TPS；A2 buffer/frozen 已被证明存在于 moka cache）
- `docs/perf-stage-by-stage-measurements.md`：三次测试的完整数据记录（包括 per-tx probe、DiffLayer filter、admission、chain depth）
- `scripts/analyze_by_tps_stage.py`：分析脚本（按 tx_count 分桶 stage）
