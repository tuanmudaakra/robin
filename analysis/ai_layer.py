"""
ai_layer.py — AI Analysis Layer (LLM) untuk Robin.

Dua fungsi:
  1. analyze_token(call)      -> boost/flags/insight (pertajam confidence)
  2. analyze_call_outcome(c)  -> post-mortem: kalau RUGI salahnya di mana,
                                 kalau UNTUNG kelebihannya di mana (untuk lesson-learn)

Provider-agnostic, kompatibel OpenAI Chat Completions:
  DeepSeek / OpenRouter / OpenAI cukup ganti base_url + model di config.
Kalau LLM tidak aktif/dikonfigurasi, semua fungsi mengembalikan fallback netral
sehingga Robin tetap jalan 100% rule-based.
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

from config.settings import settings, log


def _extract_json(text: str) -> dict:
    """Ambil objek JSON pertama dari teks (LLM kadang membungkus dgn ```json)."""
    if not text:
        return {}
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


class AIAnalyzer:
    def __init__(self):
        self.base_url = (settings.llm_base_url or "").rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_sec
        self.provider = (settings.llm_provider or "openai").lower()
        self.enabled = bool(settings.llm_enabled and self.base_url and self.api_key)
        if settings.llm_enabled and not self.enabled:
            log.warning("LLM diaktifkan tapi base_url/api_key kosong -> fallback rule-based")

    # --------------------------- core call ---------------------------

    def _chat(self, system: str, user: str, max_tokens: int = 320) -> str:
        if not self.enabled:
            return ""
        if self.provider == "anthropic":
            return self._chat_anthropic(system, user, max_tokens)
        return self._chat_openai(system, user, max_tokens)

    def _chat_openai(self, system: str, user: str, max_tokens: int) -> str:
        """Format OpenAI /v1/chat/completions (DeepSeek, OpenRouter, OpenAI, dll)."""
        url = f"{self.base_url}/v1/chat/completions" if "/chat/completions" not in self.base_url \
            else self.base_url
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        def _do(b: dict) -> str:
            data = json.dumps(b).encode()
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            return payload["choices"][0]["message"]["content"]

        try:
            return _do(body)
        except urllib.error.HTTPError as e:
            if e.code in (400, 422):
                log.debug("LLM: response_format ditolak (HTTP %s), retry tanpa", e.code)
                try:
                    return _do({k: v for k, v in body.items() if k != "response_format"})
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                        KeyError, IndexError, json.JSONDecodeError) as e2:
                    log.warning("LLM retry gagal: %s -> fallback", e2)
                    return ""
            log.warning("LLM call gagal: %s -> fallback", e)
            return ""
        except (urllib.error.URLError, TimeoutError,
                KeyError, IndexError, json.JSONDecodeError) as e:
            log.warning("LLM call gagal: %s -> fallback", e)
            return ""

    def _chat_anthropic(self, system: str, user: str, max_tokens: int) -> str:
        """Format Anthropic Messages API /v1/messages (freemodel.dev, api.anthropic.com)."""
        url = f"{self.base_url}/v1/messages" if "/v1/messages" not in self.base_url \
            else self.base_url
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            return payload["content"][0]["text"]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                KeyError, IndexError, json.JSONDecodeError) as e:
            log.warning("LLM (anthropic) call gagal: %s -> fallback", e)
            return ""

    # --------------------- 1. token analysis -------------------------

    def analyze_token(self, call: dict) -> dict:
        if not self.enabled:
            return {"boost": 0, "flags": [], "insight": "", "recommendation": "HOLD"}
        f = call.get("features", {})
        system = (
            "Kamu analis on-chain Solana untuk bot 'Robin'. Nilai apakah token layak "
            "di-boost. Jawab HANYA JSON: {\"boost\": int(-10..10), \"flags\": [str], "
            "\"insight\": str(<=120 char, Bahasa Indonesia), "
            "\"recommendation\": \"BOOST|HOLD|REDUCE|SKIP\"}. "
            "boost hanya kalibrasi kecil, JANGAN mengganti keputusan utama."
        )
        user = json.dumps({
            "symbol": call.get("symbol"),
            "call_type": call.get("call_type"),
            "scores": {k: f.get(k) for k in
                       ("smart_money_score", "momentum_score", "safety_score", "timing_score")},
            "cluster": f.get("cluster"), "alpha": f.get("alpha"),
            "n_wallets": f.get("n_wallets"), "rug_risk": f.get("rug_risk"),
            "liquidity_usd": f.get("liquidity_usd"), "market_cap": f.get("market_cap"),
            "price_change_1h": f.get("price_change_1h"),
        }, ensure_ascii=False)
        res = _extract_json(self._chat(system, user))
        if not res:
            return {"boost": 0, "flags": [], "insight": "", "recommendation": "HOLD"}
        try:
            boost = int(res.get("boost", 0))
        except (TypeError, ValueError):
            boost = 0
        return {
            "boost": max(-10, min(10, boost)),
            "flags": [str(x) for x in (res.get("flags") or [])][:5],
            "insight": str(res.get("insight", ""))[:160],
            "recommendation": res.get("recommendation", "HOLD"),
        }

    # ------------------- 2. post-mortem lesson -----------------------

    def analyze_call_outcome(self, call: dict) -> dict:
        """
        Diberi call yang sudah final (HIT/MISS) + tracking harga.
        Hasil: narasi pelajaran. Kalau RUGI -> salah di mana; UNTUNG -> kelebihan di mana.
        """
        outcome = call.get("outcome", "UNKNOWN")
        if not self.enabled:
            return {"summary": "", "factors": [], "source": "rule"}
        feats = call.get("features") or {}
        if isinstance(feats, str):
            feats = _extract_json(feats) or {}
        system = (
            "Kamu mentor trading memecoin Solana. Lakukan post-mortem 1 call yang sudah selesai. "
            "Kalau MISS: jelaskan KESALAHAN spesifik (entry, timing, likuiditas, sinyal palsu). "
            "Kalau HIT: jelaskan KELEBIHAN yang bikin menang. "
            "Jawab HANYA JSON: {\"summary\": str(<=160 char, Bahasa Indonesia), "
            "\"factors\": [str] (2-4 poin), \"adjust_hint\": str}."
        )
        user = json.dumps({
            "symbol": call.get("token_symbol"),
            "call_type": call.get("call_type"),
            "outcome": outcome,
            "price_change_final_pct": call.get("final_pct"),
            "peak_pct": call.get("peak_pct"), "trough_pct": call.get("trough_pct"),
            "hold_hours": call.get("hold_hours"),
            "confidence": call.get("confidence"),
            "features": feats,
        }, ensure_ascii=False)
        res = _extract_json(self._chat(system, user, max_tokens=360))
        if not res:
            return {"summary": "", "factors": [], "source": "rule"}
        return {
            "summary": str(res.get("summary", ""))[:200],
            "factors": [str(x) for x in (res.get("factors") or [])][:4],
            "adjust_hint": str(res.get("adjust_hint", "")),
            "source": "llm",
        }
