"""
learning.py — Darwin Learning Engine + Auto Lesson-Learn.

Alur yang diminta:
  1. Tiap call tercatat sejak WAKTU CALL PERTAMA (call_history.called_at, price_at_call).
  2. Sepanjang periode, harga ditrack: peak / trough / current (update_outcomes).
  3. Di AKHIR PERIODE (period_end_at), outcome difinalkan (HIT/MISS/UNKNOWN)
     berdasar harga akhir + peak/trough.
  4. LESSON-LEARN: tiap call yang selesai dipelajari —
       - kalau RUGI  -> salahnya di mana (entry/timing/likuiditas/sinyal palsu)
       - kalau UNTUNG -> kelebihannya di mana (cluster/alpha/timing/safety)
     Rule-based + (opsional) post-mortem LLM.
  5. Agregat -> sesuaikan threshold adaptif + statistik untuk report & discovery.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from config.settings import settings, log
import db
from ingestion.dexscreener import DexScreenerClient
from analysis.ai_layer import AIAnalyzer


def _now():
    return datetime.now(timezone.utc)


def _parse(ts: str):
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _pct(new: float, old: float) -> float:
    if not old:
        return 0.0
    return (new - old) / old * 100.0


class Darwin:
    def __init__(self):
        self.dex = DexScreenerClient()
        self.ai = AIAnalyzer()
        self.hit = settings.hit_threshold_pct
        self.miss = settings.miss_threshold_pct

    # ============== 1. tracking harga sepanjang periode ==============

    def update_outcomes(self) -> dict:
        """Track harga semua call PENDING; finalkan yang sudah lewat period_end_at."""
        finalized, tracked = 0, 0
        for row in db.open_calls():
            call = dict(row)
            snap = self.dex.token_snapshot(call["token_mint"])
            price_now = snap.price_usd if snap else None
            if not price_now or not call.get("price_at_call"):
                # tak ada harga -> cek apakah sudah lewat periode utk ditandai UNKNOWN
                if self._past_period(call):
                    db.update_call_tracking(call["id"], {"outcome": "UNKNOWN",
                                                         "hold_hours": self._hold_hours(call)})
                    finalized += 1
                continue

            pct = _pct(price_now, call["price_at_call"])
            fields = {
                "price_now": price_now,
                "price_change_pct": round(pct, 2),
                "hold_hours": self._hold_hours(call),
            }
            # peak / trough
            peak = call.get("peak_pct")
            if peak is None or pct > peak:
                fields["peak_pct"] = round(pct, 2)
                fields["peak_price"] = price_now
            trough = call.get("trough_pct")
            if trough is None or pct < trough:
                fields["trough_pct"] = round(pct, 2)
                fields["trough_price"] = price_now
            tracked += 1

            if self._past_period(call):
                merged = {**call, **fields}
                outcome = self._decide_outcome(merged)
                fields["final_price"] = price_now
                fields["final_pct"] = round(pct, 2)
                fields["outcome"] = outcome
                db.update_call_tracking(call["id"], fields)
                self._learn_from_call({**merged, **fields})
                finalized += 1
            else:
                db.update_call_tracking(call["id"], fields)

        log.info("update_outcomes: %d tracked, %d finalized", tracked, finalized)
        return {"tracked": tracked, "finalized": finalized}

    def _past_period(self, call: dict) -> bool:
        end = _parse(call.get("period_end_at", ""))
        return bool(end and _now() >= end)

    def _hold_hours(self, call: dict) -> int:
        start = _parse(call.get("called_at", ""))
        if not start:
            return 0
        return int((_now() - start).total_seconds() / 3600)

    def _decide_outcome(self, c: dict) -> str:
        peak = c.get("peak_pct") or 0.0
        trough = c.get("trough_pct") or 0.0
        final = c.get("final_pct") or 0.0
        hit_target = peak >= self.hit
        stopped = trough <= self.miss
        if hit_target and not stopped:
            return "HIT"
        if stopped and not hit_target:
            return "MISS"
        if hit_target and stopped:                # dua-duanya kena -> pakai harga akhir
            return "HIT" if final >= 0 else "MISS"
        if final >= self.hit:
            return "HIT"
        if final <= self.miss:
            return "MISS"
        return "UNKNOWN"                           # flat

    # ================== 4. lesson-learn per call =====================

    def _learn_from_call(self, c: dict) -> None:
        outcome = c.get("outcome")
        if outcome not in ("HIT", "MISS"):
            return
        feats = c.get("features")
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except json.JSONDecodeError:
                feats = {}
        feats = feats or {}

        summary, factors = self._rule_lesson(c, feats, outcome)
        source = "rule"
        # post-mortem LLM (opsional) -> lebih kaya
        if self.ai.enabled:
            ai = self.ai.analyze_call_outcome({**c, "features": feats})
            if ai.get("summary"):
                summary = ai["summary"]
                factors = ai.get("factors") or factors
                source = "llm"

        db.insert_lesson(
            call_id=c.get("id"), symbol=c.get("token_symbol", "?"),
            outcome=outcome, kind="per_call", summary=summary,
            detail={"factors": factors, "final_pct": c.get("final_pct"),
                    "peak_pct": c.get("peak_pct"), "trough_pct": c.get("trough_pct")},
            source=source)
        log.info("lesson[%s] %s: %s", outcome, c.get("token_symbol"), summary)

    def _rule_lesson(self, c: dict, f: dict, outcome: str) -> tuple[str, list[str]]:
        final = c.get("final_pct") or 0.0
        peak = c.get("peak_pct") or 0.0
        trough = c.get("trough_pct") or 0.0
        factors: list[str] = []

        if outcome == "MISS":
            # cari KESALAHAN
            if (f.get("liquidity_usd") or 0) < 15000:
                factors.append("Likuiditas tipis → rawan slippage/rug")
            if not f.get("cluster"):
                factors.append("Solo buy, tanpa konfirmasi cluster")
            if (f.get("timing_score") or 0) < 8:
                factors.append("Entry telat — momentum sudah lewat")
            if (f.get("momentum_score") or 0) >= 15 and final < 0:
                factors.append("Momentum palsu (pump & dump)")
            if (f.get("rug_risk") or 0) >= 50:
                factors.append("Rug risk tinggi diabaikan")
            if trough <= self.miss:
                factors.append(f"Cut loss kena di {trough:.0f}%")
            if not factors:
                factors.append("Sinyal lemah / pasar sepi")
            summary = f"MISS {final:+.0f}% — {factors[0]}"
        else:  # HIT
            n = f.get("n_wallets") or 0
            if f.get("cluster"):
                factors.append(f"Cluster {n} wallet = sinyal kuat")
            if f.get("alpha"):
                factors.append("Alpha wallet masuk awal")
            if (f.get("timing_score") or 0) >= 8:
                factors.append("Entry cepat (<15m)")
            if (f.get("safety_score") or 0) >= 15:
                factors.append("Token sehat (likuiditas/holder bagus)")
            if peak >= self.hit * 3:
                factors.append(f"Sempat +{peak:.0f}% (runner)")
            if not factors:
                factors.append("Eksekusi tepat waktu")
            summary = f"HIT {final:+.0f}% — {factors[0]}"
        return summary, factors

    # ================== 5. agregat + adaptif =========================

    def analyze(self) -> dict:
        rows = self._finalized_calls()
        stats = self._aggregate(rows)
        self._adapt_thresholds(stats)
        self._recompute_wallet_stats(rows)
        self._store_aggregate_lesson(stats)
        return stats

    def _finalized_calls(self) -> list[dict]:
        with db.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM call_history WHERE outcome IN ('HIT','MISS')")]

    def _aggregate(self, rows: list[dict]) -> dict:
        def wr(items):
            h = sum(1 for r in items if r["outcome"] == "HIT")
            t = len(items)
            return (round(h / t * 100, 1) if t else 0.0), h, t

        hits = [r for r in rows if r["outcome"] == "HIT"]
        miss = [r for r in rows if r["outcome"] == "MISS"]
        overall_wr, h, t = wr(rows)

        def feat(r, k):
            f = r.get("features")
            if isinstance(f, str):
                try:
                    f = json.loads(f)
                except json.JSONDecodeError:
                    f = {}
            return (f or {}).get(k)

        cluster = [r for r in rows if feat(r, "cluster")]
        solo = [r for r in rows if not feat(r, "cluster")]
        jp = [r for r in rows if r["call_type"] == "JANGKA_PENDEK"]
        jpj = [r for r in rows if r["call_type"] == "JANGKA_PANJANG"]

        avg_hit = round(sum((r.get("final_pct") or 0) for r in hits) / len(hits), 1) if hits else 0.0
        avg_miss = round(sum((r.get("final_pct") or 0) for r in miss) / len(miss), 1) if miss else 0.0

        return {
            "total": t, "hits": h, "miss": len(miss), "win_rate": overall_wr,
            "avg_hit_pct": avg_hit, "avg_miss_pct": avg_miss,
            "cluster_wr": wr(cluster)[0], "cluster_n": len(cluster),
            "solo_wr": wr(solo)[0], "solo_n": len(solo),
            "jp_wr": wr(jp)[0], "jp_n": len(jp),
            "jpj_wr": wr(jpj)[0], "jpj_n": len(jpj),
        }

    def _adapt_thresholds(self, stats: dict) -> None:
        th = db.get_state("thresholds", {"jp": 65, "jpj": 50, "watch": 35})
        # JP: kalau WR rendah -> lebih pemilih (naik); kalau tinggi -> lebih sering (turun)
        if stats["jp_n"] >= 8:
            if stats["jp_wr"] < 45:
                th["jp"] = min(75, th["jp"] + 2)
            elif stats["jp_wr"] > 65:
                th["jp"] = max(50, th["jp"] - 2)
        if stats["jpj_n"] >= 8:
            if stats["jpj_wr"] < 45:
                th["jpj"] = min(65, th["jpj"] + 2)
            elif stats["jpj_wr"] > 65:
                th["jpj"] = max(35, th["jpj"] - 2)
        db.set_state("thresholds", th)
        db.set_state("cluster_edge", {"cluster_wr": stats["cluster_wr"],
                                      "solo_wr": stats["solo_wr"]})
        log.info("adapt: thresholds=%s", th)

    def _recompute_wallet_stats(self, rows: list[dict]) -> None:
        """Kredit HIT/MISS ke wallet yang membeli token tsb (feed Discovery)."""
        outcome_by_token: dict[str, list[dict]] = {}
        for r in rows:
            outcome_by_token.setdefault(r["token_mint"], []).append(r)
        if not outcome_by_token:
            return
        # Fase 1: baca semua data dalam satu koneksi, kumpulkan update.
        updates: list[tuple] = []
        with db.get_conn() as conn:
            wallets = [w["address"] for w in conn.execute(
                "SELECT address FROM wallets WHERE is_active=1")]
            for addr in wallets:
                toks = {x["token_mint"] for x in conn.execute(
                    "SELECT DISTINCT token_mint FROM wallet_trades "
                    "WHERE wallet_address=? AND trade_type='buy'", (addr,))}
                wins = losses = 0
                rois: list[float] = []
                for tk in toks:
                    for c in outcome_by_token.get(tk, []):
                        if c["outcome"] == "HIT":
                            wins += 1
                        elif c["outcome"] == "MISS":
                            losses += 1
                        if c.get("final_pct") is not None:
                            rois.append(c["final_pct"])
                tot = wins + losses
                if tot:
                    updates.append((
                        addr, round(wins / tot * 100, 1),
                        round(sum(rois) / len(rois), 1) if rois else 0.0, tot))
        # Fase 2: tulis setelah koneksi baca ditutup (hindari lock saat WAL).
        for addr, wr, roi, tot in updates:
            db.update_wallet_stats(addr, wr, roi, tot)

    def _store_aggregate_lesson(self, stats: dict) -> None:
        msgs = []
        if stats["cluster_n"] and stats["solo_n"]:
            if stats["cluster_wr"] > stats["solo_wr"] + 10:
                msgs.append(f"Cluster jauh lebih unggul ({stats['cluster_wr']}% vs {stats['solo_wr']}%) — prioritaskan cluster")
        if stats["jp_n"] >= 8 and stats["jp_wr"] < 45:
            msgs.append(f"JP win rate rendah ({stats['jp_wr']}%) — threshold dinaikkan")
        if stats["avg_miss_pct"] and stats["avg_hit_pct"]:
            msgs.append(f"Avg HIT {stats['avg_hit_pct']:+.0f}% vs MISS {stats['avg_miss_pct']:+.0f}%")
        if msgs:
            db.insert_lesson(None, "ALL", "AGG", "aggregate",
                             " · ".join(msgs), {"stats": stats}, "rule")

    # ===================== report Telegram ===========================

    def build_report(self) -> str:
        stats = self.analyze()
        cs = db.call_stats()
        th = db.get_state("thresholds", {"jp": 65, "jpj": 50})
        lines = [
            "🧬 Darwin Learning Report", "",
            f"📊 {cs.get('total', 0)} calls | {stats['total']} checked | {stats['win_rate']}% WR",
            f"   ✅ HIT: {stats['hits']} | ❌ MISS: {stats['miss']} | ⏳ Pending: {cs.get('PENDING', 0)}",
            "",
            f"📈 Avg HIT: {stats['avg_hit_pct']:+.1f}%",
            f"📉 Avg MISS: {stats['avg_miss_pct']:+.1f}%",
            "",
            "⚙️ Self-Adjustments:",
            f"   📏 Threshold JP: {th['jp']} | JPJ: {th['jpj']}",
            f"   🔗 Cluster WR {stats['cluster_wr']}% vs Solo {stats['solo_wr']}%",
            "",
            "💡 Lessons Learned:",
            f"   JANGKA_PENDEK: {stats['jp_wr']}% WR ({stats['jp_n']})",
            f"   JANGKA_PANJANG: {stats['jpj_wr']}% WR ({stats['jpj_n']})",
        ]
        recent = db.recent_lessons(limit=3, kind="per_call")
        if recent:
            lines += ["", "🕐 Pelajaran terakhir:"]
            for l in recent:
                icon = "✅" if l["outcome"] == "HIT" else "❌"
                lines.append(f"   {icon} {l['token_symbol']}: {l['summary']}")
        return "\n".join(lines)
