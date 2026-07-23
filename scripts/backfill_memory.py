"""
一次性回填所有已完成评估到 investment_memory 表。
运行: python scripts/backfill_memory.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.memory.extractor import ExperienceExtractor

ext = ExperienceExtractor()
count = ext.extract_daily(limit=10000)

print(f"回填完成: {count} 条记忆")

# Show distribution
from src.data.evaluation_db import EvaluationDB

conn = EvaluationDB().connect()
total = conn.execute("SELECT COUNT(*) FROM investment_memory").fetchone()[0]
print(f"记忆库总数: {total}")

for label, cond in [
    ("优秀", "success=2"),
    ("成功", "success=1"),
    ("中性", "success=0"),
    ("失败", "success=-1"),
]:
    n = conn.execute(f"SELECT COUNT(*) FROM investment_memory WHERE {cond}").fetchone()[0]
    print(f"  {label}: {n}")

print()
print("按 Thesis 模式统计:")
for row in conn.execute("""
    SELECT thesis_pattern, COUNT(*) as cnt,
           ROUND(AVG(CASE WHEN success>0 THEN 1.0 ELSE 0.0 END)*100,1) as win_pct,
           ROUND(AVG(alpha)*100,2) as avg_alpha_pct
    FROM investment_memory GROUP BY thesis_pattern ORDER BY cnt DESC LIMIT 10
""").fetchall():
    print(f"  {row[0]:25s} {row[1]:4d}条  胜率={row[2]:.1f}%  α={row[3]:+.2f}%")
conn.close()
