"""
normalizer.py — Struktur data ternormalisasi lintas sumber API.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class TokenSnapshot:
    """Snapshot satu token, gabungan DexScreener + Birdeye."""
    mint: str
    symbol: str = "?"
    name: str = ""
    price_usd: float = 0.0
    market_cap: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    volume_1h: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    holders: int = 0
    pair_created_at: Optional[str] = None
    txns_1h_buys: int = 0
    txns_1h_sells: int = 0
    source: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def has_price(self) -> bool:
        return self.price_usd > 0


@dataclass
class WalletTrade:
    """Normalisasi satu transaksi wallet (buy/sell)."""
    wallet_address: str
    tx_signature: str
    timestamp: str
    trade_type: str          # buy / sell
    token_mint: str
    token_symbol: str = "?"
    token_amount: float = 0.0
    sol_amount: float = 0.0
    usd_value: float = 0.0
    price_at_trade: float = 0.0
    program: str = ""
    wallet_label: str = ""

    def as_dict(self) -> dict:
        return asdict(self)
