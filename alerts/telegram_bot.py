"""
telegram_bot.py — Pengirim pesan Telegram via urllib (tanpa library eksternal).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from config.settings import settings, log

API = "https://api.telegram.org/bot{token}/{method}"


def _post(method: str, payload: dict) -> Optional[dict]:
    token = settings.telegram_bot_token
    if not token:
        log.warning("telegram: bot token kosong, lewati %s", method)
        return None
    url = API.format(token=token, method=method)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError) as e:
        log.warning("telegram %s gagal: %s", method, e)
        return None


def send_message(text: str, chat_id: Optional[str] = None,
                 reply_markup: Optional[dict] = None, parse_mode: str = "Markdown") -> Optional[dict]:
    chat_id = chat_id or settings.telegram_signal_chat_id
    if not chat_id:
        log.warning("telegram: chat_id kosong")
        return None
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    _post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def edit_message(chat_id: str, message_id: int, text: str) -> None:
    _post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                              "text": text, "parse_mode": "Markdown"})


def main_keyboard() -> dict:
    return {"keyboard": [["📞 Token Caller", "🧠 Stats"], ["🔍 Discover", "❓ Help"]],
            "resize_keyboard": True}


def approval_keyboard(address: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{address}"},
        {"text": "❌ Reject", "callback_data": f"reject:{address}"},
    ]]}
