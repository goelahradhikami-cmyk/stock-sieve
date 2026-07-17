"""Tests for Commit 2: Real Market Data Pipeline."""
import pytest
from datetime import date

from src.data.provider import MarketDataProvider
from src.data.calendar import TradingCalendar
from src.execution.simulator import ExecutionSimulator


def test_kline_with_adj():
    """Test mootdx K-line returns data directly."""
    from mootdx.quotes import Quotes
    q = Quotes.factory(market='std')
    df = q.bars(symbol='600519', frequency=9, offset=250)
    if df.empty:
        pytest.skip("mootdx not connected — skipping")
    assert 'close' in df.columns, "Should have close column"
    assert df['close'].iloc[-1] > 0, "Price should be positive"


def test_kline_columns():
    """Test K-line has required columns."""
    from mootdx.quotes import Quotes
    q = Quotes.factory(market='std')
    df = q.bars(symbol='000001', frequency=9, offset=30)
    if df.empty:
        pytest.skip("mootdx not connected — skipping")
    for col in ['open', 'close', 'high', 'low']:
        assert col in df.columns, f"Missing column: {col}"


def test_trading_calendar_unknown():
    """Test that unknown dates default to non-trading."""
    cal = TradingCalendar()
    # Sunday — definitely not trading
    assert cal.is_trade_day(date(2026, 2, 1)) is False
    # Far future date — not in DB → not trading
    assert cal.is_trade_day(date(2030, 1, 1)) is False


def test_trading_calendar_seed():
    """Test seed generates tradable weekdays."""
    cal = TradingCalendar()
    cal.seed_sample()
    # A known Monday should be trading
    assert cal.is_trade_day(date(2025, 3, 10)) is True  # Monday
    # National Day should NOT be trading
    assert cal.is_trade_day(date(2025, 10, 1)) is False


def test_trading_calendar_navigation():
    """Test previous/next trade day."""
    cal = TradingCalendar()
    cal.seed_sample()
    prev = cal.previous_trade_day(date(2025, 3, 12))  # Wednesday
    assert prev is not None
    assert prev == date(2025, 3, 11)  # Tuesday


def test_execution_simulator_buy():
    """Test buy order simulation."""
    sim = ExecutionSimulator()
    res = sim.simulate_order('600519', 'BUY', 100, 1500.0)
    assert res['fill_price'] >= 1500.0, "Buy slippage should push price up"
    assert res['commission'] > 0
    assert res['stamp_tax'] == 0.0, "Buy has no stamp tax"
    assert res['total_cost'] > 0
    assert res['execution_mode'] == 'PAPER'


def test_execution_simulator_sell():
    """Test sell order simulation."""
    sim = ExecutionSimulator()
    res = sim.simulate_order('600519', 'SELL', 100, 1500.0)
    assert res['fill_price'] <= 1500.0, "Sell slippage should push price down"
    assert res['stamp_tax'] > 0, "Sell has stamp tax"
    assert res['commission'] > 0


def test_execution_simulator_cost_breakdown():
    """Test all cost components are present."""
    sim = ExecutionSimulator()
    res = sim.simulate_order('600519', 'SELL', 100, 1500.0, market_volume=1e7)
    for key in ['fill_price', 'slippage', 'commission', 'stamp_tax',
                'transfer_fee', 'total_cost', 'execution_mode']:
        assert key in res, f"Missing key: {key}"
        assert res[key] is not None, f"None value for: {key}"
