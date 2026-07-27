"""
Doctrine Engine - Investment Personality Expression Engine (Commit 6-L.1).

Transforms an Identity Genome into a Doctrine Genome - a complete, heritable
investment philosophy DNA that drives factor weights, thesis preferences, and
confidence models.

Architecture (补丁1: Doctrine as evolvable DNA, not just a classification result):

    Identity Genome
          ↓
    DoctrineEngine.classify()   ← Genesis: identity -> nearest archetype + interpolation
          ↓
    Doctrine Genome (persisted in doctrine_genome table, has parent_doctrine_id)
          ↓
    DoctrineEngine.crossover()  ← Evolution: doctrine × doctrine -> child doctrine
    DoctrineEngine.mutate()     ← Mutation: weighted distance (补丁3)
          ↓
    Expression (factor_bias / thesis_priority / confidence_model)

Key design decisions:
  * 12 archetypes are a Genesis Library (Gen 0 seeds), NOT a ceiling (补丁2).
    Evolution produces hybrid doctrines beyond the library; DoctrineRegistry
    tracks archetype | generated | extinct lifecycle.
  * classify() uses nearest-neighbor + continuous interpolation between the
    top-2 archetypes so the doctrine space is continuous (evolvable), not
    discrete (which would lock the search space).
  * crossover/mutate operate on Doctrine directly (not via identity), so a
    successful doctrine can reproduce without round-tripping through identity.
  * Mutation distance is weighted per-dimension (补丁3): patience/risk changes
    cost more than valuation changes, preventing species jumps in one generation.

IMPORTANT (import-cycle safety): imports only stdlib at module load. DB access
is lazy via _connect() so evaluation_db -> migration -> doctrine_engine never
forms a cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sqlite3
from dataclasses import dataclass, field

import yaml

# ──────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────

IDENTITY_DIMS = [
    "valuation",
    "quality",
    "growth",
    "momentum",
    "macro",
    "contrarian",
    "patience",
    "concentration",
]

FACTOR_FAMILIES = ["quality", "value", "growth", "momentum", "risk", "sentiment"]

# Default mutation cost weights (补丁3: weighted mutation distance).
# patience and risk-related dimensions have higher cost because changing them
# fundamentally alters the investment species.
DEFAULT_MUTATION_COST = {
    "valuation": 1.0,
    "quality": 1.0,
    "growth": 1.2,
    "momentum": 1.3,
    "macro": 1.0,
    "contrarian": 1.2,
    "patience": 2.0,
    "concentration": 1.0,
}

# Maximum allowed weighted mutation distance. Beyond this, the mutation is
# rejected (species jump prevention). Tuned so ~15 points on one high-cost
# dimension or ~25 on a low-cost dimension is the ceiling.
MAX_MUTATION_DISTANCE = 0.35

# Factor weight bounds for normalization.
FACTOR_WEIGHT_MIN = 0.0
FACTOR_WEIGHT_MAX = 0.60


# ──────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────


@dataclass
class ConfidenceModel:
    """Doctrine-specific confidence computation recipe."""

    base: float = 5.0  # starting confidence (0-10)
    evidence_weight: float = 0.8  # weight per matched primary metric
    primary_metrics: list[str] = field(default_factory=list)  # e.g. [roe, pb, fcf_yield]
    consistency_bonus: float = 0.5  # bonus per consistent factor family

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "evidence_weight": self.evidence_weight,
            "primary_metrics": self.primary_metrics,
            "consistency_bonus": self.consistency_bonus,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ConfidenceModel:
        return cls(
            base=d.get("base", 5.0),
            evidence_weight=d.get("evidence_weight", 0.8),
            primary_metrics=d.get("primary_metrics", []),
            consistency_bonus=d.get("consistency_bonus", 0.5),
        )


@dataclass
class DoctrineGenome:
    """A complete, heritable investment philosophy DNA.

    This is the evolvable unit in the Doctrine layer. It can be:
      - Created from identity via DoctrineEngine.classify() (Genesis)
      - Bred via DoctrineEngine.crossover() (Evolution)
      - Mutated via DoctrineEngine.mutate() (Exploration)
      - Persisted in doctrine_genome table (has parent_doctrine_id)
    """

    doctrine_id: str
    factor_bias: dict[str, float] = field(default_factory=dict)  # {quality: 0.35, value: 0.30, ...}
    thesis_priority: list[str] = field(default_factory=list)  # [quality_compound, deep_value, ...]
    confidence_model: ConfidenceModel = field(default_factory=ConfidenceModel)
    holding_period: int = 120
    principles: list[str] = field(default_factory=list)
    identity_origin: dict = field(default_factory=dict)  # identity that produced this (溯源)
    mutation_cost: dict = field(default_factory=lambda: dict(DEFAULT_MUTATION_COST))
    parent_doctrine_id: str | None = None  # 补丁1: direct lineage
    generation: int = 0
    mutation_history: list[dict] = field(default_factory=list)  # 补丁1: audit trail

    def to_dict(self) -> dict:
        return {
            "doctrine_id": self.doctrine_id,
            "factor_bias": self.factor_bias,
            "thesis_priority": self.thesis_priority,
            "confidence_model": self.confidence_model.to_dict(),
            "holding_period": self.holding_period,
            "principles": self.principles,
            "identity_origin": self.identity_origin,
            "mutation_cost": self.mutation_cost,
            "parent_doctrine_id": self.parent_doctrine_id,
            "generation": self.generation,
            "mutation_history": self.mutation_history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DoctrineGenome:
        return cls(
            doctrine_id=d["doctrine_id"],
            factor_bias=d.get("factor_bias", {}),
            thesis_priority=d.get("thesis_priority", []),
            confidence_model=ConfidenceModel.from_dict(d.get("confidence_model", {})),
            holding_period=d.get("holding_period", 120),
            principles=d.get("principles", []),
            identity_origin=d.get("identity_origin", {}),
            mutation_cost=d.get("mutation_cost", dict(DEFAULT_MUTATION_COST)),
            parent_doctrine_id=d.get("parent_doctrine_id"),
            generation=d.get("generation", 0),
            mutation_history=d.get("mutation_history", []),
        )

    def identity_distance(self, other_identity: dict) -> float:
        """Weighted Euclidean distance to an identity vector (补丁3).

        Uses per-dimension mutation_cost as weights so that patience/risk
        differences count more than valuation differences.
        """
        sq_sum = 0.0
        for dim in IDENTITY_DIMS:
            a = self.identity_origin.get(dim, 50)
            b = other_identity.get(dim, 50)
            cost = self.mutation_cost.get(dim, 1.0)
            sq_sum += cost * ((a - b) / 100.0) ** 2
        return math.sqrt(sq_sum)


# ──────────────────────────────────────────────────────────
# DDL for doctrine_genome table (补丁1)
# ──────────────────────────────────────────────────────────

DDL_DOCTRINE_GENOME = """
CREATE TABLE IF NOT EXISTS doctrine_genome (
    doctrine_id        TEXT PRIMARY KEY,
    parent_doctrine_id TEXT,
    identity_origin    TEXT NOT NULL,       -- JSON: identity vector that produced this
    factor_bias        TEXT NOT NULL,       -- JSON: {quality: 0.35, value: 0.30, ...}
    thesis_priority    TEXT NOT NULL,       -- JSON: ordered list
    confidence_model   TEXT NOT NULL,       -- JSON: ConfidenceModel
    holding_period     INTEGER DEFAULT 120,
    principles         TEXT,                -- JSON: list of strings
    mutation_cost      TEXT,                -- JSON: per-dim weights
    mutation_history   TEXT DEFAULT '[]',   -- JSON: audit trail
    fitness_score      REAL DEFAULT 0.0,
    generation         INTEGER DEFAULT 0,
    status             TEXT DEFAULT 'active',  -- active / extinct
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dg_parent ON doctrine_genome(parent_doctrine_id);
CREATE INDEX IF NOT EXISTS idx_dg_status ON doctrine_genome(status);
CREATE INDEX IF NOT EXISTS idx_dg_generation ON doctrine_genome(generation);
"""

# Confidence calibration table (补丁5)
DDL_CONFIDENCE_CALIBRATION = """
CREATE TABLE IF NOT EXISTS confidence_calibration (
    doctrine_id       TEXT NOT NULL,
    confidence_level  REAL NOT NULL,        -- 0-10 bucket (rounded to 0.5)
    realized_winrate  REAL DEFAULT 0.0,     -- historical actual win rate
    sample_size       INTEGER DEFAULT 0,
    last_updated      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (doctrine_id, confidence_level)
);
CREATE INDEX IF NOT EXISTS idx_cc_doctrine ON confidence_calibration(doctrine_id);
"""

# Thesis genome table (6-L.4: evolution of thesis patterns beyond fixed 6)
DDL_THESIS_GENOME = """
CREATE TABLE IF NOT EXISTS thesis_genome (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name        TEXT NOT NULL UNIQUE,
    parent_pattern      TEXT,               -- lineage for evolved patterns
    required_factors    TEXT NOT NULL,      -- JSON: {metric: (min, max)}
    claim_template      TEXT,
    family              TEXT,
    horizon             TEXT,
    generation          INTEGER DEFAULT 0,  -- 0 = genesis template
    fitness_score       REAL DEFAULT 0.0,
    status              TEXT DEFAULT 'active',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tg_pattern ON thesis_genome(pattern_name);
CREATE INDEX IF NOT EXISTS idx_tg_status ON thesis_genome(status);
"""


# ──────────────────────────────────────────────────────────
# DoctrineEngine
# ──────────────────────────────────────────────────────────


class DoctrineEngine:
    """Identity -> Doctrine -> Expression engine.

    Three core operations:
      1. classify(identity)     - Genesis: create doctrine from identity
      2. crossover(a, b, alpha) - Evolution: breed two doctrines
      3. mutate(d, rate)        - Exploration: weighted mutation
    """

    def __init__(self, archetypes_path: str | None = None):
        if archetypes_path is None:
            archetypes_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config",
                "doctrine_archetypes.yaml",
            )
        with open(archetypes_path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}
        self.archetypes: list[dict] = self._raw.get("archetypes", [])
        if not self.archetypes:
            raise ValueError(f"No archetypes found in {archetypes_path}")

    # ── Genesis: classify ────────────────────────────────

    def classify(self, identity_vector: dict, doctrine_seed: str | None = None) -> DoctrineGenome:
        """Create a DoctrineGenome from an identity vector.

        Uses nearest-neighbor matching against the 12 archetypes, then
        continuously interpolates factor_bias between the top-2 so the
        doctrine space is continuous (evolvable), not locked to discrete
        templates.

        Args:
            identity_vector: {valuation: 90, quality: 85, ...}
            doctrine_seed: optional hint to bias matching (e.g. "deep_value_purist")
        """
        # Score all archetypes by (inverse) distance to identity
        scored = []
        for arch in self.archetypes:
            sig = arch.get("identity_signature", {})
            dist = self._identity_distance(identity_vector, sig)
            scored.append((dist, arch))
        scored.sort(key=lambda x: x[0])

        nearest = scored[0][1]
        second = scored[1][1] if len(scored) > 1 else nearest
        d1, d2 = scored[0][0], scored[1][0]

        # Interpolation weight: closer archetype gets more weight.
        # If both equidistant, 50/50. Avoid division by zero.
        w1 = d2 / (d1 + d2) if d1 + d2 > 0 else 1.0
        w2 = 1.0 - w1

        # doctrine_seed biases the nearest selection
        if doctrine_seed:
            for _, arch in scored:
                if arch.get("doctrine_id") == doctrine_seed:
                    nearest = arch
                    w1 = max(w1, 0.7)
                    w2 = 1.0 - w1
                    break

        # Build doctrine_id: use nearest archetype name as base
        doctrine_id = nearest["doctrine_id"]

        # Interpolate factor_bias (continuous, not discrete)
        factor_bias = self._interpolate_factor_bias(
            nearest.get("factor_bias", {}),
            second.get("factor_bias", {}),
            w1,
            w2,
        )

        # Thesis priority: take nearest's ordering (thesis is discrete, not interpolated)
        thesis_priority = list(nearest.get("thesis_priority", []))

        # Confidence model: interpolate base + evidence_weight, take nearest's metrics
        cm_nearest = nearest.get("confidence_model", {})
        cm_second = second.get("confidence_model", {})
        confidence_model = ConfidenceModel(
            base=cm_nearest.get("base", 5.0) * w1 + cm_second.get("base", 5.0) * w2,
            evidence_weight=cm_nearest.get("evidence_weight", 0.8) * w1
            + cm_second.get("evidence_weight", 0.8) * w2,
            primary_metrics=list(cm_nearest.get("primary_metrics", [])),
            consistency_bonus=cm_nearest.get("consistency_bonus", 0.5),
        )

        return DoctrineGenome(
            doctrine_id=doctrine_id,
            factor_bias=factor_bias,
            thesis_priority=thesis_priority,
            confidence_model=confidence_model,
            holding_period=int(
                round(
                    nearest.get("holding_period", 120) * w1 + second.get("holding_period", 120) * w2
                )
            ),
            principles=list(nearest.get("principles", [])),
            identity_origin=dict(identity_vector),
            mutation_cost=dict(nearest.get("mutation_cost", DEFAULT_MUTATION_COST)),
            generation=0,
        )

    # ── Evolution: crossover (补丁1: doctrine × doctrine) ──

    def crossover(
        self, parent_a: DoctrineGenome, parent_b: DoctrineGenome, alpha: float | None = None
    ) -> DoctrineGenome:
        """Breed two DoctrineGenomes into a child.

        Unlike identity-level crossover, this operates directly on doctrine
        DNA: factor_bias, thesis_priority, confidence_model are all mixed.
        The child has parent_doctrine_id set for lineage tracking.

        Args:
            alpha: interpolation weight (child = A × alpha + B × (1-alpha)).
                   If None, random in [0.3, 0.7].
        """
        if alpha is None:
            alpha = random.uniform(0.3, 0.7)

        # Interpolate factor_bias
        child_bias = {}
        for fam in FACTOR_FAMILIES:
            va = parent_a.factor_bias.get(fam, 0.0)
            vb = parent_b.factor_bias.get(fam, 0.0)
            child_bias[fam] = round(va * alpha + vb * (1 - alpha), 4)
        self._normalize_bias(child_bias)

        # Thesis priority: merge lists preserving order, dedupe
        child_thesis = list(parent_a.thesis_priority)
        for t in parent_b.thesis_priority:
            if t not in child_thesis:
                child_thesis.append(t)

        # Confidence model: interpolate
        cm_a = parent_a.confidence_model
        cm_b = parent_b.confidence_model
        child_cm = ConfidenceModel(
            base=round(cm_a.base * alpha + cm_b.base * (1 - alpha), 2),
            evidence_weight=round(
                cm_a.evidence_weight * alpha + cm_b.evidence_weight * (1 - alpha), 2
            ),
            primary_metrics=cm_a.primary_metrics if alpha >= 0.5 else cm_b.primary_metrics,
            consistency_bonus=round(
                cm_a.consistency_bonus * alpha + cm_b.consistency_bonus * (1 - alpha), 2
            ),
        )

        # Generate child doctrine_id
        gen = max(parent_a.generation, parent_b.generation) + 1
        child_id = self._generate_child_id(parent_a.doctrine_id, parent_b.doctrine_id, gen)

        # Mutation cost: inherit from primary parent
        child_cost = dict(parent_a.mutation_cost)

        return DoctrineGenome(
            doctrine_id=child_id,
            factor_bias=child_bias,
            thesis_priority=child_thesis,
            confidence_model=child_cm,
            holding_period=int(
                round(parent_a.holding_period * alpha + parent_b.holding_period * (1 - alpha))
            ),
            principles=list(set(parent_a.principles) | set(parent_b.principles)),
            identity_origin=self._merge_identities(
                parent_a.identity_origin, parent_b.identity_origin, alpha
            ),
            mutation_cost=child_cost,
            parent_doctrine_id=parent_a.doctrine_id,  # primary parent for lineage
            generation=gen,
            mutation_history=[
                {
                    "type": "crossover",
                    "parent_a": parent_a.doctrine_id,
                    "parent_b": parent_b.doctrine_id,
                    "alpha": round(alpha, 3),
                }
            ],
        )

    # ── Exploration: mutate (补丁3: weighted distance) ─────

    def mutate(
        self,
        doctrine: DoctrineGenome,
        mutation_rate: float = 0.15,
        max_distance: float = MAX_MUTATION_DISTANCE,
    ) -> DoctrineGenome | None:
        """Mutate a DoctrineGenome's factor_bias with weighted distance guard.

        Returns a NEW DoctrineGenome (original unchanged), or None if the
        mutation would exceed the max weighted distance (species jump).

        补丁3: mutation distance = sqrt(Σ(cost_i × delta_i²)) where cost_i
        is the per-dimension weight. patience/risk changes cost more.
        """
        # Mutate factor_bias: nudge each family by ±mutation_rate
        delta_sq_sum = 0.0
        new_bias = {}
        for fam in FACTOR_FAMILIES:
            old_val = doctrine.factor_bias.get(fam, 0.0)
            delta = random.gauss(0, mutation_rate * 0.3)  # small Gaussian nudge
            new_val = max(FACTOR_WEIGHT_MIN, min(FACTOR_WEIGHT_MAX, old_val + delta))
            new_bias[fam] = round(new_val, 4)
            delta_sq_sum += delta**2
        self._normalize_bias(new_bias)

        # The factor_bias mutation distance is in weight space (0-1 scale),
        # not identity space. Check it's reasonable.
        weight_distance = math.sqrt(delta_sq_sum)
        if weight_distance > max_distance:
            return None  # reject: too large a jump

        # Also nudge holding_period slightly
        new_holding = max(15, int(doctrine.holding_period * (1 + random.gauss(0, 0.1))))
        new_holding = min(new_holding, 500)

        # Nudge confidence base slightly
        cm = doctrine.confidence_model
        new_base = max(2.0, min(7.0, cm.base + random.gauss(0, 0.3)))

        child_id = self._generate_child_id(doctrine.doctrine_id, None, doctrine.generation + 1)
        history = list(doctrine.mutation_history) + [
            {
                "type": "mutation",
                "parent": doctrine.doctrine_id,
                "weight_distance": round(weight_distance, 4),
                "mutation_rate": mutation_rate,
            }
        ]

        return DoctrineGenome(
            doctrine_id=child_id,
            factor_bias=new_bias,
            thesis_priority=list(doctrine.thesis_priority),
            confidence_model=ConfidenceModel(
                base=round(new_base, 2),
                evidence_weight=cm.evidence_weight,
                primary_metrics=list(cm.primary_metrics),
                consistency_bonus=cm.consistency_bonus,
            ),
            holding_period=new_holding,
            principles=list(doctrine.principles),
            identity_origin=dict(doctrine.identity_origin),
            mutation_cost=dict(doctrine.mutation_cost),
            parent_doctrine_id=doctrine.doctrine_id,
            generation=doctrine.generation + 1,
            mutation_history=history,
        )

    # ── Weighted identity mutation distance (补丁3) ───────

    def identity_mutation_distance(
        self, parent_identity: dict, child_identity: dict, cost_weights: dict | None = None
    ) -> float:
        """Compute weighted mutation distance between two identity vectors.

        补丁3: distance = sqrt(Σ(weight_i × (delta_i/100)²))
        where weight_i comes from the doctrine's mutation_cost.
        """
        costs = cost_weights or DEFAULT_MUTATION_COST
        sq_sum = 0.0
        for dim in IDENTITY_DIMS:
            a = parent_identity.get(dim, 50)
            b = child_identity.get(dim, 50)
            w = costs.get(dim, 1.0)
            sq_sum += w * ((a - b) / 100.0) ** 2
        return math.sqrt(sq_sum)

    def is_mutation_allowed(
        self,
        parent_identity: dict,
        child_identity: dict,
        cost_weights: dict | None = None,
        threshold: float = MAX_MUTATION_DISTANCE,
    ) -> bool:
        """Check if an identity mutation is within allowed distance (补丁3)."""
        return (
            self.identity_mutation_distance(parent_identity, child_identity, cost_weights)
            <= threshold
        )

    # ── Helpers ──────────────────────────────────────────

    def _identity_distance(self, a: dict, b: dict) -> float:
        """Plain Euclidean distance between two identity vectors (for classify)."""
        sq_sum = 0.0
        for dim in IDENTITY_DIMS:
            va = a.get(dim, 50)
            vb = b.get(dim, 50)
            sq_sum += ((va - vb) / 100.0) ** 2
        return math.sqrt(sq_sum)

    def _interpolate_factor_bias(self, bias_a: dict, bias_b: dict, w1: float, w2: float) -> dict:
        """Interpolate two factor_bias dicts and renormalize."""
        result = {}
        for fam in FACTOR_FAMILIES:
            va = bias_a.get(fam, 0.0)
            vb = bias_b.get(fam, 0.0)
            result[fam] = round(va * w1 + vb * w2, 4)
        self._normalize_bias(result)
        return result

    def _normalize_bias(self, bias: dict) -> None:
        """Normalize factor_bias weights to sum to 1.0 (in-place)."""
        total = sum(bias.values())
        if total > 0:
            for k in bias:
                bias[k] = round(bias[k] / total, 4)

    def _merge_identities(self, a: dict, b: dict, alpha: float) -> dict:
        """Interpolate two identity vectors."""
        result = {}
        for dim in IDENTITY_DIMS:
            va = a.get(dim, 50)
            vb = b.get(dim, 50)
            result[dim] = int(round(va * alpha + vb * (1 - alpha)))
        return result

    def _generate_child_id(self, parent_a_id: str, parent_b_id: str | None, generation: int) -> str:
        """Generate a unique child doctrine_id."""
        short_a = parent_a_id.split("_")[0][:4] if parent_a_id else "root"
        if parent_b_id:
            short_b = parent_b_id.split("_")[0][:4]
            base = f"{short_a}_{short_b}"
        else:
            base = short_a
        suffix = hashlib.sha256(
            f"{parent_a_id}_{parent_b_id}_{generation}_{random.randint(1, 99999)}".encode()
        ).hexdigest()[:6]
        return f"{base}_gen{generation}_{suffix}"


# ──────────────────────────────────────────────────────────
# DoctrineRegistry - lifecycle management (补丁2)
# ──────────────────────────────────────────────────────────


class DoctrineRegistry:
    """Manages doctrine lifecycle: archetype | generated | extinct.

    Archetypes (Gen 0) are loaded from the YAML library.
    Generated doctrines (Gen 1+) are persisted in doctrine_genome table.
    Extinct doctrines are marked status='extinct' (补丁5后续: doctrine extinction).
    """

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db_path = db_path
        self.engine = DoctrineEngine()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(DDL_DOCTRINE_GENOME)
        conn.executescript(DDL_CONFIDENCE_CALIBRATION)
        conn.executescript(DDL_THESIS_GENOME)
        conn.commit()
        return conn

    def seed_archetypes(self) -> int:
        """Persist all 12 archetypes into doctrine_genome table (Gen 0).

        Idempotent: existing archetypes are skipped.
        """
        conn = self._connect()
        try:
            count = 0
            for arch in self.engine.archetypes:
                doctrine = DoctrineGenome(
                    doctrine_id=arch["doctrine_id"],
                    factor_bias=arch.get("factor_bias", {}),
                    thesis_priority=arch.get("thesis_priority", []),
                    confidence_model=ConfidenceModel.from_dict(arch.get("confidence_model", {})),
                    holding_period=arch.get("holding_period", 120),
                    principles=arch.get("principles", []),
                    identity_origin=arch.get("identity_signature", {}),
                    mutation_cost=arch.get("mutation_cost", dict(DEFAULT_MUTATION_COST)),
                    generation=0,
                )
                self._upsert(conn, doctrine)
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get(self, doctrine_id: str) -> DoctrineGenome | None:
        """Load a doctrine by ID from the DB."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM doctrine_genome WHERE doctrine_id=?", (doctrine_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_doctrine(row)
        finally:
            conn.close()

    def get_active(self) -> list[DoctrineGenome]:
        """Get all active doctrines (archetypes + generated)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM doctrine_genome WHERE status='active' ORDER BY generation, doctrine_id"
            ).fetchall()
            return [self._row_to_doctrine(r) for r in rows]
        finally:
            conn.close()

    def save(self, doctrine: DoctrineGenome) -> None:
        """Persist a doctrine (insert or update)."""
        conn = self._connect()
        try:
            self._upsert(conn, doctrine)
            conn.commit()
        finally:
            conn.close()

    def extinct(self, doctrine_id: str) -> bool:
        """Mark a doctrine as extinct (补丁5后续: investment philosophy extinction)."""
        conn = self._connect()
        try:
            n = conn.execute(
                "UPDATE doctrine_genome SET status='extinct' WHERE doctrine_id=? AND status='active'",
                (doctrine_id,),
            ).rowcount
            conn.commit()
            return n > 0
        finally:
            conn.close()

    def _upsert(self, conn: sqlite3.Connection, doctrine: DoctrineGenome) -> None:
        """INSERT OR REPLACE a doctrine into the DB."""
        conn.execute(
            """
            INSERT OR REPLACE INTO doctrine_genome
            (doctrine_id, parent_doctrine_id, identity_origin, factor_bias,
             thesis_priority, confidence_model, holding_period, principles,
             mutation_cost, mutation_history, generation, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,COALESCE(
                (SELECT status FROM doctrine_genome WHERE doctrine_id=?), 'active'))
        """,
            (
                doctrine.doctrine_id,
                doctrine.parent_doctrine_id,
                json.dumps(doctrine.identity_origin),
                json.dumps(doctrine.factor_bias),
                json.dumps(doctrine.thesis_priority),
                json.dumps(doctrine.confidence_model.to_dict()),
                doctrine.holding_period,
                json.dumps(doctrine.principles),
                json.dumps(doctrine.mutation_cost),
                json.dumps(doctrine.mutation_history),
                doctrine.generation,
                doctrine.doctrine_id,
            ),
        )

    def _row_to_doctrine(self, row: sqlite3.Row) -> DoctrineGenome:
        """Convert a DB row to a DoctrineGenome."""
        return DoctrineGenome(
            doctrine_id=row["doctrine_id"],
            factor_bias=json.loads(row["factor_bias"]) if row["factor_bias"] else {},
            thesis_priority=json.loads(row["thesis_priority"]) if row["thesis_priority"] else [],
            confidence_model=ConfidenceModel.from_dict(
                json.loads(row["confidence_model"]) if row["confidence_model"] else {}
            ),
            holding_period=row["holding_period"],
            principles=json.loads(row["principles"]) if row["principles"] else [],
            identity_origin=json.loads(row["identity_origin"]) if row["identity_origin"] else {},
            mutation_cost=json.loads(row["mutation_cost"])
            if row["mutation_cost"]
            else dict(DEFAULT_MUTATION_COST),
            parent_doctrine_id=row["parent_doctrine_id"],
            generation=row["generation"],
            mutation_history=json.loads(row["mutation_history"]) if row["mutation_history"] else [],
        )
