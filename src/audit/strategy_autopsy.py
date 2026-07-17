"""
Strategy Autopsy — Structured post-mortem for failed genomes.

Commit 6-J.1: Categorizes failures and derives institutional lessons.
"""


from src.data.db import managed_connect


class StrategyAutopsy:
    """Structured failure analysis for investment genomes."""

    LESSONS = {
        'overfitting': '{type} 过度拟合历史数据，需增加样本外验证',
        'crowding': '{type} 策略拥挤导致收益消失，需增加拥挤度检测',
        'regime_shift': '{type} 无法适应市场状态切换，需增加环境适应性',
        'leakage': '{type} 存在未来信息泄露，需加强数据隔离',
        'decay': '{type} Alpha衰减，因子已失效',
        'cost_erosion': '{type} 交易成本侵蚀超额收益',
    }

    PREVENTIONS = {
        'overfitting': '增加最小样本量 + 样本外验证',
        'crowding': '增加拥挤度评分 + 因子相关性检测',
        'regime_shift': '增加跨市场状态压力测试',
        'leakage': '强化数据时间戳审计',
        'decay': '增加因子IC监控 + 自动退役机制',
        'cost_erosion': '增加成本调整后的收益评估',
    }

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_autopsy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genome_id TEXT NOT NULL, failure_stage TEXT,
                failure_reason TEXT, detected_by TEXT,
                lesson TEXT, future_prevention TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.commit()

    def perform_autopsy(self, genome_id: str, genome_type: str,
                         failure_stage: str, failure_reason: str,
                         detected_by: str = 'system') -> dict:
        lesson = self._derive_lesson(failure_reason, genome_type)
        prevention = self._derive_prevention(failure_reason, genome_type)

        self.db.execute("""
            INSERT INTO strategy_autopsy
            (genome_id, failure_stage, failure_reason, detected_by, lesson, future_prevention)
            VALUES (?,?,?,?,?,?)
        """, (genome_id, failure_stage, failure_reason, detected_by, lesson, prevention))
        self.db.commit()

        return {'genome_id': genome_id, 'lesson': lesson, 'prevention': prevention}

    def _derive_lesson(self, reason: str, genome_type: str) -> str:
        template = self.LESSONS.get(reason, '{type} 因 {reason} 失败')
        return template.replace('{type}', genome_type).replace('{reason}', reason)

    def _derive_prevention(self, reason: str, genome_type: str) -> str:
        return self.PREVENTIONS.get(reason, '通用审查加强')

    def get_recent_autopsies(self, limit: int = 10) -> list:
        rows = self.db.execute(
            "SELECT * FROM strategy_autopsy ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        cols = [d[1] for d in self.db.execute("PRAGMA table_info(strategy_autopsy)")]
        return [dict(zip(cols, r)) for r in rows]
