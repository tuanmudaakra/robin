"""
settings.py — Loader konfigurasi Robin + logging.

Urutan prioritas (tinggi -> rendah):
  1. Environment variable (mis. HELIUS_API_KEY) atau .env di root project
  2. config/config.json
  3. default hard-coded di kelas Settings
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON = ROOT / "config" / "config.json"
ENV_FILE = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """Parser .env minimal (tanpa dependency python-dotenv)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _coerce(value, default):
    """Cast string env ke tipe yang sama dengan default."""
    if value is None:
        return default
    if isinstance(default, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


class Settings:
    """Konfigurasi runtime Robin. Akses sebagai atribut: settings.helius_api_key."""

    # default aman (dipakai kalau config.json & env kosong)
    _DEFAULTS = {
        "helius_api_key": "",
        "birdeye_api_key": "",
        "telegram_bot_token": "",
        "telegram_signal_chat_id": "",
        "telegram_admin_chat_id": "",
        "rpc_url": "https://mainnet.helius-rpc.com/?api-key=",
        "scan_interval_min": 10,
        "max_tx_per_scan": 50,
        "min_liquidity_usd": 5000,
        "max_rug_risk_score": 70,
        # discovery
        "discovery_enabled": True,
        "discovery_interval_hours": 6,
        "outstanding_min_pump_pct": 200,
        "outstanding_min_liquidity_usd": 10000,
        "candidate_min_winners": 2,
        "auto_promote_score": 80,
        "approval_score_min": 60,
        "demote_winrate_pct": 30,
        "demote_inactive_days": 30,
        "max_active_wallets": 100,
        # lesson-learn horizon
        "jp_horizon_hours": 6,
        "jpj_horizon_hours": 72,
        "hit_threshold_pct": 5.0,
        "miss_threshold_pct": -10.0,
        # llm
        "llm_enabled": False,
        "llm_provider": "openai",   # "openai" atau "anthropic"
        "llm_base_url": "https://api.deepseek.com",
        "llm_api_key": "",
        "llm_model": "deepseek-chat",
        "llm_timeout_sec": 20,
        "llm_top_n": 5,
        # db
        "db_path": str(ROOT / "robin.db"),
    }

    # nama env var khusus -> key config (selain UPPER(key))
    _ENV_ALIASES = {
        "rpc_url": "RPC_URL",
        "llm_api_key": "LLM_API_KEY",
    }

    def __init__(self):
        _load_env_file(ENV_FILE)
        file_cfg = {}
        if CONFIG_JSON.exists():
            try:
                file_cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                print(f"[settings] config.json invalid: {exc}", file=sys.stderr)

        for key, default in self._DEFAULTS.items():
            base = file_cfg.get(key, default)
            env_name = self._ENV_ALIASES.get(key, key.upper())
            env_val = os.environ.get(env_name)
            setattr(self, key, _coerce(env_val, base))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._DEFAULTS}

    def masked(self) -> dict:
        """Untuk logging — sembunyikan secret."""
        out = self.as_dict()
        for k in out:
            if any(s in k for s in ("key", "token", "api")):
                v = str(out[k])
                out[k] = (v[:6] + "…") if v else "(empty)"
        return out


def setup_logging(name: str = "robin", level: int = logging.INFO) -> logging.Logger:
    """Logger ke stdout + file logs/<name>.log."""
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logging.FileHandler(logs_dir / f"{name}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger


# singleton
settings = Settings()
log = setup_logging()
