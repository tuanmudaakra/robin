"""
token_caller.py — Mesin klasifikasi inti.

Pipeline:
  recent trades -> grup per token -> skor 4 komponen -> total 0..100
  -> klasifikasi JP / JPJ / WATCH / SKIP (threshold adaptif Darwin)
  -> override rules -> (opsional) AI boost top-N -> daftar call.

Komponen skor (total 100):
  Smart Money (30) | Momentum (25) | Safety (25) | Timing (20)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from config.settings import settings, log
from db import active_wallets, recent_trades, insert_call, get_state
from ingestion.dexscreener import DexScreenerClient
from ingestion.birdeye import BirdeyeClient
from analysis.momentum import momentum_score
from analysis.token_health import rug_risk_score, safety_score
from analysis.ai_layer import AIAnalyzer

SMART_TAGS = ("alpha", "insider", "kol", "smart")


def _now():
    return datetime.now(timezone.utc)


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class TokenCaller:
    def __init__(self):
        self.dex = DexScreenerClient()
        self.birdeye = BirdeyeClient()
        self.ai = AIAnalyzer()

    # --------------------- skor per komponen ---------------------

    def _smart_money(self, trades: list[dict], wallet_meta: dict) -> tuple[int, int, bool, list[str]]:
        buys = [t for t in trades if t.get("trade_type") == "buy"]
        wallets = {t["wallet_address"] for t in buys}
        n = len(wallets)
        score = {1: 3, 2: 6, 3: 9}.get(n, 12 if n >= 4 else 0)

        tags = set()
        for w in wallets:
            tags |= set((wallet_meta.get(w, {}).get("tags") or "").lower().split(","))
        notes = []
        if "alpha" in tags:
            score += 5; notes.append("🅰️ Alpha involved")
        if "insider" in tags:
            score += 4; notes.append("Insider involved")
        if "kol" in tags:
            score += 2; notes.append("KOL involved")

        sol_total = sum(float(t.get("sol_amount") or 0) for t in buys)
        if sol_total >= 20:
            score += 6
        elif sol_total >= 5:
            score += 3

        cluster = n >= 2
        if cluster:
            score += 4
            notes.insert(0, f"🔴 Cluster: {n} wallets")
        return min(30, score), n, cluster, notes

    def _timing(self, trades: list[dict]) -> tuple[int, list[str]]:
        buys = [t for t in trades if t.get("trade_type") == "buy"]
        if not buys:
            return 0, []
        times = sorted(filter(None, (_parse(t.get("timestamp") or t.get("detected_at", "")) for t in buys)))
        if not times:
            return 0, []
        age_min = (_now() - times[-1]).total_seconds() / 60.0
        score, notes = 0, []
        if age_min <= 15:
            score += 10; notes.append("⚡ Recent buys (<15m ago)")
        elif age_min <= 30:
            score += 7
        elif age_min <= 60:
            score += 4
        elif age_min <= 180:
            score += 2
        # frekuensi
        if len(buys) >= 5:
            score += 6
        elif len(buys) >= 3:
            score += 4
        elif len(buys) >= 2:
            score += 2
        # konsistensi window
        if len(times) >= 2 and (times[-1] - times[0]).total_seconds() <= 1800:
            score += 4; notes.append("Akumulasi rapat <30m")
        return min(20, score), notes

    # --------------------- threshold adaptif ---------------------

    def _thresholds(self) -> dict:
        t = get_state("thresholds", {})
        return {
            "jp": int(t.get("jp", 65)),
            "jpj": int(t.get("jpj", 50)),
            "watch": int(t.get("watch", 35)),
        }

    # --------------------- pipeline utama ------------------------

    def run_caller(self, hours_back: int = 6) -> list[dict]:
        wallet_meta = {w["address"]: dict(w) for w in active_wallets()}
        trades = [dict(t) for t in recent_trades(hours_back)]
        by_token: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            if t.get("token_mint"):
                by_token[t["token_mint"]].append(t)

        th = self._thresholds()
        calls: list[dict] = []

        for mint, ttrades in by_token.items():
            snap = self.dex.token_snapshot(mint)
            if snap is None or snap.liquidity_usd < settings.min_liquidity_usd:
                continue
            # enrich holders dari birdeye (opsional)
            ov = self.birdeye.token_overview(mint)
            if ov:
                snap.holders = int(ov.get("holder") or snap.holders)
                snap.market_cap = float(ov.get("mc") or snap.market_cap)

            smart, n_wallets, cluster, smart_notes = self._smart_money(ttrades, wallet_meta)
            mom, mom_notes = momentum_score(snap)
            rug, rug_flags = rug_risk_score(snap)
            safe, safe_notes = safety_score(snap, rug)
            timing, timing_notes = self._timing(ttrades)

            if rug > settings.max_rug_risk_score:
                continue

            total = smart + mom + safe + timing
            call_type, conf = self._classify(total, smart, safe, cluster, timing, th)
            if call_type == "SKIP":
                continue

            symbol = next((t.get("token_symbol") for t in ttrades if t.get("token_symbol")), snap.symbol)
            features = {
                "smart_money_score": smart, "momentum_score": mom,
                "safety_score": safe, "timing_score": timing,
                "rug_risk": rug, "cluster": cluster, "n_wallets": n_wallets,
                "alpha": "🅰️ Alpha involved" in smart_notes,
                "liquidity_usd": snap.liquidity_usd, "market_cap": snap.market_cap,
                "vol_1h": snap.volume_1h, "price_change_1h": snap.price_change_1h,
                "holders": snap.holders,
            }
            calls.append({
                "token_mint": mint,
                "symbol": symbol,
                "call_type": call_type,
                "confidence": conf,
                "price_at_call": snap.price_usd,
                "mc_at_call": snap.market_cap,
                "liquidity_at_call": snap.liquidity_usd,
                "smart_wallet_buys": n_wallets,
                "cluster_buy": 1 if cluster else 0,
                "rug_risk": rug,
                "reason": " | ".join(smart_notes + mom_notes + safe_notes + timing_notes + rug_flags) or "—",
                "features": features,
                "ai_flags": [], "ai_insight": "", "ai_boost": 0,
            })

        calls.sort(key=lambda c: c["confidence"], reverse=True)
        self._apply_ai(calls)
        return calls

    def _classify(self, total, smart, safe, cluster, timing, th) -> tuple[str, int]:
        conf = int(max(0, min(99, total)))
        # override: cluster + alpha + recent -> JP walau total sedang
        if cluster and smart >= 18 and timing >= 8 and total >= 30:
            return "JANGKA_PENDEK", max(conf, 60)
        # override: akumulasi stabil -> JPJ
        if safe >= 15 and smart >= 12 and total >= th["jpj"] - 5:
            if total < th["jp"]:
                return "JANGKA_PANJANG", conf
        if total >= th["jp"]:
            return "JANGKA_PENDEK", conf
        if total >= th["jpj"]:
            return "JANGKA_PANJANG", conf
        if total >= th["watch"]:
            return "WATCH", conf
        return "SKIP", conf

    def _apply_ai(self, calls: list[dict]) -> None:
        if not self.ai.enabled:
            return
        for call in calls[: settings.llm_top_n]:
            res = self.ai.analyze_token(call)
            boost = int(res.get("boost", 0))
            call["ai_boost"] = boost
            call["confidence"] = int(max(0, min(99, call["confidence"] + boost)))
            call["ai_flags"] = res.get("flags", [])
            call["ai_insight"] = res.get("insight", "")
            # AI boleh override WATCH -> JP/JPJ kalau rekomendasi kuat
            if call["call_type"] == "WATCH" and res.get("recommendation") == "BOOST" and boost >= 5:
                call["call_type"] = "JANGKA_PANJANG"

    # --------------------- persist call --------------------------

    def record_calls(self, calls: list[dict]) -> int:
        """Simpan call JP/JPJ ke call_history dengan period_end_at sesuai horizon."""
        from datetime import timedelta
        now = _now()
        saved = 0
        for c in calls:
            if c["call_type"] not in ("JANGKA_PENDEK", "JANGKA_PANJANG"):
                continue
            horizon = (settings.jp_horizon_hours if c["call_type"] == "JANGKA_PENDEK"
                       else settings.jpj_horizon_hours)
            cid = insert_call({
                **c,
                "token_symbol": c.get("symbol"),   # kolom DB = token_symbol
                "called_at": now.isoformat(),
                "period_end_at": (now + timedelta(hours=horizon)).isoformat(),
                "outcome": "PENDING",
            })
            if cid:
                c["call_id"] = cid
                saved += 1
        log.info("record_calls: %d call baru disimpan", saved)
        return saved
