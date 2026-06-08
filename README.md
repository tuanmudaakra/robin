# 🤖 Robin — Smart Wallet Tracker (Solana)

Versi **2.1-discovery**. Implementasi referensi dari `ROBIN_DOCUMENTATION.md`.
Dependency-ringan: **hanya stdlib** (`urllib`, `sqlite3`, `threading`). Python ≥ 3.9.

## Fitur
- **Token Caller** — skor 4-komponen (Smart 30 / Momentum 25 / Safety 25 / Timing 20) → JP / JPJ / WATCH / SKIP.
- **Darwin + Auto Lesson-Learn** — tiap call ditrack sejak waktu call → harga akhir periode → difinalkan HIT/MISS → **dipelajari**: kalau rugi salahnya di mana, kalau untung kelebihannya di mana (rule-based + post-mortem LLM opsional). Threshold menyesuaikan sendiri.
- **Wallet Discovery (Auto-Update)** — temukan dompet baru dari token outstanding, promosi hybrid (auto ≥80 / approval Telegram 60–79), auto-demote dompet pasif/buruk.
- **LLM Layer** — opsional, OpenAI-compatible (DeepSeek / OpenRouter / OpenAI). Mati = Robin 100% rule-based.
- **Telegram** — tombol 📞 Token Caller · 🧠 Stats · 🔍 Discover · ❓ Help + inline approval ✅/❌.

## Setup
```bash
cd robin
cp .env.example .env        # isi HELIUS_API_KEY, BIRDEYE_API_KEY, TELEGRAM_*
# (opsional LLM) set LLM_ENABLED=true + LLM_API_KEY
python3 main.py --selftest   # cek import + init DB (tanpa jaringan)
```

> Konfigurasi: `.env` meng-override `config/config.json`. Jangan commit `.env` (sudah di `.gitignore`).

## Menjalankan
```bash
python3 main.py --once                  # 1x siklus scan token
python3 main.py --discover              # 1x siklus wallet discovery
python3 main.py --once --no-telegram    # tanpa kirim Telegram (uji lokal)
pm2 start ecosystem.config.cjs          # produksi (scan 10m, discovery 6j, poller 2s)
```

## Struktur
```
main.py            entry + orkestrasi 2 loop (scan 10m / discovery 6j)
db.py              SQLite schema + migrasi + helper
learning.py        Darwin + Auto Lesson-Learn
config/            settings.py + config.json
ingestion/         base, normalizer, helius, dexscreener, birdeye
analysis/          token_caller, wallet_profiler, wallet_discovery, momentum, token_health, ai_layer
alerts/            telegram_bot, telegram_handler (poller 2s + inline approval)
```

## Catatan port ke server
Robin asli ada di mesin lain — ini versi bersih untuk di-merge. Yang paling
mungkin perlu disesuaikan ke bentuk respons API aktual: parsing di
`ingestion/helius.py` (`parse_swaps`) dan `ingestion/birdeye.py` (`top_traders`/`token_txs`),
karena bentuk JSON provider bisa berubah. Logika skor/lesson/discovery murni lokal & stabil.
