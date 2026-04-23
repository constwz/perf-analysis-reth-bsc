#!/usr/bin/env python3
"""Analyze reth-bsc miner logs, bucketing blocks by TPS stage.

Usage:
  python3 analyze_by_tps_stage.py reth.log
  python3 analyze_by_tps_stage.py reth.log --caller miner

Bucketing: each block is assigned to a TPS stage based on its user_tx_count
(from the `Block payload built successfully` log line).  Buckets target
200/400/800/1200/1500/2000 TPS at a 450 ms slot time:

  <= 90  tx/block -> stage_01_<=200tps
   91- 180        -> stage_02_400tps
  181- 360        -> stage_03_800tps
  361- 540        -> stage_04_1200tps
  541- 700        -> stage_05_1500tps
  701-1000        -> stage_06_2000tps
  >1000           -> stage_07_>2000tps

Correlation:
  Many probe logs (state-root breakdown, per-tx exec breakdown, ...) are
  emitted by build_payload BEFORE the corresponding "Block payload built
  successfully" line that establishes the block_number → bucket mapping.
  The old implementation bucketed events eagerly and dropped those that
  arrived before the mapping was known.  This version **buffers all events
  keyed by block_number** and correlates them at the end — no events lost.

Logs consumed:

  payload_builder                   `Block payload built successfully`
  bsc::builder                      `Calculated state root using triedb`
  bsc::builder::timing              `state root breakdown`
                                    `prefetch storage coverage`
                                    `per-tx exec breakdown`
  bsc::builder::deadline            `build deadline snapshot`            (P-2)
  triedb::timing                    `intermediate_and_commit breakdown`
                                    `intermediate_inner breakdown`
                                    `commit_inner breakdown`
  pathdb::admission                 `commit_difflayer moka admission`
  engine::tree  (opt RUST_LOG)      `Finished persisting, calling finish` (P-1)
  engine::persistence (opt)         `Saving range of blocks`              (P-1)

Probe changes since v1 (fix/timestamp-drift):
  - state root breakdown now emits `*_us` (u64) instead of `*_ms` (u128).
    Script accepts both; `_us` is preferred when present and displayed as ms.
  - per-tx exec breakdown counters are now instance-scoped on BscBlockExecutor
    (not process-global atomics), so concurrent speculative + normal builds
    don't contaminate each other's snapshot.
"""

import argparse
import re
import statistics
import sys
from collections import defaultdict

KV = re.compile(r'(\w+)=(0x[0-9a-fA-F]+|"[^"]*"|[\w.]+)')
ANSI = re.compile(r'\x1b\[[0-9;]*m')
DURATION_RE = re.compile(r'elapsed=([0-9.]+)(ns|µs|us|ms|s)\b')
LAST_PERSISTED_RE = re.compile(r'last_persisted_block_number=(\d+)')
BLOCK_COUNT_RE = re.compile(r'block_count=(\d+)')


def parse_kv(line):
    out = {}
    for m in KV.finditer(line):
        k, v = m.group(1), m.group(2).strip('"')
        try:
            out[k] = float(v) if '.' in v else int(v)
        except ValueError:
            out[k] = v
    return out


def pct(data, p):
    if not data:
        return 0
    s = sorted(data)
    idx = min(int(len(s) * p / 100), len(s) - 1)
    return s[idx]


def stat_line(label, values, unit=""):
    if not values:
        return f"  {label:<32s}: n/a"
    return (f"  {label:<32s}: avg={statistics.mean(values):>8.1f}{unit}  "
            f"p50={pct(values,50):>6}{unit}  "
            f"p95={pct(values,95):>6}{unit}  "
            f"p99={pct(values,99):>6}{unit}  "
            f"p999={pct(values,99.9):>6}{unit}  "
            f"max={max(values):>6}{unit}")


def bucket_for(tx_count):
    if tx_count <= 90:
        return "stage_01_<=200tps"
    if tx_count <= 180:
        return "stage_02_400tps"
    if tx_count <= 360:
        return "stage_03_800tps"
    if tx_count <= 540:
        return "stage_04_1200tps"
    if tx_count <= 700:
        return "stage_05_1500tps"
    if tx_count <= 1000:
        return "stage_06_2000tps"
    return "stage_07_>2000tps"


def parse_duration_us(line):
    m = DURATION_RE.search(line)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit == "ns":
        return value / 1_000.0
    if unit in ("µs", "us"):
        return value
    if unit == "ms":
        return value * 1_000.0
    if unit == "s":
        return value * 1_000_000.0
    return None


# Event buffers keyed by block_number. Correlation runs at end-of-file.
#
# Each entry maps block_number -> dict of probe fields we scraped for that block.
# When we see "Block payload built successfully" we note tx_count and build_ms;
# those two determine both the bucket and whether the block ever finished.
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", help="reth.log path")
    ap.add_argument("--caller", default="miner", choices=["miner", "import", "all"])
    args = ap.parse_args()

    # block_number -> {field: value, ...}
    per_block = defaultdict(dict)
    # Last seen block_number for logs that don't embed it (triedb::timing
    # *_breakdown).  These still lag the Block payload successful event, so we
    # attach them to that last block number and correlate later.
    last_block_seen = None

    persistence = {
        "save_durations_us": [],
        "last_persisted_history": [],
    }
    last_built_block_number = 0

    def merge(bn, fields, caller=None):
        """Merge fields into the per-block bucket."""
        if caller is not None and caller != "" and args.caller != "all" and caller != args.caller:
            return
        dst = per_block[bn]
        for k, v in fields.items():
            # Prefer first write so rebuilds don't overwrite the chosen build.
            if k not in dst:
                dst[k] = v

    with open(args.logfile, 'rb') as f:
        for raw in f:
            try:
                line = raw.decode('utf-8', errors='ignore')
            except Exception:
                continue
            line = ANSI.sub('', line)

            if "Block payload built successfully" in line:
                kv = parse_kv(line)
                bn = kv.get("block_number")
                if bn is not None:
                    last_block_seen = bn
                    if isinstance(bn, int) and bn > last_built_block_number:
                        last_built_block_number = bn
                merge(bn, {
                    "tx_count": kv.get("tx_count", 0),
                    "build_duration_ms": kv.get("build_duration_ms", 0),
                    "prepare_duration_ms": kv.get("prepare_duration_ms", 0),
                    "trie_root_duration_ms": kv.get("trie_root_duration_ms", 0),
                    "avg_tx_duration_micros": kv.get("avg_tx_duration_micros", 0),
                })
                continue

            if "build deadline snapshot" in line:
                kv = parse_kv(line)
                bn = kv.get("block_number")
                merge(bn, {
                    "overrun_ms": kv.get("overrun_ms", 0),
                    "deadline_used_pct": kv.get("deadline_used_pct", 0),
                    "over_budget_count": 1 if kv.get("overrun_ms", 0) > 0 else 0,
                })
                continue

            if "state root breakdown" in line:
                kv = parse_kv(line)
                bn = kv.get("block_number")
                if bn is not None:
                    last_block_seen = bn
                fields = {}
                # New probe: *_us fields as u64 microseconds.
                for k in (
                    "state_root_total_us",
                    "executor_finish_us",
                    "merge_transitions_us",
                    "hashed_post_state_us",
                    "prefetcher_finish_us",
                    "to_triedb_state_us",
                    "triedb_calc_us",
                ):
                    v = kv.get(k)
                    if v is not None:
                        # surface as ms with float precision so <1ms steps are visible
                        fields[k.replace("_us", "_ms_f")] = float(v) / 1000.0
                # Back-compat: older probe was *_ms (u128).
                for k in (
                    "state_root_total_ms",
                    "executor_finish_ms",
                    "merge_transitions_ms",
                    "hashed_post_state_ms",
                    "prefetcher_finish_ms",
                    "to_triedb_state_ms",
                    "triedb_calc_ms",
                ):
                    v = kv.get(k)
                    if v is not None:
                        fields[k] = v
                merge(bn, fields)
                continue

            if "prefetch storage coverage" in line:
                kv = parse_kv(line)
                bn = kv.get("block_number")
                merge(bn, {
                    "needed_storage_accounts": kv.get("needed_storage_accounts"),
                    "prefetched_storage_tries": kv.get("prefetched_storage_tries"),
                    "prefetched_storage_roots": kv.get("prefetched_storage_roots"),
                    "coverage_pct": kv.get("coverage_pct"),
                })
                continue

            if "intermediate_and_commit breakdown" in line:
                kv = parse_kv(line)
                caller = kv.get("caller", "")
                bn = last_block_seen
                merge(bn, {k: kv.get(k) for k in (
                    "total_ms", "state_at_ms", "intermediate_inner_ms", "commit_ms",
                    "cache_hits", "cache_misses", "acct_misses", "stor_misses",
                    "state_at_misses", "intermediate_misses", "commit_misses",
                    "intermediate_stor", "commit_stor",
                    "resolve_total", "resolve_difflayer_hit", "resolve_fallthrough",
                    "difflayer_filter_pct",
                    "difflayer_chain_depth", "difflayer_total_nodes",
                ) if kv.get(k) is not None}, caller=caller)
                continue

            if "intermediate_inner breakdown" in line:
                kv = parse_kv(line)
                caller = kv.get("caller", "")
                bn = last_block_seen
                merge(bn, {k: kv.get(k) for k in (
                    "update_state_objects_ms",
                    "update_account_trie_ms",
                    "account_hash_ms",
                    "account_count",
                ) if kv.get(k) is not None}, caller=caller)
                continue

            if "commit_inner breakdown" in line:
                kv = parse_kv(line)
                caller = kv.get("caller", "")
                bn = last_block_seen
                merge(bn, {k: kv.get(k) for k in (
                    "commit_state_objects_ms",
                    "storage_tries_count",
                ) if kv.get(k) is not None}, caller=caller)
                continue

            if "commit_difflayer moka admission" in line:
                kv = parse_kv(line)
                bn = kv.get("block_number")
                merge(bn, {k: kv.get(k) for k in (
                    "node_insert_attempted",
                    "node_insert_admitted",
                    "node_admit_pct",
                    "node_invalidated",
                    "trie_cache_entries",
                ) if kv.get(k) is not None})
                continue

            if "per-tx exec breakdown" in line:
                kv = parse_kv(line)
                bn = kv.get("block_number")
                merge(bn, {k: kv.get(k) for k in (
                    "exec_duration_ms",
                    "avg_pre_exec_us", "avg_evm_transact_us", "avg_state_clone_us",
                    "avg_prefetcher_hook_us", "avg_receipt_build_us", "avg_commit_us",
                    "total_pre_exec_ms", "total_evm_transact_ms", "total_state_clone_ms",
                    "total_prefetcher_hook_ms", "total_receipt_build_ms", "total_commit_ms",
                ) if kv.get(k) is not None})
                continue

            if "Finished persisting, calling finish" in line:
                m_lp = LAST_PERSISTED_RE.search(line)
                dur_us = parse_duration_us(line)
                if m_lp and dur_us is not None:
                    last_persisted = int(m_lp.group(1))
                    persistence["save_durations_us"].append(dur_us)
                    persistence["last_persisted_history"].append(
                        (last_built_block_number, last_persisted, dur_us)
                    )
                continue

            if "Saving range of blocks" in line:
                m_bc = BLOCK_COUNT_RE.search(line)
                if m_bc:
                    persistence.setdefault("save_block_counts", []).append(int(m_bc.group(1)))
                continue

    # ------------------------------------------------------------------
    # Correlate per-block events into stages.
    # ------------------------------------------------------------------
    data = defaultdict(lambda: defaultdict(list))
    for bn, fields in per_block.items():
        tx = fields.get("tx_count")
        if tx is None:
            # Block never finished (no "Block payload built successfully"); skip.
            continue
        bucket = bucket_for(tx)
        b = data[bucket]
        for k, v in fields.items():
            b[k].append(v)

    stages = sorted(data.keys())
    if not stages:
        print("No data collected. Ensure reth-bsc probes from fix/timestamp-drift "
              "are in this build and RUST_LOG includes "
              "payload_builder=debug,bsc::builder::timing=debug,"
              "bsc::builder::deadline=debug,triedb::timing=debug,"
              "pathdb::admission=debug.")
        sys.exit(1)

    for bucket in stages:
        b = data[bucket]
        n_blocks = len(b["tx_count"])
        if n_blocks < 3:
            continue
        print("=" * 88)
        print(f"  {bucket}   ({n_blocks} blocks, caller={args.caller})")
        print("=" * 88)

        # --- workload ---
        print()
        print("[workload]")
        print(stat_line("tx_count", b["tx_count"]))
        print(stat_line("build_duration_ms", b["build_duration_ms"], "ms"))
        print(stat_line("prepare_duration_ms", b["prepare_duration_ms"], "ms"))
        print(stat_line("trie_root_duration_ms", b["trie_root_duration_ms"], "ms"))
        print(stat_line("avg_tx_duration_micros (misleading!)",
                        b["avg_tx_duration_micros"], "us"))

        # --- deadline (P-2) ---
        print()
        print("[build deadline vs 450ms slot]")
        if b["deadline_used_pct"]:
            over_cnt = sum(b["over_budget_count"])
            pct_over = over_cnt * 100.0 / max(n_blocks, 1)
            print(f"  {'blocks over 450ms':<32s}: {over_cnt}/{n_blocks} "
                  f"({pct_over:.1f}%)")
        print(stat_line("deadline_used_pct", b["deadline_used_pct"], "%"))
        print(stat_line("overrun_ms (how far over 450ms)", b["overrun_ms"], "ms"))

        # --- state root timing (prefer _us variant) ---
        print()
        print("[state root breakdown (ms)]")
        def _sr(label, stem, unit="ms"):
            # Script-side converted us-field lives under <stem>_ms_f; legacy
            # u128 ms-field lives under <stem>_ms.
            vus = b.get(stem + "_ms_f")
            if vus:
                print(stat_line(label, vus, unit))
                return
            vms = b.get(stem + "_ms")
            if vms:
                print(stat_line(label + " (legacy ms)", vms, unit))
            else:
                print(stat_line(label, []))
        _sr("state_root_total", "state_root_total")
        _sr("executor_finish", "executor_finish")
        _sr("merge_transitions", "merge_transitions")
        _sr("hashed_post_state", "hashed_post_state")
        _sr("prefetcher_finish", "prefetcher_finish")
        _sr("to_triedb_state", "to_triedb_state")
        _sr("triedb_calc", "triedb_calc")

        # --- intermediate_and_commit ---
        print()
        print("[triedb intermediate_and_commit]")
        print(stat_line("total_ms", b["total_ms"], "ms"))
        print(stat_line("state_at_ms", b["state_at_ms"], "ms"))
        print(stat_line("intermediate_inner_ms", b["intermediate_inner_ms"], "ms"))
        print(stat_line("commit_ms", b["commit_ms"], "ms"))

        # --- DiffLayer filter ---
        print()
        print("[DiffLayer filter]")
        print(stat_line("resolve_total", b["resolve_total"]))
        print(stat_line("resolve_difflayer_hit", b["resolve_difflayer_hit"]))
        print(stat_line("resolve_fallthrough", b["resolve_fallthrough"]))
        print(stat_line("difflayer_filter_pct", b["difflayer_filter_pct"], "%"))
        print(stat_line("difflayer_chain_depth", b["difflayer_chain_depth"]))
        print(stat_line("difflayer_total_nodes", b["difflayer_total_nodes"]))

        # --- moka cache ---
        print()
        print("[moka cache (below DiffLayer)]")
        h, m = b["cache_hits"], b["cache_misses"]
        if h and m:
            th, tm = sum(h), sum(m)
            rate = th / max(th + tm, 1) * 100
            print(f"  {'overall hit rate':<32s}: {rate:.1f}%  ({th} hits / {th+tm} total)")
        print(stat_line("cache_hits per block", b["cache_hits"]))
        print(stat_line("cache_misses per block", b["cache_misses"]))
        print(stat_line("acct_misses (account trie)", b["acct_misses"]))
        print(stat_line("stor_misses (storage trie)", b["stor_misses"]))

        # --- per-phase misses ---
        print()
        print("[per-phase misses]")
        print(stat_line("state_at_misses", b["state_at_misses"]))
        print(stat_line("intermediate_misses", b["intermediate_misses"]))
        print(stat_line("commit_misses", b["commit_misses"]))

        # --- intermediate_inner ---
        print()
        print("[intermediate_inner — update_account_trie is serial]")
        print(stat_line("update_state_objects_ms", b["update_state_objects_ms"], "ms"))
        print(stat_line("update_account_trie_ms (SERIAL)",
                        b["update_account_trie_ms"], "ms"))
        print(stat_line("account_hash_ms", b["account_hash_ms"], "ms"))
        print(stat_line("account_count", b["account_count"]))

        # --- commit_inner ---
        print()
        print("[commit_inner]")
        print(stat_line("commit_state_objects_ms", b["commit_state_objects_ms"], "ms"))
        print(stat_line("storage_tries_count", b["storage_tries_count"]))

        # --- admission ---
        print()
        print("[moka admission]")
        print(stat_line("node_admit_pct", b["node_admit_pct"], "%"))
        print(stat_line("trie_cache_entries", b["trie_cache_entries"]))
        print(stat_line("node_insert_attempted", b["node_insert_attempted"]))

        # --- per-tx exec breakdown ---
        print()
        print("[per-tx exec breakdown (microseconds)]")
        print(stat_line("pre_exec_us", b["avg_pre_exec_us"], "us"))
        print(stat_line("evm_transact_us", b["avg_evm_transact_us"], "us"))
        print(stat_line("state_clone_us", b["avg_state_clone_us"], "us"))
        print(stat_line("prefetcher_hook_us", b["avg_prefetcher_hook_us"], "us"))
        print(stat_line("receipt_build_us", b["avg_receipt_build_us"], "us"))
        print(stat_line("commit_us", b["avg_commit_us"], "us"))
        print()
        print("[per-tx exec totals per block (ms)]")
        print(stat_line("total_evm_transact_ms", b["total_evm_transact_ms"], "ms"))
        print(stat_line("total_prefetcher_hook_ms", b["total_prefetcher_hook_ms"], "ms"))
        print(stat_line("total_commit_ms", b["total_commit_ms"], "ms"))
        print(stat_line("exec_duration_ms (loop total)", b["exec_duration_ms"], "ms"))

        # --- prefetch coverage ---
        print()
        print("[prefetch storage coverage]")
        print(stat_line("coverage_pct", b["coverage_pct"], "%"))
        print(stat_line("needed_storage_accounts", b["needed_storage_accounts"]))
        print(stat_line("prefetched_storage_tries", b["prefetched_storage_tries"]))

        print()

    # --- global persistence-thread report (P-1) ---
    print("=" * 88)
    print("  persistence thread   (P-1: back-pressure signal)")
    print("=" * 88)
    print()
    dur = persistence["save_durations_us"]
    if dur:
        print(stat_line("save_blocks_us (per event)", dur, "us"))
    else:
        print("  save_blocks events: 0 observed.")
        print("  To capture P-1, add `engine::tree=debug,engine::persistence=debug`")
        print("  to RUST_LOG and re-run. Example:")
        print("    RUST_LOG=\"info,bsc::builder::timing=debug,bsc::builder::deadline=debug,"
              "triedb::timing=debug,payload_builder=debug,pathdb::admission=debug,"
              "engine::tree=debug,engine::persistence=debug\"")

    if persistence["last_persisted_history"]:
        lags = [
            max(built - last_persisted, 0)
            for built, last_persisted, _dur in persistence["last_persisted_history"]
            if built > 0
        ]
        if lags:
            print()
            print("[persistence lag at completion (unpersisted at that moment)]")
            print(stat_line("lag_blocks", lags))

    if persistence.get("save_block_counts"):
        print(stat_line("save_blocks batch size", persistence["save_block_counts"]))

    print()


if __name__ == "__main__":
    main()
