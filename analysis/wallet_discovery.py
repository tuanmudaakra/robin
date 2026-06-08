"""
wallet_discovery.py — Smart Wallet Discovery / Auto-Update Engine (v2.1).

Membalik arah analisis: dari token OUTSTANDING -> cari DOMPET yang menggerakkannya
sejak awal -> promosikan kandidat terbaik ke registry, pensiunkan yang buruk.

Alur:
  1. KUMPULKAN outstanding token (DexScreener trending + call_history HIT)
  2. TELUSURI early/top buyers tiap token (Birdeye)
  3. SKOR Discovery Score 0..100 (recurrence/earliness/hit_rate/roi)
  4. DEDUP vs registry -> akumulasi ke wallet_candidates
  5. PROMOTE hybrid (>=auto: langsung; min..auto: approval Telegram)
  6. DEMOTE dompet pasif/jelek (kecuali tag manual/core)
  7. REPORT
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from config.settings import settings, log
import db
from ingestion.dexscreener import DexScreenerClient
from ingestion.birdeye import BirdeyeClient

PROTECTED_TAGS = ("manual", "core")


def _now():
    return datetime.now(timezone.utc)


class WalletDiscovery:
    def __init__(self):
        self.dex = DexScreenerClient()
        self.birdeye = BirdeyeClient()

    # ----------------- 1. outstanding tokens -----------------

    def find_outstanding_tokens(self) -> list[dict]:
        seen: dict[str, dict] = {}

        # a) DexScreener trending/boosts
        for item in self.dex.trending():
            mint = item.get("tokenAddress") or item.get("address")
            if not mint:
                continue
            snap = self.dex.token_snapshot(mint)
            if not snap:
                continue
            if (snap.liquidity_usd >= settings.outstanding_min_liquidity_usd and
                    snap.price_change_24h >= settings.outstanding_min_pump_pct):
                seen[mint] = {"mint": mint, "symbol": snap.symbol,
                              "pump_pct": snap.price_change_24h, "via": "trending"}

        # b) winner internal (call HIT)
        with db.get_conn() as conn:
            hit_tokens = conn.execute(
                "SELECT DISTINCT token_mint, token_symbol FROM call_history WHERE outcome='HIT'"
            ).fetchall()
        for r in hit_tokens:
            mint = r["token_mint"]
            if mint and mint not in seen:
                seen[mint] = {"mint": mint, "symbol": r["token_symbol"],
                              "pump_pct": None, "via": "hit_history"}

        log.info("discovery: %d outstanding token", len(seen))
        return list(seen.values())

    # ----------------- 2. early buyers -----------------

    def early_buyers(self, mint: str) -> list[str]:
        """Top/early trader sebuah token (kandidat penggerak)."""
        addrs: list[str] = []
        for t in self.birdeye.top_traders(mint, limit=30):
            a = t.get("owner") or t.get("address") or t.get("wallet")
            if a:
                addrs.append(a)
        if not addrs:  # fallback ke tx swap kalau top_traders kosong
            for tx in self.birdeye.token_txs(mint, limit=50):
                a = tx.get("owner") or tx.get("from") or (tx.get("from", {}) or {}).get("owner")
                if a:
                    addrs.append(a)
        return addrs

    # ----------------- 3-4. skor & akumulasi -----------------

    def run_discovery(self) -> dict:
        if not settings.discovery_enabled:
            return {"enabled": False}

        outstanding = self.find_outstanding_tokens()
        existing = {w["address"] for w in db.active_wallets()}

        # akumulasi kemunculan wallet lintas winner token
        appear: dict[str, dict] = defaultdict(
            lambda: {"winners": set(), "samples": [], "rank_sum": 0, "appearances": 0})
        analyzed = 0
        for tok in outstanding:
            buyers = self.early_buyers(tok["mint"])
            analyzed += len(buyers)
            for rank, addr in enumerate(buyers):
                a = appear[addr]
                a["winners"].add(tok["mint"])
                a["appearances"] += 1
                a["rank_sum"] += rank
                if len(a["samples"]) < 5 and tok.get("symbol"):
                    a["samples"].append(tok["symbol"])

        promoted, pending, kept = [], [], 0
        for addr, info in appear.items():
            if addr in existing:
                continue
            n_winners = len(info["winners"])
            if n_winners < settings.candidate_min_winners:
                continue
            score, parts = self._score(info, len(outstanding))
            row = {
                "address": addr, "discovery_score": score,
                "winner_tokens": n_winners,
                "earliness_avg": parts["earliness"],
                "hit_rate": parts["hit_rate"],
                "est_roi": parts.get("roi", 0.0),
                "sample_tokens": info["samples"],
            }
            if score >= settings.auto_promote_score:
                db.upsert_wallet(addr, label=f"disc_{addr[:4]}", tags="discovered",
                                 source="discovered", discovery_score=score)
                row["status"] = "PROMOTED"
                db.upsert_candidate(row)
                db.set_candidate_status(addr, "PROMOTED")
                promoted.append(row)
            elif score >= settings.approval_score_min:
                row["status"] = "PENDING"
                db.upsert_candidate(row)
                pending.append(row)
            else:
                row["status"] = "PENDING"
                db.upsert_candidate(row)
                kept += 1

        demoted = self._auto_demote()
        report = {
            "enabled": True, "outstanding": len(outstanding), "analyzed": analyzed,
            "promoted": promoted, "pending": pending, "kept": kept, "demoted": demoted,
            "active_after": db.count_active_wallets(),
        }
        log.info("discovery done: +%d promoted, %d pending, -%d demoted",
                 len(promoted), len(pending), len(demoted))
        return report

    def _score(self, info: dict, n_outstanding: int) -> tuple[int, dict]:
        n_winners = len(info["winners"])
        appearances = max(info["appearances"], 1)

        # Recurrence (35): muncul di banyak winner berbeda
        recurrence = min(35, 12 * n_winners)
        # Earliness (30): rank rata-rata makin kecil (atas) makin awal
        avg_rank = info["rank_sum"] / appearances
        earliness_norm = max(0.0, 1 - avg_rank / 30.0)  # 0..1
        earliness = round(30 * earliness_norm)
        # Hit rate (20): proporsi kemunculan yang di winner
        hit_rate = round(n_winners / appearances, 2)
        hit_pts = round(20 * min(1.0, hit_rate))
        # ROI placeholder (15): butuh data PnL realisasi; pakai proxy recurrence
        roi_pts = min(15, 5 * n_winners)

        score = int(min(100, recurrence + earliness + hit_pts + roi_pts))
        return score, {"earliness": round(earliness_norm, 2), "hit_rate": hit_rate,
                       "roi": float(roi_pts)}

    # ----------------- 6. auto-demote -----------------

    def _auto_demote(self) -> list[dict]:
        demoted = []
        with db.get_conn() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM wallets WHERE is_active=1")]
        for w in rows:
            tags = (w.get("tags") or "").lower()
            if any(p in tags for p in PROTECTED_TAGS):
                continue
            reason = None
            wr = w.get("win_rate")
            if wr is not None and w.get("total_trades", 0) >= 3 and wr < settings.demote_winrate_pct:
                reason = f"WR {wr:.0f}%"
            elif self._inactive_days(w) > settings.demote_inactive_days:
                reason = f"{self._inactive_days(w)} hari pasif"
            if reason:
                db.set_wallet_active(w["address"], False)
                demoted.append({"address": w["address"], "label": w.get("label"),
                                "reason": reason})

        # cap registry
        over = db.count_active_wallets() - settings.max_active_wallets
        if over > 0:
            with db.get_conn() as conn:
                worst = conn.execute(
                    """SELECT address, label, win_rate FROM wallets
                       WHERE is_active=1 AND COALESCE(tags,'') NOT LIKE '%core%'
                         AND COALESCE(tags,'') NOT LIKE '%manual%'
                       ORDER BY COALESCE(win_rate, 0) ASC, COALESCE(discovery_score,0) ASC
                       LIMIT ?""", (over,)).fetchall()
            for w in worst:
                db.set_wallet_active(w["address"], False)
                demoted.append({"address": w["address"], "label": w["label"],
                                "reason": "cap registry"})
        return demoted

    def _inactive_days(self, w: dict) -> int:
        last = w.get("last_scan_at") or w.get("added_at")
        try:
            dt = datetime.fromisoformat((last or "").replace("Z", "+00:00"))
            return int((_now() - dt).total_seconds() / 86400)
        except (ValueError, AttributeError):
            return 0


def approve_candidate(address: str) -> bool:
    cand = db.get_candidate(address)
    if not cand:
        return False
    db.upsert_wallet(address, label=f"disc_{address[:4]}", tags="discovered",
                     source="discovered", discovery_score=cand["discovery_score"])
    db.set_candidate_status(address, "APPROVED")
    return True


def reject_candidate(address: str) -> bool:
    if not db.get_candidate(address):
        return False
    db.set_candidate_status(address, "REJECTED")
    return True
