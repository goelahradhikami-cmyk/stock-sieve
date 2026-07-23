"""Non-destructive end-to-end proof: with warm baostock cache (env=1),
compute factors for representative stocks and insert research_decisions under a
FUTURE entry_date (so decision_hash is unique -> bypasses idempotency), then
verify the stored growth_score is non-50. Does NOT touch today's rows.
"""

import ast
import hashlib
import os
import sys
import time

sys.path.insert(0, ".")
os.environ["STOCK_SIEVE_USE_BAOSTOCK"] = "1"

from src.data.evaluation_db import EvaluationDB
from src.data.financial_provider import get_financial_provider
from src.data.local_provider import LocalDataProvider
from src.factors.engine import FactorEngine

PROBE_DATE = "2026-07-18"  # future date -> unique decision_hash, non-destructive
CODES = ["600000", "600519", "600004", "600006", "600036"]

prov = get_financial_provider()
lp = LocalDataProvider()
fe = FactorEngine()
db = EvaluationDB()

inserted = []
for code in CODES:
    fin = prov.get_financial_dict(code)  # warm cache -> no network
    price = lp.get_daily_kline(code)
    res = fe.compute_single_stock(code, fin, price)
    fs = {
        "quality": res.quality_score,
        "value": res.value_score,
        "growth": res.growth_score,
        "momentum": res.momentum_score,
    }
    dh = hashlib.sha256(
        f"probe_agent|{code}|{PROBE_DATE}|{int(time.time() * 1000)}".encode()
    ).hexdigest()[:16]
    ih = hashlib.sha256(f"{code}|{PROBE_DATE}".encode()).hexdigest()[:16]
    rid = db.insert_research_decision(
        agent_id="probe_agent",
        genome_hash="probe",
        security_id=code,
        thesis={
            "thesis_id": f"auto_{code}_{PROBE_DATE}",
            "family": "value",
            "pattern": "probe",
            "claim": "probe",
            "evidence": [],
            "invalidation": [],
            "horizon": "12_months",
        },
        alpha_score=50,
        confidence=0.5,
        factor_snapshot=fs,
        risk_assessment={},
        entry_price=100,
        entry_date=PROBE_DATE,
        decision_hash=dh,
        input_hash=ih,
    )
    inserted.append((rid, code, fs["growth"]))

# verify what got stored
conn = db.connect()
print(f"{'id':>5} {'code':>8} {'stored_growth':>14}  (neutral=50.0)")
all_non50 = True
for rid, code, g_exp in inserted:
    row = conn.execute(
        "SELECT factor_snapshot FROM research_decisions WHERE id=?", (rid,)
    ).fetchone()
    d = ast.literal_eval(row[0]) if row and row[0] else {}
    g = d.get("growth")
    non50 = g is not None and g != 50.0
    all_non50 &= non50
    print(f"{rid:>5} {code:>8} {g:>14.2f}  {'NON-50 ✓' if non50 else '50 ✗'}")
# cleanup probe rows (keep DB clean)
for rid, _, _ in inserted:
    conn.execute("DELETE FROM research_decisions WHERE id=?", (rid,))
conn.commit()
conn.close()

print(
    "\nRESULT:",
    "PASS — baostock multi-period growth flows into stored decisions (non-50)"
    if all_non50
    else "FAIL — growth still neutral",
)
