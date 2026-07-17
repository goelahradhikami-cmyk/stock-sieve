"""Investment Committee 数据库迁移运行器 (Phase 5A-3)。

将 evaluation.db 升级到委员会所需 schema：
  init_db()                         → v2.0 基础表
  migrate_v2_1()                    → v2.1 表（含 committee_decisions 初版）
  migrate_committee_decisions_v2_1_1() → v2.1.1 补齐 spec §4.1 字段

全部幂等，可反复运行。

用法:
  python migrations/committee_migration.py [db_path]
默认 db_path = data/evaluation.db
"""

import os
import sys

# 将项目根目录加入 sys.path，便于以脚本方式直接运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data.evaluation_db import EvaluationDB  # noqa: E402


def run_migration(db_path: str = "data/evaluation.db") -> None:
    print(f"[committee_migration] 目标数据库: {db_path}")
    db = EvaluationDB(db_path)
    db.init_db()
    print("  ✓ v2.0 基础表就绪")
    db.migrate_v2_1()
    print("  ✓ v2.1 表就绪（含 committee_decisions 初版）")
    db.migrate_committee_decisions_v2_1_1()
    print("  ✓ v2.1.1 committee_decisions 字段补齐")

    # 验证列存在
    conn = db.connect()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(committee_decisions)")]
    conn.close()
    required = {
        "devil_advocate_score", "weighted_score", "verdict_reason",
        "position_cap_modifier", "confidence_modifier",
        "required_conditions_json", "member_statements_json",
        "devil_advocate_attack_points_json",
    }
    missing = required - set(cols)
    if missing:
        raise RuntimeError(f"迁移后缺失列: {missing}")
    print(f"  ✓ committee_decisions 列齐备（共 {len(cols)} 列）")
    print("[committee_migration] 迁移完成。")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data/evaluation.db"
    run_migration(target)
