import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT / "webapp" / "frontend"
DATA_DIR = ROOT / "a_share" / "data"
CUSTOM_ASSETS_PATH = DATA_DIR / "custom_assets.json"

ASSETS = {
    "881121": {
        "code": "881121",
        "name": "半导体行业指数",
        "path": DATA_DIR / "ths_industry_881121_20180101_20260618_none.csv",
        "source": "ths_industry",
        "symbol": "半导体",
    },
    "884229": {
        "code": "884229",
        "name": "884229 板块指数",
        "path": DATA_DIR / "ths_884229.csv",
        "source": "generic_index",
        "symbol": "884229",
    },
}


def _load_custom_assets() -> None:
    if not CUSTOM_ASSETS_PATH.exists():
        return
    try:
        records = json.loads(CUSTOM_ASSETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        name = str(record.get("name") or f"{code} 自定义标的").strip()[:40]
        if code in ASSETS:
            # Built-in assets keep their trusted source/path, but users may
            # override the display name from custom_assets.json.
            if name:
                ASSETS[code]["name"] = name
            continue
        ASSETS[code] = {
            "code": code,
            "name": name,
            "path": DATA_DIR / f"stock_{code}.csv",
            "source": "custom",
            "symbol": code,
        }


def reload_asset_overrides() -> None:
    """Reload display-name overrides without rebuilding the Flask process."""
    if not CUSTOM_ASSETS_PATH.exists():
        return
    try:
        records = json.loads(CUSTOM_ASSETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code", "")).strip()
        name = str(record.get("name", "")).strip()[:40]
        if code in ASSETS and name:
            ASSETS[code]["name"] = name


def register_asset(code: str, name: str = "") -> dict:
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("股票代码必须是 6 位数字")
    if code in ASSETS:
        raise ValueError(f"标的 {code} 已存在")
    meta = {
        "code": code,
        "name": (str(name or "").strip()[:40] or f"{code} 自定义标的"),
        "path": DATA_DIR / f"stock_{code}.csv",
        "source": "custom",
        "symbol": code,
    }
    ASSETS[code] = meta
    records = [{"code": item["code"], "name": item["name"]} for item in ASSETS.values() if item["source"] == "custom"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_ASSETS_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def unregister_asset(code: str) -> None:
    meta = ASSETS.get(code)
    if not meta or meta.get("source") != "custom":
        return
    ASSETS.pop(code, None)
    records = [{"code": item["code"], "name": item["name"]} for item in ASSETS.values() if item["source"] == "custom"]
    if records:
        CUSTOM_ASSETS_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    elif CUSTOM_ASSETS_PATH.exists():
        CUSTOM_ASSETS_PATH.unlink()


_load_custom_assets()
