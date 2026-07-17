"""
Memory Retriever — Multi-dimensional embedding search for past experiences.

Commit 6-E: Searches investment_memory by factor similarity, market regime,
thesis pattern, and agent identity. Returns risk scores and failure patterns.
"""

import hashlib
import json
from collections import Counter

import numpy as np

from src.data.db import managed_connect


class MemoryRetriever:
    """Multi-dimensional memory retrieval engine.

    Embedding dimensions:
      - Factor vector (7-dim, weight 0.35)
      - Market regime one-hot (4-dim, weight 0.25)
      - Thesis pattern hash (1-dim, weight 0.25)
      - Agent identity (6-dim, weight 0.15)
    """

    FACTOR_KEYS = ['roe', 'pe_percentile', 'momentum_3m', 'volatility_60d',
                   'fcf_yield', 'revenue_growth', 'debt_to_equity']
    REGIME_MAP = {'bull': 0, 'bear': 1, 'crisis': 2, 'rotation': 3}

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)

    def search(self, thesis_pattern: str, market_regime: str,
               factor_snapshot: dict, agent_identity: dict | None = None,
               top_k: int = 10) -> dict:
        """Search investment_memory for similar past cases.

        Returns memory_context with risk_score, similar_cases, and warnings.
        Does NOT modify confidence directly.
        """
        # 1. Fetch candidates with same thesis pattern
        candidates = self.db.execute("""
            SELECT id, agent_id, research_decision_id, thesis_pattern, market_regime,
                   factor_snapshot_json, alpha, success, error_type, decay_weight
            FROM investment_memory
            WHERE thesis_pattern = ?
            ORDER BY created_at DESC
            LIMIT 300
        """, (thesis_pattern,)).fetchall()

        if not candidates:
            return {
                'similar_cases': [],
                'pattern_stats': {'total': 0, 'weighted_win_rate': 0.5, 'common_failures': []},
                'memory_risk_score': 0.5,
                'historical_failure_patterns': [],
                'memory_warning': '历史案例不足，参考价值有限',
            }

        # 2. Build current query vector
        current_vec = self._build_vector(factor_snapshot, market_regime,
                                          thesis_pattern, agent_identity)

        # 3. Score by cosine similarity × decay
        scored = []
        for row in candidates:
            mem_id, agent_id, decision_id, pattern, regime, factors_json, alpha, success, error_type, decay = row
            mem_factors = json.loads(factors_json) if isinstance(factors_json, str) else (factors_json or {})
            mem_vec = self._build_vector(mem_factors, regime or '', pattern, agent_identity)
            similarity = self._cosine_similarity(current_vec, mem_vec) * (decay or 1.0)
            scored.append({
                'id': mem_id, 'agent_id': agent_id,
                'research_decision_id': decision_id,
                'thesis_pattern': pattern, 'market_regime': regime,
                'similarity': round(similarity, 4),
                'alpha': alpha, 'success': success, 'error_type': error_type,
                'decay_weight': round(decay or 1.0, 4),
            })

        scored.sort(key=lambda x: x['similarity'], reverse=True)
        similar_cases = scored[:top_k]

        # 4. Weighted statistics
        total_weight = sum(c['decay_weight'] for c in scored)
        if total_weight > 0:
            weighted_win_rate = sum(
                (1 if (c['success'] or 0) > 0 else 0) * c['decay_weight']
                for c in scored
            ) / total_weight
        else:
            weighted_win_rate = 0.5

        # 5. Common failure patterns
        failures = [c['error_type'] for c in scored if (c['success'] or 0) <= 0 and c['error_type']]
        common_failures = Counter(failures).most_common(3)

        # 6. Memory risk score (0-1)
        if len(scored) >= 10:
            memory_risk_score = round(1.0 - weighted_win_rate, 2)
        else:
            memory_risk_score = 0.5

        # 7. Warning
        if memory_risk_score > 0.7 and len(scored) >= 10:
            memory_warning = (
                f"历史{len(scored)}次类似案例成功率仅{weighted_win_rate:.0%}，"
                f"主要失败模式: {common_failures}"
            )
        elif len(scored) < 10:
            memory_warning = f"历史案例不足（{len(scored)}次），参考价值有限"
        else:
            memory_warning = ""

        return {
            'similar_cases': similar_cases,
            'pattern_stats': {
                'total': len(scored),
                'weighted_win_rate': round(weighted_win_rate, 3),
                'common_failures': common_failures,
            },
            'memory_risk_score': memory_risk_score,
            'historical_failure_patterns': common_failures,
            'memory_warning': memory_warning,
        }

    def _build_vector(self, factors: dict, regime: str = '',
                      thesis_pattern: str = '',
                      agent_identity: dict | None = None) -> np.ndarray:
        """Build multi-dimensional embedding vector."""
        # Factor vector (7-dim × 0.35)
        fvec = np.array([float(factors.get(k, 0) or 0) for k in self.FACTOR_KEYS])
        norm = np.linalg.norm(fvec)
        fvec = fvec / (norm + 1e-9) * 0.35

        # Regime one-hot (4-dim × 0.25)
        rvec = np.zeros(4)
        if regime in self.REGIME_MAP:
            rvec[self.REGIME_MAP[regime]] = 1
        rvec = rvec * 0.25

        # Thesis hash (1-dim × 0.25)
        tval = self._stable_hash(thesis_pattern) * 0.25 if thesis_pattern else 0.125

        # Agent identity (6-dim × 0.15)
        if agent_identity:
            dims = agent_identity.get('dimensions', {})
            pvec = np.array([
                dims.get('valuation', 50), dims.get('quality', 50),
                dims.get('growth', 50), dims.get('momentum', 50),
                dims.get('contrarian', 50), dims.get('patience', 50),
            ], dtype=float) / 100.0 * 0.15
        else:
            pvec = np.zeros(6)

        return np.concatenate([fvec, rvec, [tval], pvec])

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return float(np.dot(a, b) / (na * nb + 1e-9))

    def _stable_hash(self, text: str) -> float:
        h = hashlib.md5(text.encode()).hexdigest()
        return int(h[:8], 16) % 100 / 100.0
