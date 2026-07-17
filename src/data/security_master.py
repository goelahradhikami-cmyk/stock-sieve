"""
Security Master — A-share stock identity CRUD.

Phase: Commit 1 — Stock Identity Infrastructure
"""

import sqlite3
from src.data.db import managed_connect
from datetime import date
from typing import List, Optional, Dict
import pandas as pd


class SecurityMaster:
    """Manage the security_master table — stock universe identity."""

    def __init__(self, db_path: str = "data/cache.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS security_master (
                security_id     TEXT PRIMARY KEY,
                code            TEXT NOT NULL,
                exchange        TEXT NOT NULL,
                name            TEXT NOT NULL,
                ipo_date        DATE,
                list_days       INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'active',
                industry        TEXT,
                industry_index  TEXT,
                total_mv        REAL,
                float_mv        REAL,
                avg_turnover_20d REAL,
                avg_amount_20d  REAL,
                is_st           INTEGER DEFAULT 0,
                is_new_stock    INTEGER DEFAULT 0,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sm_code ON security_master(code)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sm_industry ON security_master(industry)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sm_status ON security_master(status)")
        self.db.commit()

    def upsert(self, records: List[Dict]):
        """Batch insert or update stock records."""
        sql = """
            INSERT OR REPLACE INTO security_master
            (security_id, code, exchange, name, ipo_date, list_days,
             status, industry, industry_index, total_mv, float_mv,
             avg_turnover_20d, avg_amount_20d, is_st, is_new_stock, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
        """
        data = []
        for r in records:
            data.append((
                r.get('security_id'),
                r.get('code'),
                r.get('exchange'),
                r.get('name'),
                r.get('ipo_date'),
                r.get('list_days', 0),
                r.get('status', 'active'),
                r.get('industry'),
                r.get('industry_index'),
                r.get('total_mv'),
                r.get('float_mv'),
                r.get('avg_turnover_20d', 0.0),
                r.get('avg_amount_20d', 0.0),
                r.get('is_st', 0),
                r.get('is_new_stock', 0)
            ))
        self.db.executemany(sql, data)
        self.db.commit()

    def get_active_universe(self) -> pd.DataFrame:
        """Return active, non-ST stock list (raw, no extra filtering)."""
        return pd.read_sql_query(
            "SELECT * FROM security_master WHERE status='active' AND is_st=0",
            self.db
        )

    def get_by_code(self, code: str) -> Optional[Dict]:
        """Look up a single stock by numeric code."""
        df = pd.read_sql_query(
            "SELECT * FROM security_master WHERE code=?",
            self.db, params=(code,)
        )
        return df.iloc[0].to_dict() if not df.empty else None

    def count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM security_master"
        ).fetchone()[0]
