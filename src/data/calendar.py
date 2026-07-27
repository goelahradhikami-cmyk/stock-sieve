"""
Trading Calendar — A-share trading day lookup.

Strict DB-only: unknown dates default to non-trading days.
No weekend fallback heuristic.
"""

from datetime import date, timedelta

import pandas as pd

from src.data.db import managed_connect
from src.utils.logger import get_logger

logger = get_logger(__name__)


class TradingCalendar:
    """A-share trading calendar backed by trading_calendar table.

    Conservative principle: if a date is not in the database,
    it is NOT a trading day.
    """

    def __init__(self, db_path: str = "data/cache.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS trading_calendar (
                trade_date DATE PRIMARY KEY,
                is_trading INTEGER DEFAULT 1,
                week_of_year INTEGER,
                month INTEGER,
                quarter INTEGER
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tc_date ON trading_calendar(trade_date)")
        self.db.commit()

    def is_trade_day(self, d: date) -> bool:
        """Check if a date is a trading day. Unknown → False."""
        row = self.db.execute(
            "SELECT is_trading FROM trading_calendar WHERE trade_date=?", (d.isoformat(),)
        ).fetchone()
        return bool(row[0]) if row else False

    def previous_trade_day(self, reference_date: date, n: int = 1) -> date | None:
        """Get the nth trading day before reference_date (n=1 = most recent)."""
        row = self.db.execute(
            """
            SELECT trade_date FROM trading_calendar
            WHERE trade_date < ? AND is_trading = 1
            ORDER BY trade_date DESC
            LIMIT 1 OFFSET ?
        """,
            (reference_date.isoformat(), n - 1),
        ).fetchone()
        return date.fromisoformat(row[0]) if row else None

    def next_trade_day(self, reference_date: date, n: int = 1) -> date | None:
        """Get the nth trading day after reference_date (n=1 = next)."""
        row = self.db.execute(
            """
            SELECT trade_date FROM trading_calendar
            WHERE trade_date > ? AND is_trading = 1
            ORDER BY trade_date ASC
            LIMIT 1 OFFSET ?
        """,
            (reference_date.isoformat(), n - 1),
        ).fetchone()
        return date.fromisoformat(row[0]) if row else None

    def trade_days_between(self, start: date, end: date) -> list[date]:
        """Get all trading days in a date range (inclusive)."""
        rows = self.db.execute(
            """
            SELECT trade_date FROM trading_calendar
            WHERE trade_date BETWEEN ? AND ? AND is_trading = 1
            ORDER BY trade_date
        """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [date.fromisoformat(r[0]) for r in rows]

    def sync_from_akshare(self, start_year: int = 2010):
        """Sync trading calendar from akshare (requires: pip install akshare)."""
        try:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                records = []
                for _, row in df.iterrows():
                    d = row["trade_date"].date()
                    records.append(
                        (d.isoformat(), 1, int(d.strftime("%U")), d.month, (d.month - 1) // 3 + 1)
                    )
                self.db.executemany(
                    """INSERT OR IGNORE INTO trading_calendar
                       (trade_date, is_trading, week_of_year, month, quarter)
                       VALUES (?,?,?,?,?)""",
                    records,
                )
                self.db.commit()
                logger.info(f"✅ Trading calendar synced: {len(records)} days")
        except ImportError:
            logger.warning("calendar: akshare not installed. Run: pip install akshare")
        except Exception as e:
            logger.warning("calendar: sync failed: %s", e)

    def seed_sample(self):
        """Seed with a minimal trading calendar (weekdays) for offline use.

        This is a fallback — only use when akshare is unavailable.
        Covers 2020-2027.
        """
        from datetime import date

        start = date(2020, 1, 1)
        end = date(2027, 12, 31)
        d = start
        records = []
        while d <= end:
            if d.weekday() < 5:  # Mon-Fri
                # Rough holiday exclusion: Chinese New Year, National Day
                m, day = d.month, d.day
                skip = False
                if m == 10 and 1 <= day <= 7:
                    skip = True
                if m == 1 and 24 <= day <= 31:
                    skip = True
                if m == 2 and 1 <= day <= 7:
                    skip = True
                if not skip:
                    records.append(
                        (d.isoformat(), 1, int(d.strftime("%U")), d.month, (d.month - 1) // 3 + 1)
                    )
            d += timedelta(days=1)

        self.db.executemany(
            """INSERT OR IGNORE INTO trading_calendar
               (trade_date, is_trading, week_of_year, month, quarter)
               VALUES (?,?,?,?,?)""",
            records,
        )
        self.db.commit()
        logger.info(f"✅ Seeded {len(records)} trading days (2020-2027, weekdays only)")
