#!/usr/bin/env python3
"""
main.py — Entry point Robin.

Mode:
  python main.py --once         # 1x siklus scan token (ingest -> call -> learn -> output)
  python main.py --discover     # 1x siklus wallet discovery
  python main.py --cron --interval 600   # loop produksi (scan tiap interval, discovery tiap 6j)
  python main.py --selftest     # cek import + init DB tanpa jaringan

Dua loop:
  - scan token       : tiap `scan_interval_min` (default 10 menit)
  - wallet discovery : tiap `discovery_interval_hours` (default 6 jam)
Telegram poller jalan di thread terpisah (2 detik).
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from config.settings import settings, log
import db
from ingestion.helius import HeliusClient
from analysis.token_caller import TokenCaller
from analysis.wallet_discovery import WalletDiscovery, approve_candidate, reject_candidate
from learning import Darwin
from alerts import telegram_bot as tg
from alerts.telegram_handler import TelegramPoller


def _now():
    return datetime.now(timezone.utc)


# ----------------------- ingestion -----------------------

def ingest_wallet_trades() -> int:
    """Tarik transaksi smart wallet via Helius, simpan yang baru."""
    helius = HeliusClient()
    new = 0
    for w in db.active_wallets():
        trades = helius.parse_swaps(w["address"], w["label"] or "",
                                    limit=settings.max_tx_per_scan)
        for tr in trades:
            row = tr.as_dict()
            row["detected_at"] = _now().isoformat()
            if db.insert_trade(row):
                new += 1
    log.info("ingest: %d trade baru", new)
    return new


# ----------------------- formatting -----------------------

def _fmt_price(p) -> str:
    """Format harga memecoin: hindari scientific notation (e.g. 6e-05 -> 0.000061)."""
    if not p:
        return "$?"
    p = float(p)
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.001:
        return f"${p:.6f}"
    # harga sangat kecil: cukup 4 angka penting
    import math
    digits = max(4, -int(math.floor(math.log10(p))) + 3)
    return f"${p:.{digits}f}"


def format_calls_telegram(calls: list[dict]) -> str:
    jp = [c for c in calls if c["call_type"] == "JANGKA_PENDEK"]
    jpj = [c for c in calls if c["call_type"] == "JANGKA_PANJANG"]
    if not jp and not jpj:
        return "📞 Token Caller\n\nBelum ada call kuat saat ini. 😴"

    lines = ["📞 *Token Caller*", ""]

    def block(items, header):
        if not items:
            return
        lines.append(header)
        for c in items:
            ai = f" 🚀(+{c['ai_boost']} AI)" if c.get("ai_boost") else ""
            lines.append(f"  $`{c['symbol']}` · {_fmt_price(c['price_at_call'])} | "
                         f"conf={c['confidence']}{ai} | {c['smart_wallet_buys']}wl")
            lines.append(f"     `{c['token_mint']}`")
            lines.append(f"     [{c['call_type']}] {c['reason']}")
            if c.get("ai_insight"):
                lines.append(f"     🤖 {c['ai_insight']}")
            if c.get("ai_flags"):
                lines.append("     🏷 " + " ".join(f"#{f}" for f in c["ai_flags"]))
        lines.append("")

    block(jp, f"⚡ *JANGKA PENDEK* ({len(jp)}) — Scalp 5m–2j")
    block(jpj, f"🐢 *JANGKA PANJANG* ({len(jpj)}) — Swing 4j–7h")
    lines.append(f"🔄 Update: {_now().strftime('%H:%M')}")
    return "\n".join(lines)


def format_discovery_report(r: dict) -> str:
    if not r.get("enabled"):
        return "🔍 Discovery dinonaktifkan (discovery_enabled=false)."
    lines = ["🔍 *Wallet Discovery Report*", "",
             f"📡 Scan: {r['outstanding']} outstanding token | {r['analyzed']} early buyer dianalisis", ""]
    if r["promoted"]:
        lines.append(f"⚡ *AUTO-PROMOTED* ({len(r['promoted'])}):")
        for w in r["promoted"]:
            lines.append(f"   • `{w['address']}` score={w['discovery_score']} | "
                         f"{w['winner_tokens']} winners")
        lines.append("")
    if r["pending"]:
        lines.append(f"📩 *BUTUH APPROVAL* ({len(r['pending'])}): cek kartu approval ↓")
        for w in r["pending"]:
            lines.append(f"   • `{w['address']}` score={w['discovery_score']} | "
                         f"{','.join(w['sample_tokens'])}")
        lines.append("")
    if r["demoted"]:
        lines.append(f"💤 *DEMOTED* ({len(r['demoted'])}):")
        for w in r["demoted"]:
            lines.append(f"   • `{w['address']}` {w['reason']} → nonaktif")
        lines.append("")
    lines.append(f"📊 Registry: {r['active_after']} aktif | {r['kept']} kandidat pending")
    return "\n".join(lines)


def send_approval_cards(pending: list[dict]) -> None:
    for w in pending:
        text = (f"📩 *Kandidat Dompet Baru*\n`{w['address']}`\n"
                f"score={w['discovery_score']} · {w['winner_tokens']} winners\n"
                f"{', '.join(w['sample_tokens'])}")
        tg.send_message(text, reply_markup=tg.approval_keyboard(w["address"]))


# ----------------------- siklus -----------------------

def run_scan_cycle(send_telegram: bool = True) -> list[dict]:
    ingest_wallet_trades()
    darwin = Darwin()
    darwin.update_outcomes()                 # track + finalize + lesson-learn
    caller = TokenCaller()
    calls = caller.run_caller(hours_back=6)
    caller.record_calls(calls)
    if send_telegram:
        tg.send_message(format_calls_telegram(calls), reply_markup=tg.main_keyboard())
        tg.send_message(darwin.build_report())
    return calls


def run_discovery_cycle(send_telegram: bool = True) -> dict:
    report = WalletDiscovery().run_discovery()
    if send_telegram and report.get("enabled"):
        tg.send_message(format_discovery_report(report), reply_markup=tg.main_keyboard())
        send_approval_cards(report.get("pending", []))
    return report


# ----------------------- loop produksi -----------------------

def cron_loop(interval_sec: int) -> None:
    db.init_db()
    poller = TelegramPoller({
        "caller": lambda: format_calls_telegram(TokenCaller().run_caller(6)),
        "stats": lambda: Darwin().build_report(),
        "discover": lambda: format_discovery_report(run_discovery_cycle(send_telegram=False)),
        "approve": approve_candidate,
        "reject": reject_candidate,
    })
    poller.start()
    tg.send_message("🤖 Robin online. Born to be Learn 🚀", reply_markup=tg.main_keyboard())

    discovery_every = settings.discovery_interval_hours * 3600
    last_discovery = 0.0
    while True:
        start = time.monotonic()
        try:
            run_scan_cycle()
            if settings.discovery_enabled and (start - last_discovery) >= discovery_every:
                run_discovery_cycle()
                last_discovery = start
        except Exception as e:
            log.exception("siklus error: %s", e)
        elapsed = time.monotonic() - start
        time.sleep(max(5, interval_sec - elapsed))


# ----------------------- selftest -----------------------

def selftest() -> None:
    db.init_db()
    print("✅ import OK")
    print("✅ DB init OK:", settings.db_path)
    print("   settings:", settings.masked())
    print("   call_stats:", db.call_stats())
    print("   active wallets:", db.count_active_wallets())
    # cek pipeline tidak crash tanpa jaringan/data
    calls = TokenCaller().run_caller(6)
    print("   token_caller ->", len(calls), "calls (kosong wajar tanpa data)")
    print("   darwin report preview:\n" + Darwin().build_report())


def main() -> None:
    ap = argparse.ArgumentParser(description="Robin — Smart Wallet Tracker (Solana)")
    ap.add_argument("--once", action="store_true", help="1x siklus scan")
    ap.add_argument("--discover", action="store_true", help="1x siklus discovery")
    ap.add_argument("--cron", action="store_true", help="loop produksi")
    ap.add_argument("--interval", type=int, default=settings.scan_interval_min * 60,
                    help="interval scan (detik)")
    ap.add_argument("--selftest", action="store_true", help="cek import + DB tanpa jaringan")
    ap.add_argument("--no-telegram", action="store_true", help="jangan kirim Telegram")
    args = ap.parse_args()

    if args.selftest:
        selftest()
    elif args.discover:
        db.init_db()
        print(format_discovery_report(run_discovery_cycle(send_telegram=not args.no_telegram)))
    elif args.cron:
        cron_loop(args.interval)
    else:  # default --once
        db.init_db()
        calls = run_scan_cycle(send_telegram=not args.no_telegram)
        print(format_calls_telegram(calls))


if __name__ == "__main__":
    main()
