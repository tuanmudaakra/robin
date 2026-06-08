"""
telegram_handler.py — Poller getUpdates (thread terpisah, 2 detik) + command/callback handler.

Tombol: 📞 Token Caller · 🧠 Stats · 🔍 Discover · ❓ Help
Inline: ✅ Approve / ❌ Reject kandidat dompet (Discovery).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error

from config.settings import settings, log
from alerts import telegram_bot as tg

HELP = (
    "❓ *Robin Help*\n\n"
    "📞 *Token Caller* — call JP/JPJ terbaru (6 jam)\n"
    "🧠 *Stats* — Darwin learning + lesson terakhir\n"
    "🔍 *Discover* — cari dompet baru dari token outstanding\n\n"
    "JP = Jangka Pendek (scalp 5m–2j)\n"
    "JPJ = Jangka Panjang (swing 4j–7h)\n"
    "CA ditampilkan full untuk tap-copy."
)


class TelegramPoller:
    """
    callbacks: dict aksi -> fungsi
      'caller'   -> () -> str         (teks call)
      'stats'    -> () -> str         (teks darwin)
      'discover' -> () -> str         (jalankan discovery, return teks report)
      'approve'  -> (address) -> bool
      'reject'   -> (address) -> bool
    """

    def __init__(self, callbacks: dict):
        self.cb = callbacks
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not settings.telegram_bot_token:
            log.warning("poller: token kosong, tidak start")
            return
        self._thread = threading.Thread(target=self._loop, name="tg-poller", daemon=True)
        self._thread.start()
        log.info("telegram poller start (2s interval)")

    def stop(self) -> None:
        self._stop.set()

    def _get_updates(self) -> list[dict]:
        url = (f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
               f"?timeout=10&offset={self._offset}")
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            return data.get("result", []) if data.get("ok") else []
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError) as e:
            log.debug("getUpdates err: %s", e)
            return []

    def _loop(self) -> None:
        while not self._stop.is_set():
            for upd in self._get_updates():
                self._offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        self._on_message(upd["message"])
                    elif "callback_query" in upd:
                        self._on_callback(upd["callback_query"])
                except Exception as e:  # jangan biarkan thread mati
                    log.exception("handler error: %s", e)
            time.sleep(2)

    def _on_message(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        chat_id = str(msg["chat"]["id"])
        if "Token Caller" in text or text == "/caller":
            tg.send_message(self._safe("caller"), chat_id, tg.main_keyboard())
        elif "Stats" in text or text == "/stats":
            tg.send_message(self._safe("stats"), chat_id, tg.main_keyboard())
        elif "Discover" in text or text == "/discover":
            tg.send_message("🔍 Menjalankan discovery…", chat_id)
            tg.send_message(self._safe("discover"), chat_id, tg.main_keyboard())
        elif "Help" in text or text in ("/help", "/start"):
            tg.send_message(HELP, chat_id, tg.main_keyboard())

    def _on_callback(self, cq: dict) -> None:
        data = cq.get("data", "")
        cid = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        mid = cq["message"]["message_id"]
        if ":" not in data:
            tg.answer_callback(cid)
            return
        action, address = data.split(":", 1)
        fn = self.cb.get(action)
        ok = bool(fn and fn(address))
        if action == "approve":
            tg.answer_callback(cid, "✅ Approved" if ok else "Gagal")
            tg.edit_message(chat_id, mid, f"✅ Dompet `{address}` di-APPROVE & masuk registry.")
        elif action == "reject":
            tg.answer_callback(cid, "❌ Rejected" if ok else "Gagal")
            tg.edit_message(chat_id, mid, f"❌ Dompet `{address}` di-REJECT.")
        else:
            tg.answer_callback(cid)

    def _safe(self, key: str) -> str:
        fn = self.cb.get(key)
        if not fn:
            return "Fitur belum tersedia."
        try:
            return fn() or "(kosong)"
        except Exception as e:
            log.exception("cb %s error", key)
            return f"⚠️ Error: {e}"
