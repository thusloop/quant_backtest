"""Session configuration for the Streamlit UI; no shared configuration writes."""

from __future__ import annotations

from copy import deepcopy
import json
import re

from .config import ASSETS, DATA_DIR
from .strategy_service import list_strategies, validate_strategy


def initial_configuration() -> tuple[dict, list[dict]]:
    return deepcopy(ASSETS), deepcopy(list_strategies())


def asset_metadata(code: str, name: str = "") -> dict:
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("股票/指数代码必须是 6 位数字")
    return {
        "code": code,
        "name": str(name).strip()[:40] or f"{code} 自定义标的",
        "path": DATA_DIR / f"stock_{code}.csv",
        "source": "custom",
        "symbol": code,
    }


def export_configuration(assets: dict, definitions: list[dict]) -> str:
    return json.dumps({
        "version": 1,
        "assets": [{"code": item["code"], "name": item["name"]} for item in assets.values()],
        "strategies": [item for item in definitions if not item.get("builtin")],
    }, ensure_ascii=False, indent=2)


def import_configuration(content: bytes, assets: dict, definitions: list[dict]) -> tuple[dict, list[dict]]:
    """Validate the entire backup before replacing any session state."""
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("请选择有效的 UTF-8 JSON 配置文件") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("不支持的配置格式或版本")
    new_assets, new_definitions = deepcopy(assets), deepcopy(definitions)
    for key in ("assets", "strategies"):
        if not isinstance(payload.get(key, []), list):
            raise ValueError(f"{key} 必须是列表")
    for record in payload.get("assets", []):
        if not isinstance(record, dict):
            raise ValueError("标的配置必须是对象")
        meta = asset_metadata(record.get("code", ""), record.get("name", ""))
        code = meta["code"]
        if code in new_assets:
            new_assets[code]["name"] = meta["name"]
        else:
            new_assets[code] = meta
    for record in payload.get("strategies", []):
        if not isinstance(record, dict):
            raise ValueError("策略配置必须是对象")
        item = validate_strategy(record, new_definitions)
        new_definitions = [x for x in new_definitions if x["id"] != item["id"]] + [item]
    return new_assets, new_definitions
