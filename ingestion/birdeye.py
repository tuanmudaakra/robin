"""
birdeye.py — Client Birdeye public API.
Dipakai untuk overview token, OHLCV, dan EARLY/TOP TRADERS (Discovery).
"""
from __future__ import annotations

from typing import Optional

from config.settings import settings
from ingestion.base import BaseAPIClient


class BirdeyeClient(BaseAPIClient):
    def __init__(self):
        super().__init__(
            "https://public-api.birdeye.so",
            rate_per_sec=1.0,
            default_headers={"x-api-key": settings.birdeye_api_key, "x-chain": "solana"},
        )
        self.enabled = bool(settings.birdeye_api_key)

    def token_overview(self, mint: str) -> Optional[dict]:
        if not self.enabled:
            return None
        res = self.get("/defi/token_overview", {"address": mint})
        return (res or {}).get("data")

    def top_traders(self, mint: str, limit: int = 30) -> list[dict]:
        """Top/early traders untuk token — bahan utama Wallet Discovery."""
        if not self.enabled:
            return []
        res = self.get("/defi/v2/tokens/top_traders",
                       {"address": mint, "limit": limit, "sort_by": "volume",
                        "sort_type": "desc"})
        data = (res or {}).get("data") or {}
        return data.get("items") or []

    def token_txs(self, mint: str, limit: int = 50, offset: int = 0) -> list[dict]:
        if not self.enabled:
            return []
        res = self.get("/defi/txs/token",
                       {"address": mint, "limit": limit, "offset": offset,
                        "tx_type": "swap"})
        data = (res or {}).get("data") or {}
        return data.get("items") or []

    def wallet_token_list(self, wallet: str) -> list[dict]:
        if not self.enabled:
            return []
        res = self.get("/v1/wallet/token_list", {"wallet": wallet})
        data = (res or {}).get("data") or {}
        return data.get("items") or []
