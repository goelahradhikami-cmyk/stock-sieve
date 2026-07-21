"""
Candidate Generator v3 - Commit 6-S.13.

Replaces MarketAnomalyDetector.scan() as the candidate source for the
Security Analyst layer. Implements a three-stage funnel that reverses
the v1/v2 logic:

  v1/v2: Universe -> Anomaly (entrance) -> Ranking
  v3:    Universe -> Recovery Eligibility -> Relative Strength -> Mispricing

The reversal is the core architectural change. v2 proved the problem is
candidate generation, not scoring: many stocks are 'deserved cheapness'
(earnings deterioration + industry decline), not mispricing. v3 puts
recovery eligibility FIRST and mispricing LAST.

Three stages:
  Stage 1 (6-S.13.2): Recovery Eligibility Gate
    - Liquidity gate (volume_ratio > 0.3 from snapshot)
    - FRM hard gate (reject 'deteriorating' earnings direction)
    - Recovery score (FRM-weighted composite)
  Stage 2 (6-S.13.3): Relative Strength Confirmation
    - Stock must outperform sector (rs_vs_sector > 0)
    - Reuses SectorConfirmationScorer (6-S.12.3)
  Stage 3 (6-S.13.4): Mispricing Detection
    - Reuses MarketAnomalyDetector._detect_anomaly as LAST gate
    - divergence_score used for final ranking

Output: list[MispricingObject] with v3_features populated.
KillCriteria and DoctrineUnderwriter consume the core fields only;
v3_features are advisory.

Usage:
    from src.thesis.candidate_generator import CandidateGenerator
    gen = CandidateGenerator()
    candidates = gen.generate("2024-08-29", "EARLY_RECOVERY")
"""

from __future__ import annotations

import os
import sys
import sqlite3
import json
from dataclasses import dataclass
from typing import Optional

from src.thesis.market_anomaly import (
    MispricingObject, V3CandidateFeatures, MarketAnomalyDetector
)
from src.thesis.fundamental_recovery import FundamentalRecoveryScorer
from src.thesis.sector_confirmation import SectorConfirmationScorer
from src.thesis.expectation_gap import ExpectationGapEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Stage 1 thresholds (6-S.13.1 Design Freeze)
LIQUIDITY_MIN_VOLUME_RATIO = 0.3    # reject extreme illiquidity
FRM_HARD_REJECT_DIRECTION = "deteriorating"

# Stage 2 threshold (6-S.13.1 Design Freeze)
RS_HARD_GATE_THRESHOLD = 0.0        # rs_vs_sector must be > 0


class CandidateGenerator:
    """6-S.13: v3 Candidate Generator - three-stage funnel.

    Replaces MarketAnomalyDetector.scan() as the candidate source.
    Returns list[MispricingObject] with v3_features populated.

    Universe contract (FROZEN): stock_factor_snapshot, NOT UniverseFilter.
    This avoids historical leakage from avg_amount_20d (which stores
    current values, not point-in-time).
    """

    def __init__(self, cache_db: str = "data/cache.db",
                 eval_db: str = "data/evaluation.db",
                 shadow_db: str = "data/shadow_trading.db"):
        self.cache_db = cache_db
        self.eval_db = eval_db
        self.shadow_db = shadow_db

        # Reuse existing scorers (6-S.12 components)
        self.frm_scorer = FundamentalRecoveryScorer(cache_db)
        self.rs_scorer = SectorConfirmationScorer(cache_db)
        self.anomaly_detector = MarketAnomalyDetector(
            cache_db=cache_db, eval_db=eval_db)
        # v3.3: EGE replaces RS gate (6-S.15.2)
        self.ege_engine = ExpectationGapEngine(cache_db)

        # Funnel log buffer (batched writes)
        self._funnel_log_buffer: list[tuple] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, trade_date: str, market_state: str = "unknown",
                 universe: list[str] | None = None,
                 top_n: int = 50,
                 episode_id: str | None = None) -> list[MispricingObject]:
        """Run the three-stage funnel.

        Args:
            trade_date: ISO date
            market_state: PANIC/STABILIZING/EARLY_RECOVERY/
                          CONFIRMED_RECOVERY/EUPHORIA/unknown
            universe: stock codes; None -> snapshot universe
            top_n: max candidates to return
            episode_id: if provided, log funnel entries to shadow_funnel_log

        Returns: list[MispricingObject] with v3_features, sorted by
                 divergence_score descending.
        """
        # Stage 0: Universe (frozen contract: stock_factor_snapshot)
        if universe is None:
            universe = self._get_snapshot_universe(trade_date)
        if not universe:
            logger.warning("candidate_generator: empty universe for %s", trade_date)
            return []

        # Stage 1: Recovery Eligibility
        stage1_passed = self._stage1_recovery_eligibility(
            universe, trade_date, market_state, episode_id)

        # Stage 2: Relative Strength
        stage2_passed = self._stage2_relative_strength(
            stage1_passed, trade_date, episode_id)

        # Stage 3: Mispricing (last gate)
        candidates = self._stage3_mispricing(
            stage2_passed, trade_date, top_n, episode_id)

        # Flush funnel log
        if episode_id and self._funnel_log_buffer:
            self._flush_funnel_log()

        return candidates

    # ------------------------------------------------------------------
    # Stage 0: Universe (frozen contract)
    # ------------------------------------------------------------------

    def _get_snapshot_universe(self, trade_date: str) -> list[str]:
        """Universe from stock_factor_snapshot (FROZEN - not UniverseFilter).

        Rationale: UniverseFilter uses security_master.avg_amount_20d which
        stores current values, not point-in-time. Using it for historical
        replay would leak future information. stock_factor_snapshot is
        date-stamped and point-in-time correct.
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT DISTINCT security_id FROM stock_factor_snapshot "
                "WHERE trade_date = ?",
                (trade_date,),
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows if r[0]]

    # ------------------------------------------------------------------
    # Stage 1: Recovery Eligibility Gate (6-S.13.2)
    # ------------------------------------------------------------------

    def _stage1_recovery_eligibility(
        self, universe: list[str], trade_date: str,
        market_state: str, episode_id: str | None
    ) -> list[tuple[str, V3CandidateFeatures]]:
        """Filter: does the stock have the right to participate in recovery?

        Three gates:
          1a. Liquidity (volume_ratio > 0.3)
          1b. FRM direction (hard reject 'deteriorating')
          1c. Recovery score (composite, not a hard gate)
        """
        passed = []
        # Batch-load volume_ratio for the whole universe (one query)
        vol_ratios = self._batch_get_volume_ratio(universe, trade_date)

        for code in universe:
            features = V3CandidateFeatures()
            vol_ratio = vol_ratios.get(code)
            features.liquidity_pass = (
                vol_ratio is not None
                and vol_ratio > LIQUIDITY_MIN_VOLUME_RATIO
            )

            # Gate 1a: Liquidity
            if not features.liquidity_pass:
                self._buffer_funnel_log(
                    episode_id, trade_date, code,
                    stage1_pass=0,
                    stage1_liquidity_pass=0,
                    stage1_volume_ratio=vol_ratio,
                    rejection_stage='stage1',
                    rejection_reason='LOW_LIQUIDITY',
                )
                continue

            # Gate 1b: FRM (Fundamental Recovery Momentum, 6-S.12.2)
            frm = self.frm_scorer.compute(code, trade_date, market_state)
            features.frm_direction = frm.revision_direction
            features.frm_score = frm.score
            features.earnings_acceleration = (
                frm.earnings_yoy_current - frm.earnings_yoy_previous
                if (frm.earnings_yoy_current is not None
                    and frm.earnings_yoy_previous is not None)
                else None
            )

            # HARD GATE: deteriorating earnings -> reject
            if frm.revision_direction == FRM_HARD_REJECT_DIRECTION:
                self._buffer_funnel_log(
                    episode_id, trade_date, code,
                    stage1_pass=0,
                    stage1_liquidity_pass=1,
                    stage1_volume_ratio=vol_ratio,
                    stage1_frm_direction=frm.revision_direction,
                    stage1_frm_score=frm.score,
                    stage1_earnings_accel=features.earnings_acceleration,
                    rejection_stage='stage1',
                    rejection_reason='DETERIORATING',
                )
                continue

            # Gate 1c: Recovery score (composite, not a hard gate)
            features.recovery_score = self._compute_recovery_score(frm)
            features.candidate_stage = 'stage1_pass'
            passed.append((code, features))

            self._buffer_funnel_log(
                episode_id, trade_date, code,
                stage1_pass=1,
                stage1_liquidity_pass=1,
                stage1_volume_ratio=vol_ratio,
                stage1_frm_direction=frm.revision_direction,
                stage1_frm_score=frm.score,
                stage1_earnings_accel=features.earnings_acceleration,
                stage1_recovery_score=features.recovery_score,
            )

        return passed

    def _compute_recovery_score(self, frm_result) -> float:
        """Recovery score composite (6-S.13.1 Design Freeze formula).

        recovery_score = 0.35 * earnings_acceleration
                       + 0.25 * margin_stabilization
                       + 0.20 * sector_strength   (placeholder, filled in Stage 2)
                       + 0.20 * relative_strength (placeholder, filled in Stage 2)

        In Stage 1, only FRM subscores are available. The sector/RS
        components are filled in Stage 2. For the Stage 1 pass/fail
        decision, recovery_score is advisory (the hard gate is FRM
        direction). The score becomes meaningful after Stage 2 enriches it.
        """
        # Stage 1 version: weight FRM subscores at 0.60 (0.35+0.25),
        # leave 0.40 for Stage 2 components (filled later).
        frm_component = (
            0.35 * frm_result.earnings_acceleration
            + 0.25 * frm_result.margin_stabilization
        )
        # Rescale to 0-100 (frm subscores are 0-100, weights sum to 0.60)
        # Then add 0.40 * 50 (neutral) as placeholder for Stage 2
        stage1_score = frm_component + 0.40 * 50.0
        return float(min(100.0, max(0.0, stage1_score)))

    # ------------------------------------------------------------------
    # Stage 2: Relative Strength Confirmation (6-S.13.3)
    # ------------------------------------------------------------------

    def _stage2_relative_strength(
        self, stage1_passed: list[tuple[str, V3CandidateFeatures]],
        trade_date: str, episode_id: str | None
    ) -> list[tuple[str, V3CandidateFeatures]]:
        """Filter: is the stock leading, not just riding sector beta?

        Hard gate: rs_vs_sector > 0 (when sector data available).
        Pre-2024-06: sector data unavailable -> soft gate (skip, log).
        """
        passed = []
        for code, features in stage1_passed:
            rs = self.rs_scorer.compute(code, trade_date)

            # RS retained as DIAGNOSTIC field only (v3.2.2 proved RS gate
            # destroys alpha). No longer a hard gate.
            features.relative_strength = rs.rs_vs_sector
            features.sector_strength = rs.sector_vs_market
            features.rs_score = rs.score

            # v3.3: EGE scoring (replaces RS gate, 6-S.15.2)
            # EGE does NOT gate - it scores for ranking.
            ege_score = self.ege_engine.compute(code, trade_date)
            # Store EGE in recovery_score field (repurposed for v3.3)
            if ege_score.gap_score is not None:
                # Use gap_score as the Stage 2 ranking signal
                features.recovery_score = ege_score.gap_score * 50 + 50  # map z-score to 0-100
            else:
                features.recovery_score = 50.0  # neutral when EGE data unavailable

            # NO HARD GATE in Stage 2 (v3.3 design freeze)
            # All Stage 1 survivors pass through; EGE score used for ranking only
            features.candidate_stage = 'stage2_pass'
            passed.append((code, features))

            self._update_funnel_log(
                episode_id, trade_date, code,
                stage2_rs_vs_sector=rs.rs_vs_sector,
                stage2_sector_vs_market=rs.sector_vs_market,
                stage2_rs_score=rs.score,
                stage2_data_available=1 if rs.data_available else 0,
                stage2_pass=1,  # always pass (no gate)
            )

        return passed

    def _enrich_recovery_score(self, features: V3CandidateFeatures,
                                rs_result) -> float:
        """Fill in Stage 2 components of recovery_score.

        Full formula (6-S.13.1):
          0.35 * earnings_acceleration_score
          0.25 * margin_stabilization_score
          0.20 * sector_strength (normalized)
          0.20 * relative_strength (normalized)

        Stage 2 adds the last two. rs_result.score is 0-100 composite.
        We use rs_result.sector_vs_market for sector_strength mapping
        and rs_result.rs_vs_sector for relative_strength mapping.
        """
        # The frm_score already encodes earnings + margin subscores.
        # Extract them via frm_scorer's subscore scale (0-100).
        # For simplicity, use frm_score * 0.60 as the FRM component,
        # then add RS components * 0.40.
        frm_component = (features.frm_score or 50.0) * 0.60

        # RS component: use rs_result.score (0-100) for both sector + RS
        # (the score already blends relative_strength + sector_strength)
        rs_component = (rs_result.score or 50.0) * 0.40

        return float(min(100.0, max(0.0, frm_component + rs_component)))

    # ------------------------------------------------------------------
    # Stage 3: Mispricing Detection (6-S.13.4) - last gate
    # ------------------------------------------------------------------

    def _stage3_mispricing(
        self, stage2_passed: list[tuple[str, V3CandidateFeatures]],
        trade_date: str, top_n: int, episode_id: str | None
    ) -> list[MispricingObject]:
        """Detect mispricing among stocks that passed recovery + RS gates.

        Reuses MarketAnomalyDetector._detect_anomaly as the LAST gate.
        This is the key reversal: anomaly is no longer the entrance.
        """
        candidates = []
        for rank, (code, features) in enumerate(stage2_passed, 1):
            # Reuse existing anomaly detection (not rewritten)
            anomaly = self.anomaly_detector._detect_anomaly(code, trade_date)
            if anomaly is None:
                self._update_funnel_log(
                    episode_id, trade_date, code,
                    stage3_divergence_score=None,
                    stage3_pass=0,
                    rejection_stage='stage3',
                    rejection_reason='NO_MISPRICING',
                )
                continue

            # Attach v3 features
            anomaly.v3_features = features
            features.candidate_stage = 'stage3_pass'

            self._update_funnel_log(
                episode_id, trade_date, code,
                stage3_divergence_score=anomaly.divergence_score,
                stage3_pass=1,
                final_pass=1,
            )
            candidates.append(anomaly)

        # Rank by divergence_score descending (retained from v1/v2)
        candidates.sort(key=lambda a: -(a.divergence_score or 0))
        # Assign funnel rank
        for rank, c in enumerate(candidates[:top_n], 1):
            if c.v3_features:
                pass  # rank tracked implicitly by position
        return candidates[:top_n]

    # ------------------------------------------------------------------
    # Volume ratio batch loading
    # ------------------------------------------------------------------

    def _batch_get_volume_ratio(self, universe: list[str],
                                 trade_date: str) -> dict[str, float]:
        """Batch-load volume_ratio from stock_factor_snapshot.

        Returns dict {code: volume_ratio}. Avoids N single queries.
        """
        if not universe:
            return {}
        conn = sqlite3.connect(self.eval_db)
        result = {}
        try:
            # Batch in chunks of 500 (SQLite parameter limit)
            for i in range(0, len(universe), 500):
                chunk = universe[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT security_id, factor_values_json "
                    f"FROM stock_factor_snapshot "
                    f"WHERE trade_date = ? AND security_id IN ({placeholders})",
                    [trade_date] + chunk,
                ).fetchall()
                for sec_id, fj_str in rows:
                    if fj_str:
                        try:
                            fj = json.loads(fj_str)
                            vr = fj.get("volume_ratio")
                            if vr is not None:
                                result[sec_id] = float(vr)
                        except (json.JSONDecodeError, TypeError):
                            pass
        finally:
            conn.close()
        return result

    # ------------------------------------------------------------------
    # Funnel log (batched writes to shadow_funnel_log)
    # ------------------------------------------------------------------

    def _buffer_funnel_log(self, episode_id, trade_date, code, **fields):
        """Buffer a funnel log entry for batch insert."""
        if not episode_id:
            return
        self._funnel_log_buffer.append({
            "episode_id": episode_id,
            "trade_date": trade_date,
            "stock_code": code,
            **fields,
        })

    def _update_funnel_log(self, episode_id, trade_date, code, **fields):
        """Update an existing funnel log entry (matched by episode+code).

        Stage 2/3 add fields to entries created in Stage 1.
        """
        if not episode_id:
            return
        for entry in self._funnel_log_buffer:
            if (entry["episode_id"] == episode_id
                    and entry["stock_code"] == code):
                entry.update(fields)
                return
        # If not found (shouldn't happen), create new
        self._buffer_funnel_log(episode_id, trade_date, code, **fields)

    def _flush_funnel_log(self):
        """Batch-insert funnel log entries to shadow_funnel_log."""
        if not self._funnel_log_buffer:
            return
        conn = sqlite3.connect(self.shadow_db)
        try:
            for entry in self._funnel_log_buffer:
                conn.execute(
                    """INSERT INTO shadow_funnel_log
                       (episode_id, trade_date, stock_code,
                        stage1_liquidity_pass, stage1_volume_ratio,
                        stage1_frm_direction, stage1_frm_score,
                        stage1_earnings_accel, stage1_recovery_score,
                        stage1_pass,
                        stage2_rs_vs_sector, stage2_sector_vs_market,
                        stage2_rs_score, stage2_data_available, stage2_pass,
                        stage3_divergence_score, stage3_pass,
                        final_pass, rejection_stage, rejection_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        entry.get("episode_id"),
                        entry.get("trade_date"),
                        entry.get("stock_code"),
                        entry.get("stage1_liquidity_pass"),
                        entry.get("stage1_volume_ratio"),
                        entry.get("stage1_frm_direction"),
                        entry.get("stage1_frm_score"),
                        entry.get("stage1_earnings_accel"),
                        entry.get("stage1_recovery_score"),
                        entry.get("stage1_pass"),
                        entry.get("stage2_rs_vs_sector"),
                        entry.get("stage2_sector_vs_market"),
                        entry.get("stage2_rs_score"),
                        entry.get("stage2_data_available"),
                        entry.get("stage2_pass"),
                        entry.get("stage3_divergence_score"),
                        entry.get("stage3_pass"),
                        entry.get("final_pass"),
                        entry.get("rejection_stage"),
                        entry.get("rejection_reason"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        self._funnel_log_buffer.clear()
