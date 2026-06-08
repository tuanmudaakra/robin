"""
dexscreener.py — Client DexScreener (tanpa auth).
Dipakai untuk snapshot token + daftar trending/boosts (Discovery).
"""
from __future__ import annotations

from typing import Optional

from ingestion.base import BaseAPIClient
from ingestion.normalizer import TokenSnapshot


class DexScreenerClient(BaseAPIClient):
    def __init__(self):
        super().__init__("https://api.dexscreener.com", rate_per_sec=4.0)

    def token_snapshot(self, mint: str) -> Optional[TokenSnapshot]:
        res = self.get(f"/latest/dex/tokens/{mint}")
        pairs = (res or {}).get("pairs") or []
        if not pairs:
            return None
        # ambil pair dgn likuiditas terbesar
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        return self._to_snapshot(mint, best)

    @staticmethod
    def _to_snapshot(mint: str, p: dict) -> TokenSnapshot:
        liq = (p.get("liquidity") or {})
        vol = (p.get("volume") or {})
        chg = (p.get("priceChange") or {})
        txns = (p.get("txns") or {}).get("h1") or {}
        base = p.get("baseToken") or {}
        return TokenSnapshot(
            mint=mint,
            symbol=base.get("symbol", "?"),
            name=base.get("name", ""),
            price_usd=float(p.get("priceUsd") or 0),
            market_cap=float(p.get("marketCap") or p.get("fdv") or 0),
            liquidity_usd=float(liq.get("usd") or 0),
            volume_24h=float(vol.get("h24") or 0),
            volume_1h=float(vol.get("h1") or 0),
            price_change_1h=float(chg.get("h1") or 0),
            price_change_24h=float(chg.get("h24") or 0),
            pair_created_at=str(p.get("pairCreatedAt") or ""),
            txns_1h_buys=int(txns.get("buys") or 0),
            txns_1h_sells=int(txns.get("sells") or 0),
            source="dexscreener",
        )

    def trending(self) -> list[dict]:
        """Token-boosts top = proxy 'trending' di DexScreener."""
        res = self.get("/token-boosts/top/v1")
        return res or []

    def latest_profiles(self) -> list[dict]:
        res = self.get("/token-profiles/latest/v1")
        return res or []
