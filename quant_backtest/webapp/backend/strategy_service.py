from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY_DIR = ROOT / "webapp" / "strategies"

DEFAULTS = [
    {
        "id": "macd_adx",
        "key": "close",
        "name": "MACD+ADX动态仓位",
        "description": "MACD金叉入场，ADX分级仓位，EMA20/ATR止损与分批止盈。",
        "base_key": "close",
        "color": "#d97706",
        "builtin": True,
        "enabled": True,
        "params": {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "adx_period": 14},
    },
    {
        "id": "macd_cross",
        "key": "macd_cross",
        "name": "MACD金叉买入/死叉卖出",
        "description": "MACD金叉买入，死叉卖出，收盘价执行。",
        "base_key": "macd_cross",
        "color": "#7c3aed",
        "builtin": True,
        "enabled": True,
        "params": {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "adx_period": 14},
    },
    {
        "id": "buy_hold",
        "key": "buy_hold",
        "name": "一直持有",
        "description": "首个交易日买入并持有，用作基准。",
        "base_key": "buy_hold",
        "color": "#176b87",
        "builtin": True,
        "enabled": True,
        "params": {},
    },
]


def _ensure_defaults() -> None:
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    for item in DEFAULTS:
        path = STRATEGY_DIR / f"{item['id']}.json"
        if not path.exists():
            path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def list_strategies() -> list[dict]:
    _ensure_defaults()
    result = []
    for path in sorted(STRATEGY_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(item, dict) and item.get("id") and item.get("base_key"):
                result.append(item)
        except (OSError, ValueError):
            continue
    return result


def get_strategy(strategy_id: str) -> dict | None:
    return next((item for item in list_strategies() if item["id"] == strategy_id), None)


def validate_strategy(payload: dict, definitions: list[dict]) -> dict:
    """Validate a strategy without writing to the shared filesystem."""
    name = str(payload.get("name", "")).strip()[:60]
    if not name:
        raise ValueError("策略名称不能为空")
    strategy_id = str(payload.get("id", "")).strip().lower()
    if not strategy_id:
        strategy_id = f"custom_{int(time.time() * 1000)}"
    if not re.fullmatch(r"[a-z0-9_]{2,50}", strategy_id):
        raise ValueError("策略ID只能包含小写字母、数字和下划线")
    base_key = str(payload.get("base_key", "macd_cross"))
    if base_key not in {"close", "macd_cross", "buy_hold"}:
        raise ValueError("暂不支持该策略模板")
    existing = next((item for item in definitions if item["id"] == strategy_id), None)
    if existing and existing.get("builtin"):
        raise ValueError("内置策略不能覆盖，请复制后保存")
    defaults = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "adx_period": 14}
    try:
        params = {key: int(payload.get("params", {}).get(key, value)) for key, value in defaults.items()}
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("MACD/ADX参数必须是整数") from exc
    if not (2 <= params["macd_fast"] < params["macd_slow"] and 2 <= params["macd_signal"] <= 50 and 5 <= params["adx_period"] <= 60):
        raise ValueError("MACD/ADX参数不合法")
    color = str(payload.get("color", "#0f766e"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("策略颜色必须是 #RRGGBB 格式")
    return {
        "id": strategy_id,
        "key": strategy_id,
        "name": name,
        "description": str(payload.get("description", "")).strip()[:200],
        "base_key": base_key,
        "color": color,
        "builtin": False,
        "enabled": bool(payload.get("enabled", True)),
        "params": params,
    }


def save_strategy(payload: dict) -> dict:
    item = validate_strategy(payload, list_strategies())
    strategy_id = item["id"]
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / f"{strategy_id}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    return item


def delete_strategy(strategy_id: str) -> None:
    item = get_strategy(strategy_id)
    if not item:
        raise ValueError("策略不存在")
    if item.get("builtin"):
        raise ValueError("内置策略不能删除")
    (STRATEGY_DIR / f"{strategy_id}.json").unlink(missing_ok=True)


_ensure_defaults()
