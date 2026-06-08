"""
helius.py — Client Helius Enhanced API (parsed transactions, metadata, RPC).

Catatan: bentuk respons Helius bisa berubah; parsing dibuat defensif.
"""
from __future__ import annotations

from typing import Optional

from config.settings import settings
from ingestion.base import BaseAPIClient
from ingestion.normalizer import WalletTrade

WSOL = "So11111111111111111111111111111111111111112"


class HeliusClient(BaseAPIClient):
    def __init__(self):
        super().__init__("https://api.helius.xyz/v0", rate_per_sec=8.0)
        self.api_key = settings.helius_api_key
        self.rpc_url = settings.rpc_url

    def _key_params(self, extra: Optional[dict] = None) -> dict:
        p = {"api-key": self.api_key}
        if extra:
            p.update(extra)
        return p

    def get_transactions(self, address: str, limit: int = 50) -> list[dict]:
        if not self.api_key:
            return []
        res = self.get(f"/addresses/{address}/transactions",
                       self._key_params({"limit": limit}))
        return res or []

    def parse_swaps(self, address: str, label: str = "", limit: int = 50) -> list[WalletTrade]:
        """Ubah transaksi mentah Helius jadi WalletTrade buy/sell (best-effort)."""
        trades: list[WalletTrade] = []
        for tx in self.get_transactions(address, limit):
            sig = tx.get("signature")
            ts = tx.get("timestamp")
            source = (tx.get("source") or "").lower()
            transfers = tx.get("tokenTransfers") or []
            for tr in transfers:
                mint = tr.get("mint")
                if not mint or mint == WSOL:
                    continue
                to_user = tr.get("toUserAccount") == address
                from_user = tr.get("fromUserAccount") == address
                if not (to_user or from_user):
                    continue
                amount = float(tr.get("tokenAmount") or 0)
                sol = self._sol_delta(tx, address)
                trades.append(WalletTrade(
                    wallet_address=address,
                    wallet_label=label,
                    tx_signature=sig,
                    timestamp=self._iso(ts),
                    trade_type="buy" if to_user else "sell",
                    token_mint=mint,
                    sol_amount=abs(sol),
                    program=source or "unknown",
                    token_amount=amount,
                ))
        return trades

    @staticmethod
    def _sol_delta(tx: dict, address: str) -> float:
        for nt in tx.get("nativeTransfers") or []:
            if nt.get("fromUserAccount") == address:
                return -float(nt.get("amount", 0)) / 1e9
            if nt.get("toUserAccount") == address:
                return float(nt.get("amount", 0)) / 1e9
        return 0.0

    @staticmethod
    def _iso(ts) -> str:
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return ""

    def token_supply(self, mint: str) -> Optional[dict]:
        """RPC getTokenSupply + mint authority via getAccountInfo (best-effort)."""
        if not self.rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
        res = self.post(self.rpc_url, body=body)
        if res and "result" in res:
            return res["result"]
        return None
