"""
Market Ecology Engine — Real-market competition simulation.

Commit 6-K.1: Crowding decay, capacity limits, A-share microstructure,
and shadow trading mode.
"""

from datetime import date, timedelta

import numpy as np

from src.data.db import managed_connect

# ═══════════════════════════════════════════════════════════
# A-Share Microstructure
# ═══════════════════════════════════════════════════════════

class AShareMicrostructure:
    """A-share specific trading rules: limit rates, T+1, lot sizes."""

    LIMIT_RATES = {'688': 0.20, '300': 0.20, '301': 0.20, 'default': 0.10}

    def get_limit_rate(self, code: str, name: str = '') -> float:
        if code.startswith(('688', '300', '301')):
            return 0.20
        if 'ST' in (name or ''):
            return 0.05
        return 0.10

    def check_t1_settlement(self, buy_date: date, sell_date: date) -> bool:
        return (sell_date - buy_date).days >= 1

    def simulate_order_book(self, signal_price: float, quantity: int,
                            action: str, adv: float) -> dict:
        """Multi-level order book simulation."""
        if action in ('BUY', 'ADD'):
            levels = [
                (signal_price * 1.001, adv * 0.02),
                (signal_price * 1.003, adv * 0.03),
                (signal_price * 1.005, adv * 0.05),
            ]
        else:
            levels = [
                (signal_price * 0.999, adv * 0.02),
                (signal_price * 0.997, adv * 0.03),
                (signal_price * 0.995, adv * 0.05),
            ]

        filled_qty, total_cost = 0, 0.0
        for price, level_qty in levels:
            if filled_qty >= quantity:
                break
            take = min(quantity - filled_qty, int(level_qty))
            filled_qty += take
            total_cost += take * price

        avg_price = total_cost / filled_qty if filled_qty > 0 else signal_price
        return {
            'executed_price': round(avg_price, 2),
            'filled_quantity': filled_qty,
            'slippage_bps': round(abs(avg_price - signal_price) / signal_price * 10000),
        }


# ═══════════════════════════════════════════════════════════
# Market Ecology Engine
# ═══════════════════════════════════════════════════════════

class MarketEcologyEngine:
    """Crowding decay, capacity limits, alpha erosion simulation."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS market_ecology_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date DATE, security_id TEXT,
                institutional_flow REAL, quant_position REAL,
                retail_sentiment REAL,
                crowding_score REAL,
                liquidity_depth REAL,
                alpha_decay_rate REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS capacity_genome (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genome_id TEXT UNIQUE NOT NULL,
                max_aum REAL DEFAULT 5000000,
                max_position_adv REAL DEFAULT 0.05,
                liquidity_preference REAL DEFAULT 0.5,
                small_cap_penalty REAL DEFAULT 0.2,
                fitness_score REAL DEFAULT 0.0,
                status TEXT DEFAULT 'testing',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shadow_trading (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_time TIMESTAMP, security_id TEXT,
                action TEXT, signal_price REAL, market_price REAL,
                sim_execution_price REAL, alpha_error REAL, execution_error REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.db.commit()

    def calc_crowding_decay(self, security_id: str, factor_exposure: dict = None,
                            trade_date: date = None) -> float:
        """Alpha decay based on strategy crowding (0-1, 1=no decay)."""
        if trade_date is None:
            trade_date = date.today()

        crowding = self._estimate_crowding(security_id, trade_date)
        decay = max(0.1, 1 - crowding * 0.7)

        try:
            self.db.execute("""
                INSERT OR REPLACE INTO market_ecology_state
                (trade_date, security_id, crowding_score, alpha_decay_rate)
                VALUES (?,?,?,?)
            """, (trade_date.isoformat(), security_id, crowding, decay))
            self.db.commit()
        except Exception:
            pass

        return decay

    def calc_capacity_limit(self, genome_id: str, signal_quantity: int,
                            security_id: str, trade_date: date = None) -> int:
        """Limit position size based on strategy capacity."""
        row = self.db.execute(
            "SELECT max_position_adv FROM capacity_genome WHERE genome_id=?",
            (genome_id,)
        ).fetchone()
        max_adv_pct = row[0] if row else 0.05
        avg_volume = 1e6  # Default: ~100万股/day
        max_shares = int(avg_volume * max_adv_pct / 100)
        return min(signal_quantity, max_shares)

    def _estimate_crowding(self, security_id: str, trade_date: date) -> float:
        """Estimate crowding from turnover/volatility changes."""
        try:
            from src.data.local_provider import LocalDataProvider
            lp = LocalDataProvider()
            code = security_id.split('.')[0] if '.' in security_id else security_id
            start = (trade_date - timedelta(days=60)).isoformat()
            kline = lp.get_daily_kline(code, start, trade_date.isoformat())
            if kline.empty or len(kline) < 40:
                return 0.5
            if 'turnover' in kline.columns and kline['turnover'].notna().any():
                recent = kline['turnover'].tail(20).mean()
                hist = kline['turnover'].head(20).mean()
                return min(1.0, max(0.0, (recent / max(0.1, hist) - 1)))
        except Exception:
            pass
        return 0.5

    def get_alpha_decay(self, security_id: str) -> float:
        """Get latest alpha decay rate for a security."""
        row = self.db.execute(
            "SELECT alpha_decay_rate FROM market_ecology_state WHERE security_id=? ORDER BY trade_date DESC LIMIT 1",
            (security_id,)
        ).fetchone()
        return row[0] if row else 1.0


# ═══════════════════════════════════════════════════════════
# Shadow Trader
# ═══════════════════════════════════════════════════════════

class ShadowTrader:
    """Record simulated executions and compare with real market."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self.micro = AShareMicrostructure()

    def execute_shadow(self, signal: dict, trade_date: date) -> dict:
        """Simulate order execution and record."""
        security_id = signal.get('security_id', '')
        code = security_id.split('.')[0] if '.' in security_id else security_id
        action = signal.get('action', 'BUY')
        signal_price = signal.get('signal_price', 100)
        quantity = signal.get('quantity', 100)

        exec_result = self.micro.simulate_order_book(signal_price, quantity, action, 1e6)

        try:
            self.db.execute("""
                INSERT INTO shadow_trading
                (signal_time, security_id, action, signal_price, market_price,
                 sim_execution_price, execution_error)
                VALUES (?,?,?,?,?,?,?)
            """, (trade_date.isoformat(), security_id, action,
                  signal_price, signal.get('market_price', signal_price),
                  exec_result['executed_price'],
                  signal_price - exec_result['executed_price']))
            self.db.commit()
        except Exception:
            pass

        return exec_result

    def get_execution_stats(self, days: int = 30) -> dict:
        """Get recent shadow trading stats."""
        rows = self.db.execute("""
            SELECT AVG(ABS(execution_error)) as avg_error, COUNT(*) as cnt
            FROM shadow_trading
            WHERE created_at >= date('now', ?)
        """, (f'-{days} days',)).fetchone()
        return {
            'avg_execution_error': round(rows[0], 4) if rows and rows[0] else 0,
            'total_trades': rows[1] if rows else 0,
        }


# ═══════════════════════════════════════════════════════════
# Market Reality Engine — Configurable friction simulation
# ═══════════════════════════════════════════════════════════

class MarketRealityEngine:
    """Full market friction: slippage, impact, limits, suspensions, costs."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self.micro = AShareMicrostructure()
        self._ensure_tables()
        self.config = self._load_config()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS market_reality_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_name TEXT NOT NULL,
                slippage_model TEXT DEFAULT 'fixed',
                slippage_bps REAL DEFAULT 5.0,
                market_impact_factor REAL DEFAULT 0.1,
                latency_ms INTEGER DEFAULT 100,
                fill_probability REAL DEFAULT 0.95,
                max_position_pct_adv REAL DEFAULT 0.05,
                lot_size INTEGER DEFAULT 100,
                stamp_duty REAL DEFAULT 0.001,
                commission_rate REAL DEFAULT 0.0003,
                min_commission REAL DEFAULT 5.0,
                limit_up_down_filter INTEGER DEFAULT 1,
                suspension_filter INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS execution_sim_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genome_id TEXT, trade_date DATE,
                security_id TEXT, action TEXT,
                signal_price REAL, theoretical_price REAL,
                executed_price REAL, slippage_bps REAL,
                market_impact_bps REAL, commission REAL,
                stamp_duty REAL, filled_quantity INTEGER,
                status TEXT, rejection_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        if not self.db.execute("SELECT id FROM market_reality_config LIMIT 1").fetchone():
            self.db.execute("INSERT INTO market_reality_config (scenario_name) VALUES ('default')")
        self.db.commit()

    def _load_config(self) -> dict:
        row = self.db.execute(
            "SELECT * FROM market_reality_config WHERE scenario_name='default'"
        ).fetchone()
        if not row:
            return {}
        cols = [d[1] for d in self.db.execute("PRAGMA table_info(market_reality_config)")]
        return dict(zip(cols, row))

    def simulate_order(self, security_id: str, action: str, quantity: int,
                        signal_price: float, trade_date: date) -> dict:
        """Simulate a single order with full market friction."""
        code = security_id.split('.')[0] if '.' in security_id else security_id

        # 1. Limit-up/down filter
        if self.config.get('limit_up_down_filter'):
            limit_rate = self.micro.get_limit_rate(code)
            if action in ('BUY', 'ADD') and signal_price >= signal_price * 1.09:
                return self._reject('涨停无法买入')
            if action in ('SELL', 'REDUCE') and signal_price <= signal_price * 0.91:
                return self._reject('跌停无法卖出')

        # 2. Suspension filter (skip if no data available for trade_date)
        if self.config.get('suspension_filter'):
            try:
                from src.data.local_provider import LocalDataProvider
                lp = LocalDataProvider()
                kline = lp.get_daily_kline(code, trade_date.isoformat(), trade_date.isoformat())
                if not kline.empty and kline.iloc[0].get('volume', 1) == 0:
                    return self._reject('停牌无法交易')
            except Exception:
                pass  # No data → assume tradable

        # 3. Slippage
        slippage = self._calc_slippage(security_id, quantity, signal_price)
        # 4. Market impact
        impact = self._calc_market_impact(security_id, quantity, trade_date)
        # 5. Fill probability
        fill_prob = self._calc_fill_probability(security_id, quantity, trade_date)

        # 6. Executed price
        if action in ('BUY', 'ADD'):
            executed_price = signal_price * (1 + slippage + impact)
        else:
            executed_price = signal_price * (1 - slippage - impact)

        # 7. Costs
        turnover = executed_price * quantity
        commission = max(
            self.config.get('min_commission', 5.0),
            turnover * self.config.get('commission_rate', 0.0003)
        )
        stamp_duty = turnover * self.config.get('stamp_duty', 0.001) if action in ('SELL', 'REDUCE') else 0

        # 8. Fill result
        if np.random.random() > fill_prob:
            filled_qty = int(quantity * fill_prob)
            status = 'partial' if filled_qty > 0 else 'rejected'
        else:
            filled_qty = quantity
            status = 'filled'

        # 9. Log
        try:
            self.db.execute("""
                INSERT INTO execution_sim_log
                (trade_date, security_id, action, signal_price, executed_price,
                 slippage_bps, market_impact_bps, commission, stamp_duty,
                 filled_quantity, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (trade_date.isoformat(), security_id, action, signal_price,
                  executed_price, slippage*10000, impact*10000,
                  commission, stamp_duty, filled_qty, status))
            self.db.commit()
        except Exception:
            pass

        return {
            'executed_price': round(executed_price, 2),
            'filled_quantity': filled_qty,
            'status': status,
            'cost': round(commission + stamp_duty, 2),
            'slippage_bps': round(slippage * 10000),
        }

    def _calc_slippage(self, security_id: str, quantity: int, price: float) -> float:
        model = self.config.get('slippage_model', 'fixed')
        base = self.config.get('slippage_bps', 5) / 10000
        if model == 'fixed':
            return base
        elif model == 'proportional':
            return base * (1 + np.random.normal(0, 0.5))
        elif model == 'square_root':
            return base * np.sqrt(quantity / 1000)
        return base

    def _calc_market_impact(self, security_id: str, quantity: int, trade_date: date) -> float:
        avg_volume = self._get_avg_volume(security_id, trade_date)
        if avg_volume <= 0:
            return self.config.get('market_impact_factor', 0.1) / 100
        participation = quantity * self.config.get('lot_size', 100) / avg_volume
        return self.config.get('market_impact_factor', 0.1) * np.sqrt(participation)

    def _calc_fill_probability(self, security_id: str, quantity: int, trade_date: date) -> float:
        avg_volume = self._get_avg_volume(security_id, trade_date)
        if avg_volume <= 0:
            return 0.5
        max_vol = self.config.get('max_position_pct_adv', 0.05) * avg_volume
        if quantity * 100 > max_vol * 2:
            return 0.3
        elif quantity * 100 > max_vol:
            return 0.7
        return self.config.get('fill_probability', 0.95)

    def _get_avg_volume(self, security_id: str, trade_date: date) -> float:
        code = security_id.split('.')[0] if '.' in security_id else security_id
        try:
            from src.data.local_provider import LocalDataProvider
            lp = LocalDataProvider()
            start = (trade_date - timedelta(days=30)).isoformat()
            kline = lp.get_daily_kline(code, start, trade_date.isoformat())
            if not kline.empty and 'volume' in kline.columns:
                return float(kline['volume'].tail(20).mean())
        except Exception:
            pass
        return 1e6

    def _reject(self, reason: str) -> dict:
        return {'status': 'rejected', 'executed_price': 0, 'filled_quantity': 0,
                'cost': 0, 'slippage_bps': 0, 'rejection_reason': reason}
