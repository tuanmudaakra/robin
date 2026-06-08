"""
momentum.py — Skor momentum (0..25) dari volume & price action.
"""
from __future__ import annotations

from ingestion.normalizer import TokenSnapshot

MAX = 25


def momentum_score(snap: TokenSnapshot) -> tuple[int, list[str]]:
    score = 0.0
    notes: list[str] = []

    # volume spike: volume 1h dianualisasi vs volume 24h rata-rata per jam
    avg_hourly = (snap.volume_24h / 24.0) if snap.volume_24h else 0.0
    if avg_hourly > 0 and snap.volume_1h > 0:
        ratio = snap.volume_1h / avg_hourly
        if ratio >= 3:
            score += 10; notes.append(f"Volume spike {ratio:.1f}x")
        elif ratio >= 1.5:
            score += 6; notes.append(f"Volume naik {ratio:.1f}x")
        elif ratio >= 1.0:
            score += 3

    # tx count 1h
    tx = snap.txns_1h_buys + snap.txns_1h_sells
    if tx >= 200:
        score += 6
    elif tx >= 50:
        score += 4
    elif tx >= 10:
        score += 2
    # dominasi buy
    if tx > 0 and snap.txns_1h_buys / tx >= 0.6:
        score += 2; notes.append("Buy pressure")

    # price change
    if snap.price_change_1h >= 20:
        score += 5; notes.append(f"+{snap.price_change_1h:.0f}% 1j")
    elif snap.price_change_1h >= 5:
        score += 3
    elif snap.price_change_1h <= -20:
        score -= 3; notes.append("Dumping 1j")

    if snap.price_change_24h >= 50:
        score += 2

    return int(max(0, min(MAX, round(score)))), notes
