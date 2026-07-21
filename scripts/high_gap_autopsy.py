"""High Gap Autopsy - 4 analyses (one-shot script)."""
import sys, sqlite3
import numpy as np
from collections import defaultdict
sys.path.insert(0, '.')
from src.thesis.expectation_gap import ExpectationGapEngine

engine = ExpectationGapEngine()
conn = sqlite3.connect('data/shadow_trading.db')
conn.row_factory = sqlite3.Row
cache = sqlite3.connect('data/cache.db')

rows = conn.execute("""
    SELECT v.*, e.market_state
    FROM shadow_candidates_v3 v
    JOIN shadow_episode e ON e.episode_id = v.episode_id
    WHERE v.residual_alpha IS NOT NULL ORDER BY v.id
""").fetchall()
print(f"Candidates: {len(rows)}")

enriched = []
for r in rows:
    score = engine.compute(r["security_id"], r["trade_date"])
    if score.gap_score is not None:
        enriched.append({
            "code": r["security_id"], "gap": score.gap_score,
            "ea": score.earnings_acceleration, "pr": score.price_reaction,
            "residual": r["residual_alpha"], "market_beta": r["market_beta"],
            "sector_beta": r["sector_beta"], "rs": r["relative_strength"],
            "frm_score": r["frm_score"], "frm_direction": r["frm_direction"],
            "trade_date": r["trade_date"], "available_date": score.available_date,
        })
print(f"With gap_score: {len(enriched)}")

# ========== Analysis 1: Extreme Value Pollution ==========
print("\n" + "=" * 60)
print("Analysis 1: Extreme Value Pollution Check")
print("=" * 60)
gaps = sorted([e["gap"] for e in enriched])
print("Gap distribution:")
for p in [10, 25, 50, 75, 90, 95, 99]:
    idx = int(len(gaps) * p / 100)
    print(f"  P{p}: {gaps[min(idx, len(gaps)-1)]:+.4f}")
print(f"  Max: {gaps[-1]:+.4f}")

d10 = sorted(enriched, key=lambda x: -x["gap"])[:len(enriched)//10]
d10_all = np.array([e["residual"] for e in d10])
p99 = np.percentile([x["gap"] for x in enriched], 99)
d10_no_top1 = np.array([e["residual"] for e in d10 if e["gap"] < p99])
print(f"\nD10 (all):         N={len(d10_all)} mean={np.mean(d10_all):+.4f}")
if len(d10_no_top1) > 0:
    print(f"D10 (excl top 1%): N={len(d10_no_top1)} mean={np.mean(d10_no_top1):+.4f}")
    delta = np.mean(d10_all) - np.mean(d10_no_top1)
    print(f"Delta from top 1%: {delta:+.4f}")
    if abs(delta) > 0.02:
        print("  WARNING: top 1% outliers significantly affect D10")
    else:
        print("  OK: top 1% outliers do NOT dominate D10")

extreme = sorted(enriched, key=lambda x: -x["gap"])[:5]
print(f"\nTop 5 extreme gap stocks:")
for e in extreme:
    print(f"  {e['code']} gap={e['gap']:+.2f} ea={e['ea']:+.4f} "
          f"pr={e['pr']:+.4f} residual={e['residual']:+.4f}")

# ========== Analysis 2: Inverted-U Validation ==========
print("\n" + "=" * 60)
print("Analysis 2: Inverted-U Shape Validation (Quintiles)")
print("=" * 60)
enriched.sort(key=lambda x: x["gap"])
n = len(enriched)
q_size = n // 5
print(f"\n{'Q':>4s} {'gap_range':>25s} {'N':>4s} {'residual':>12s} {'positive':>10s}")
for q in range(5):
    start = q * q_size
    end = (q+1) * q_size if q < 4 else n
    subset = enriched[start:end]
    ra = np.array([e["residual"] for e in subset])
    gv = [e["gap"] for e in subset]
    print(f"Q{q+1:1d} [{min(gv):+8.3f},{max(gv):+8.3f}] {len(subset):4d} "
          f"{np.mean(ra):+12.4f} {np.mean(ra>0):10.1%}")

q1_ra = np.array([e["residual"] for e in enriched[:q_size]])
q3_ra = np.array([e["residual"] for e in enriched[2*q_size:3*q_size]])
q5_ra = np.array([e["residual"] for e in enriched[4*q_size:]])
print(f"\nInverted-U test: Q3 > Q1 and Q3 > Q5?")
print(f"  Q1={np.mean(q1_ra):+.4f} Q3={np.mean(q3_ra):+.4f} Q5={np.mean(q5_ra):+.4f}")
if np.mean(q3_ra) > np.mean(q1_ra) and np.mean(q3_ra) > np.mean(q5_ra):
    print("  PASS: Inverted-U SUPPORTED")
else:
    print("  FAIL: Not inverted-U")

# ========== Analysis 3: FRM x Gap 2D Matrix ==========
print("\n" + "=" * 60)
print("Analysis 3: FRM x Gap 2D Matrix")
print("=" * 60)
frm_vals = sorted([e["frm_score"] for e in enriched if e["frm_score"] is not None])
frm_t1 = frm_vals[len(frm_vals)//3]
frm_t2 = frm_vals[2*len(frm_vals)//3]
gap_vals = sorted([e["gap"] for e in enriched])
gap_t1 = gap_vals[len(gap_vals)//3]
gap_t2 = gap_vals[2*len(gap_vals)//3]

def fb(frm):
    if frm is None: return "unknown"
    if frm < frm_t1: return "weak"
    if frm < frm_t2: return "medium"
    return "strong"

def gb(gap):
    if gap < gap_t1: return "low"
    if gap < gap_t2: return "mid"
    return "high"

matrix = defaultdict(lambda: {"n": 0, "residuals": []})
for e in enriched:
    matrix[f"{fb(e['frm_score'])}_{gb(e['gap'])}"]["n"] += 1
    matrix[f"{fb(e['frm_score'])}_{gb(e['gap'])}"]["residuals"].append(e["residual"])

print(f"\nFRM: weak<{frm_t1:.1f}, mid<{frm_t2:.1f}, strong>={frm_t2:.1f}")
print(f"Gap: low<{gap_t1:.3f}, mid<{gap_t2:.3f}, high>={gap_t2:.3f}")
print(f"\nResidual Alpha Matrix:")
print(f"  {'':8s} {'low_gap':>16s} {'mid_gap':>16s} {'high_gap':>16s}")
for f in ["weak", "medium", "strong"]:
    row = f"  {f:8s}"
    for g in ["low", "mid", "high"]:
        cell = matrix[f"{f}_{g}"]
        if cell["n"] >= 3:
            m = np.mean(cell["residuals"])
            pr = np.mean(np.array(cell["residuals"]) > 0)
            row += f" {m:+8.4f}(N={cell['n']:2d},{pr:.0%})"
        else:
            row += f" {'--':>16s}"
    print(row)

best = None
best_m = -999
for k, c in matrix.items():
    if c["n"] >= 5:
        m = np.mean(c["residuals"])
        if m > best_m:
            best_m = m
            best = k
if best:
    print(f"\n  BEST CELL: {best} residual={best_m:+.4f} N={matrix[best]['n']}")

# ========== Analysis 4: Earnings Quality ==========
print("\n" + "=" * 60)
print("Analysis 4: Earnings Quality (revenue vs earnings divergence)")
print("=" * 60)
high_gap = sorted(enriched, key=lambda x: -x["gap"])[:30]
low_gap = sorted(enriched, key=lambda x: x["gap"])[:30]

print(f"\n{'':20s} {'high_gap':>14s} {'low_gap':>14s}")
high_ea = np.array([e["ea"] for e in high_gap if e["ea"] is not None])
low_ea = np.array([e["ea"] for e in low_gap if e["ea"] is not None])
print(f"{'earnings_accel':>20s} {np.mean(high_ea):+14.4f} {np.mean(low_ea):+14.4f}")
high_pr = np.array([e["pr"] for e in high_gap if e["pr"] is not None])
low_pr = np.array([e["pr"] for e in low_gap if e["pr"] is not None])
print(f"{'price_reaction':>20s} {np.mean(high_pr):+14.4f} {np.mean(low_pr):+14.4f}")
high_res = np.array([e["residual"] for e in high_gap])
low_res = np.array([e["residual"] for e in low_gap])
print(f"{'residual_alpha':>20s} {np.mean(high_res):+14.4f} {np.mean(low_res):+14.4f}")

print(f"\nEarnings quality (revenue vs earnings acceleration):")
for label, group in [("high_gap", high_gap[:15]), ("low_gap", low_gap[:15])]:
    real = 0; suspect = 0; checked = 0
    for e in group:
        if e["available_date"]:
            fin = cache.execute(
                "SELECT earnings_yoy, revenue_yoy FROM akshare_financials "
                "WHERE code=? AND available_date<=? ORDER BY report_date DESC LIMIT 2",
                (e["code"], e["available_date"])).fetchall()
            if len(fin) >= 2:
                ea_accel = (fin[0][0] or 0) - (fin[1][0] or 0)
                rev_accel = (fin[0][1] or 0) - (fin[1][1] or 0)
                checked += 1
                if ea_accel > 0 and rev_accel > 0:
                    real += 1
                elif ea_accel > 0 and rev_accel <= 0:
                    suspect += 1
    print(f"  {label}: checked={checked} real_improvement={real} accounting_suspect={suspect}")

conn.close()
cache.close()
