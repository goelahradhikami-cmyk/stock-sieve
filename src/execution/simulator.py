"""
Execution Simulator — Paper trading order simulation.

Models slippage, commissions, stamp tax, and transfer fees.
"""

from datetime import date


class ExecutionSimulator:
    """Simulates order execution with realistic cost breakdown."""

    def __init__(self, stamp_tax_rate: float = 0.001, commission_rate: float = 0.0003):
        """
        Args:
            stamp_tax_rate: 印花税率（卖出单边征收，默认 0.1%）
            commission_rate: 券商佣金率（默认 0.03%）
        """
        self.stamp_tax_rate = stamp_tax_rate
        self.commission_rate = commission_rate

    def simulate_order(
        self, code: str, action: str, quantity: int, signal_price: float, market_volume: float = 1e7
    ) -> dict:
        """Simulate a single order execution.

        Args:
            code: Stock code
            action: 'BUY', 'SELL', 'ADD', 'REDUCE'
            quantity: Number of shares
            signal_price: Reference price (e.g. last close or limit price)
            market_volume: Daily market volume in CNY (for slippage model)

        Returns:
            dict with fill_price, slippage, commission, stamp_tax, transfer_fee,
            total_cost, execution_mode
        """
        # Slippage model: inversely proportional to market volume
        if market_volume > 0:
            slippage_rate = min(0.002, 0.0005 / (market_volume / 1e7))
        else:
            slippage_rate = 0.001

        # Buy: fill slightly higher; Sell: fill slightly lower
        if action in ("BUY", "ADD"):
            fill_price = signal_price * (1 + slippage_rate)
        else:
            fill_price = signal_price * (1 - slippage_rate)

        slippage = abs(fill_price - signal_price) / signal_price
        turnover = fill_price * quantity

        # Commission (minimum 5 CNY)
        commission = max(5.0, turnover * self.commission_rate)

        # Stamp tax: sell only
        stamp_tax = turnover * self.stamp_tax_rate if action in ("SELL", "REDUCE") else 0.0

        # Transfer fee (~0.002%)
        transfer_fee = turnover * 0.00002

        total_cost = commission + stamp_tax + transfer_fee

        return {
            "fill_price": round(fill_price, 2),
            "slippage": round(slippage, 6),
            "commission": round(commission, 2),
            "stamp_tax": round(stamp_tax, 2),
            "transfer_fee": round(transfer_fee, 2),
            "total_cost": round(total_cost, 2),
            "execution_mode": "PAPER",
        }

    def simulate_portfolio_decision(
        self, decision: dict, current_price: float, avg_daily_amount: float = 1e7
    ) -> dict:
        """Wrap simulate_order with portfolio_decision context.

        Returns dict ready for portfolio_execution table insert.
        """
        result = self.simulate_order(
            code=decision.get("stock_code", ""),
            action=decision.get("action", "BUY"),
            quantity=int(decision.get("quantity", 0)),
            signal_price=current_price,
            market_volume=avg_daily_amount,
        )
        result.update(
            {
                "portfolio_decision_id": decision.get("portfolio_decision_id"),
                "security_id": decision.get("stock_code"),
                "action": decision.get("action"),
                "order_price": current_price,
                "quantity": decision.get("quantity"),
                "execution_date": date.today().isoformat(),
                "execution_status": "filled",
            }
        )
        return result
