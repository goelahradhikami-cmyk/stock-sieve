"""
Universe Filter v2 — Rule-based stock universe screening with stage tracking.

Commit 6-B.2: Adds filter_stage, dynamic liquidity, universe_snapshot persistence.
"""

from datetime import date

import pandas as pd

from src.data.db import managed_connect


class UniverseFilter:
    """Filter security_master DataFrame into investable universe.

    Records each stock's filter stage, reason, and writes daily snapshots.
    """

    def __init__(
        self,
        db_path: str = "data/cache.db",
        exclude_st: bool = True,
        min_days_listed: int = 60,
        min_avg_amount_20d: float = 5000,
        use_dynamic_liquidity: bool = True,
        amount_percentile: float = 0.30,
        exclude_bj: bool = False,
    ):
        self.db = managed_connect(self, db_path)
        self.exclude_st = exclude_st
        self.min_days_listed = min_days_listed
        self.min_avg_amount_20d = min_avg_amount_20d
        self.use_dynamic_liquidity = use_dynamic_liquidity
        self.amount_percentile = amount_percentile  # Keep top (1-pct) by liquidity
        self.exclude_bj = exclude_bj
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS universe_filter_log (
                trade_date DATE NOT NULL,
                security_id TEXT NOT NULL,
                pass_flag INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                filter_stage INTEGER DEFAULT 0,
                avg_amount_20d REAL,
                total_mv REAL,
                PRIMARY KEY (trade_date, security_id)
            );
            CREATE TABLE IF NOT EXISTS universe_snapshot (
                trade_date DATE NOT NULL,
                security_id TEXT NOT NULL,
                universe_type TEXT DEFAULT 'A',
                score REAL DEFAULT 0.0,
                PRIMARY KEY (trade_date, security_id)
            );
            CREATE TABLE IF NOT EXISTS tradable_universe (
                security_id TEXT PRIMARY KEY,
                last_trade_date DATE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.db.commit()

    def filter(self, df: pd.DataFrame, target_date: date | None = None) -> pd.DataFrame:
        """Full filtering pipeline with stage tracking + snapshot."""
        return self._do_filter(df, target_date)

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """Alias for filter() — backward compatibility with v1 API."""
        return self._do_filter(df, None)

    def _do_filter(self, df: pd.DataFrame, target_date: date | None = None) -> pd.DataFrame:
        if target_date is None:
            target_date = date.today()

        if df.empty:
            return df

        records = []  # for universe_filter_log
        passed_codes = []  # for universe_snapshot + tradable_universe

        # ── Fix 4: Absolute liquidity floor ───────────────
        # Only apply when we have real market data (non-zero values)
        if (
            self.use_dynamic_liquidity
            and "avg_amount_20d" in df.columns
            and df["avg_amount_20d"].max() > 0  # Only if real data present
        ):
            df = df[df["avg_amount_20d"] >= 1000]

        # ── Fix 4: Dynamic percentile threshold ───────────
        dynamic_threshold = self.min_avg_amount_20d
        if (
            self.use_dynamic_liquidity
            and "avg_amount_20d" in df.columns
            and df["avg_amount_20d"].max() > 0  # Only compute if real data present
        ):
            dynamic_threshold = max(
                self.min_avg_amount_20d, df["avg_amount_20d"].quantile(self.amount_percentile)
            )

        # ── Per-stock filtering with stage tracking ───────
        for _, row in df.iterrows():
            stage = 0
            reason = "passed"
            pass_flag = 1

            # Stage 1: Active status
            stage += 1
            if row.get("status", "active") != "active":
                reason, pass_flag = "not_active", 0

            # Stage 2: ST exclusion
            if pass_flag:
                stage += 1
                if self.exclude_st and row.get("is_st", 0) == 1:
                    reason, pass_flag = "st_stock", 0

            # Stage 3: New stock (list_days=0 means unknown → skip)
            if pass_flag:
                stage += 1
                ld = row.get("list_days", 9999) or 0
                if ld > 0 and ld < self.min_days_listed:
                    reason, pass_flag = "new_stock", 0

            # Stage 4: Static liquidity floor
            if pass_flag:
                stage += 1
                if row.get("avg_amount_20d", 0) < self.min_avg_amount_20d:
                    reason, pass_flag = "low_liquidity_floor", 0

            # Stage 5: Dynamic liquidity
            if pass_flag and self.use_dynamic_liquidity:
                stage += 1
                if row.get("avg_amount_20d", 0) < dynamic_threshold:
                    reason, pass_flag = "low_liquidity_dynamic", 0

            # Stage 6: BJ exchange
            if pass_flag and self.exclude_bj:
                stage += 1
                if row.get("exchange", "") == "BJ":
                    reason, pass_flag = "beijing_exchange", 0

            # Log
            records.append(
                (
                    target_date.isoformat(),
                    row.get("security_id", row.get("code", "?")),
                    pass_flag,
                    reason,
                    stage,
                    row.get("avg_amount_20d", 0),
                    row.get("total_mv", 0),
                )
            )

            if pass_flag:
                passed_codes.append(row.get("security_id", row.get("code", "?")))

        # ── Batch write filter log ────────────────────────
        self.db.executemany(
            """
            INSERT OR REPLACE INTO universe_filter_log
            (trade_date, security_id, pass_flag, reason, filter_stage, avg_amount_20d, total_mv)
            VALUES (?,?,?,?,?,?,?)
        """,
            records,
        )

        # ── Fix 5: Write universe_snapshot ────────────────
        self.db.execute(
            "DELETE FROM universe_snapshot WHERE trade_date=?", (target_date.isoformat(),)
        )
        snapshot_records = [(target_date.isoformat(), sid, "A", 0.0) for sid in passed_codes]
        self.db.executemany(
            "INSERT INTO universe_snapshot (trade_date, security_id, universe_type, score) VALUES (?,?,?,?)",
            snapshot_records,
        )

        # Update tradable_universe
        self.db.execute("DELETE FROM tradable_universe")
        for sid in passed_codes:
            self.db.execute(
                "INSERT OR REPLACE INTO tradable_universe (security_id, last_trade_date) VALUES (?,?)",
                (sid, target_date.isoformat()),
            )

        self.db.commit()

        # Return filtered DataFrame
        passed_set = set(passed_codes)
        filtered = df[df["security_id"].isin(passed_set) | df["code"].isin(passed_set)]
        return filtered.reset_index(drop=True)

    def stats(self, original: pd.DataFrame, filtered: pd.DataFrame) -> dict:
        """Return filter statistics."""
        n = len(original) if not original.empty else 0
        m = len(filtered) if not filtered.empty else 0
        return {
            "original": n,
            "filtered": m,
            "removed": n - m,
            "retention": f"{m / n * 100:.1f}%" if n > 0 else "0%",
        }

    def get_snapshot(self, trade_date: date) -> list[str]:
        """Get the list of securities that were in the universe on a given date."""
        rows = self.db.execute(
            "SELECT security_id FROM universe_snapshot WHERE trade_date=?",
            (trade_date.isoformat(),),
        ).fetchall()
        return [r[0] for r in rows]
