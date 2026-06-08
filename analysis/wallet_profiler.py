"""
wallet_profiler.py — Behavioral clustering wallet + anomaly detection.

Tipe: SNIPER, FLIPPER, DCA, WHALE, HOLDER, TEST, SWINGER.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def classify_wallet(trades: list[dict]) -> str:
    """trades: list dict dengan trade_type, sol_amount, token_mint, timestamp."""
    if not trades:
        return "UNKNOWN"

    n = len(trades)
    buys = [t for t in trades if t.get("trade_type") == "buy"]
    sells = [t for t in trades if t.get("trade_type") == "sell"]
    tokens = {t.get("token_mint") for t in trades}
    sols = [float(t.get("sol_amount") or 0) for t in trades]
    avg_sol = sum(sols) / n if n else 0.0
    total_bought = sum(float(t.get("sol_amount") or 0) for t in buys)

    times = sorted(filter(None, (_parse(t.get("timestamp", "")) for t in trades)))
    span_hours = ((times[-1] - times[0]).total_seconds() / 3600.0) if len(times) >= 2 else 0.0

    bs_ratio = (len(buys) / len(sells)) if sells else float(len(buys))

    # TEST: wallet kecil & baru
    if n <= 5 and avg_sol < 0.1:
        return "TEST"
    # WHALE
    if avg_sol > 10 or total_bought > 50:
        return "WHALE"
    # SNIPER: hold pendek, banyak token, banyak trade
    if span_hours and span_hours / max(n, 1) < (5 / 60) and len(tokens) >= 3 and n >= 10:
        return "SNIPER"
    # FLIPPER
    if n >= 15 and 0 < span_hours <= 2 * n:
        return "FLIPPER"
    # DCA / akumulasi
    if bs_ratio > 1.5 and len(tokens) <= 5 and len(buys) >= 5:
        return "DCA"
    # HOLDER
    if span_hours > 24 and len(sells) <= 0.3 * max(len(buys), 1):
        return "HOLDER"
    # SWINGER
    if 1 <= span_hours <= 24:
        return "SWINGER"
    return "FLIPPER"


def anomaly_score(metrics: dict) -> tuple[int, list[str]]:
    """
    metrics:
      accel_ratio   — wallet beli/jam naik berapa x
      bs_ratio      — buy/sell ratio
      tx_velocity   — tx/jam naik berapa x
      small_wallets — jumlah wallet kecil beli barengan
    """
    score = 0
    notes: list[str] = []
    if metrics.get("accel_ratio", 1) >= 3:
        score += 25; notes.append("Akselerasi wallet 3x")
    if metrics.get("bs_ratio", 0) >= 5:
        score += 20; notes.append("Buy/Sell spike")
    if metrics.get("tx_velocity", 1) >= 2:
        score += 15; notes.append("Tx velocity naik")
    if metrics.get("small_wallets", 0) >= 5:
        score += 15; notes.append("Konspirasi wallet kecil")
    return min(100, score), notes
