"""
Update local TDX .day files from mootdx (live quote servers).

Phase 4 operational continuity tool. The local vipdoc tree
(D:/new_tdx_mock/vipdoc) is the pipeline's primary K-line source, but it is
a static snapshot — this script appends the missing recent bars fetched via
mootdx so the offline pipeline (LocalDataProvider → FactorSnapshotBuilder →
Guardian) can run for new trade dates.

Record layout (32 bytes, little-endian), matching src/data/local_provider.py:
    I(date YYYYMMDD) I(open cents) I(high cents) I(low cents) I(close cents)
    f(amount yuan) I(volume shares) I(reserved=0)

Conventions verified 2026-07-27 against mootdx:
  - prices stored as integer cents (price * 100)
  - volume stored in shares (mootdx 'vol' lots * 100)
  - amount in yuan (mootdx 'amount' as-is)

Scope: STOCK files only (sh6*, sz0*, sz3*, bj*). Index files (sh000*, sz399*)
are skipped — the pipeline reads indices from cache.db market_index_daily,
synced separately via IndexDataProvider (Tencent HTTP fallback).

Usage:
    python scripts/update_vipdoc_from_mootdx.py            # update all stale files
    python scripts/update_vipdoc_from_mootdx.py --limit 50 # smoke test
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)

VIPDOC = os.environ.get("STOCK_SIEVE_TDX_VIPDOC", "D:/new_tdx_mock/vipdoc")
REC = struct.Struct("<IIIIIfII")
FETCH_BARS = 40  # covers ~8 weeks of missing bars (suspension gaps beyond this are accepted)


def last_date_of(path: str) -> int:
    """Read the date int of the final record, or 0 for empty/unreadable."""
    try:
        size = os.path.getsize(path)
        if size < 32:
            return 0
        with open(path, "rb") as f:
            f.seek(size - 32)
            return REC.unpack(f.read(32))[0]
    except OSError:
        return 0


def iter_stock_files(root: str):
    """Yield (code, path) for stock .day files, skipping index files."""
    for market, prefixes in (("sh", ("6",)), ("sz", ("0", "3")), ("bj", ("4", "8"))):
        lday = os.path.join(root, market, "lday")
        if not os.path.isdir(lday):
            continue
        for fname in sorted(os.listdir(lday)):
            if not fname.endswith(".day"):
                continue
            code = fname[len(market) : -4]
            # sh000*/sz399* are indices; stock codes match the market prefixes
            if not code.startswith(prefixes):
                continue
            if market == "sz" and code.startswith("399"):
                continue
            yield code, os.path.join(lday, fname)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update vipdoc .day files from mootdx")
    parser.add_argument("--limit", type=int, default=0, help="Only process N files (smoke test)")
    parser.add_argument("--skip", type=int, default=0, help="Skip the first N files (chunked runs)")
    args = parser.parse_args()

    from mootdx.quotes import Quotes

    client = Quotes.factory(market="std")

    # Latest trade date available on the quote server (probe one liquid stock).
    # Files already current are skipped WITHOUT a network request, making
    # re-runs after interruption cheap.
    probe = client.bars(symbol="600519", frequency=9, offset=3)
    target_date = (
        int(probe.index[-1].strftime("%Y%m%d")) if probe is not None and not probe.empty else 0
    )
    print(f"target (latest server) trade date: {target_date}", flush=True)

    files = list(iter_stock_files(VIPDOC))
    if args.skip:
        files = files[args.skip :]
    if args.limit:
        files = files[: args.limit]
    total = len(files)
    print(f"vipdoc: {VIPDOC}")
    print(f"stock .day files: {total}", flush=True)

    updated = skipped_fresh = failed = no_new_bars = 0
    appended_total = 0
    t0 = time.time()

    for i, (code, path) in enumerate(files):
        try:
            last = last_date_of(path)
            if target_date and last >= target_date:
                skipped_fresh += 1
                continue
            df = client.bars(symbol=code, frequency=9, offset=FETCH_BARS)
            if df is None or df.empty:
                no_new_bars += 1
                continue

            new_recs = []
            for _, row in df.iterrows():
                d = int(row.name.strftime("%Y%m%d"))
                if d <= last:
                    continue
                new_recs.append(
                    REC.pack(
                        d,
                        int(round(float(row["open"]) * 100)),
                        int(round(float(row["high"]) * 100)),
                        int(round(float(row["low"]) * 100)),
                        int(round(float(row["close"]) * 100)),
                        float(row["amount"]) if row["amount"] == row["amount"] else 0.0,
                        int(round(float(row["vol"]) * 100)),
                        0,
                    )
                )

            if not new_recs:
                skipped_fresh += 1
                continue

            with open(path, "ab") as f:
                for rec in new_recs:
                    f.write(rec)
            updated += 1
            appended_total += len(new_recs)

        except Exception as e:  # noqa: BLE001 — per-stock isolation, keep going
            failed += 1
            if failed <= 10:
                logger.warning("update_vipdoc: %s failed: %s", code, e)

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(
                f"  {i + 1}/{total} ({(i + 1) / elapsed:.0f}/s) "
                f"updated={updated} fresh={skipped_fresh} "
                f"no_bars={no_new_bars} failed={failed}",
                flush=True,
            )

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s")
    print(f"  updated files:   {updated} ({appended_total} bars appended)")
    print(f"  already fresh:   {skipped_fresh}")
    print(f"  no new bars:     {no_new_bars} (suspended/delisted?)")
    print(f"  failed:          {failed}")


if __name__ == "__main__":
    main()
