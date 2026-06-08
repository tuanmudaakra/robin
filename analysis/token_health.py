"""
token_health.py — Safety score (0..25) + rug risk (0..100).
Menilai kualitas likuiditas, holder, market cap, dan red flag dasar.
"""
from __future__ import annotations

from ingestion.normalizer import TokenSnapshot

MAX_SAFETY = 25


def rug_risk_score(snap: TokenSnapshot, mint_authority_active: bool = False,
                   top_holder_pct: float = 0.0) -> tuple[int, list[str]]:
    """0 = aman, 100 = sangat berisiko."""
    risk = 0
    flags: list[str] = []
    if snap.liquidity_usd < 5000:
        risk += 30; flags.append("Likuiditas tipis (<$5k)")
    elif snap.liquidity_usd < 15000:
        risk += 15
    if mint_authority_active:
        risk += 25; flags.append("Mint authority aktif")
    if top_holder_pct >= 25:
        risk += 20; flags.append(f"Konsentrasi holder {top_holder_pct:.0f}%")
    if snap.holders and snap.holders < 50:
        risk += 15; flags.append("Holder <50")
    if snap.volume_24h and snap.liquidity_usd:
        if snap.volume_24h > snap.liquidity_usd * 30:
            risk += 10; flags.append("Volume/likuiditas ekstrem (wash?)")
    return min(100, risk), flags


def safety_score(snap: TokenSnapshot, rug_risk: int) -> tuple[int, list[str]]:
    score = 0.0
    notes: list[str] = []
    # likuiditas
    if snap.liquidity_usd >= 100_000:
        score += 10; notes.append("Likuiditas kuat")
    elif snap.liquidity_usd >= 30_000:
        score += 7
    elif snap.liquidity_usd >= 10_000:
        score += 4
    # holders
    if snap.holders >= 1000:
        score += 6
    elif snap.holders >= 300:
        score += 4
    elif snap.holders >= 100:
        score += 2
    # market cap (sweet spot kecil tapi tidak nano)
    if 50_000 <= snap.market_cap <= 5_000_000:
        score += 4; notes.append("MC sweet spot")
    elif snap.market_cap > 5_000_000:
        score += 2
    # penalti rug risk
    score += 5 * (1 - rug_risk / 100.0)
    return int(max(0, min(MAX_SAFETY, round(score)))), notes
