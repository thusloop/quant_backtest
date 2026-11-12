from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from .backtest_service import build
from .config import ASSETS, FRONTEND_DIR, register_asset, reload_asset_overrides, unregister_asset
from .data_service import load_asset, sync_asset
from .strategy_service import delete_strategy, list_strategies, save_strategy

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")


def public_asset(meta: dict) -> dict:
    return {key: (str(value) if isinstance(value, Path) else value) for key, value in meta.items()}


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/assets/<path:name>")
def assets(name):
    return send_from_directory(FRONTEND_DIR / "assets", name)


@app.get("/api/assets")
def asset_list():
    reload_asset_overrides()
    return jsonify([{"code": x["code"], "name": x["name"], "source": x["source"]} for x in ASSETS.values()])


@app.post("/api/assets")
def add_asset():
    payload = request.get_json(silent=True) or {}
    try:
        meta = register_asset(payload.get("code", ""), payload.get("name", ""))
        status = sync_asset(meta["code"])
        if not meta["path"].exists():
            unregister_asset(meta["code"])
            return jsonify({"error": status.get("message", "历史数据同步失败"), "sync": status}), 502
        return jsonify({"asset": public_asset(meta), "sync": status}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"添加标的失败: {exc}"}), 500


@app.get("/api/strategies")
def strategy_list():
    return jsonify(list_strategies())


@app.post("/api/strategies")
def strategy_save():
    try:
        return jsonify(save_strategy(request.get_json(silent=True) or {})), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/strategies/<strategy_id>")
def strategy_delete(strategy_id: str):
    try:
        delete_strategy(strategy_id)
        return jsonify({"status": "deleted", "id": strategy_id})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/sync")
def sync():
    code = request.args.get("code", "881121")
    if code not in ASSETS:
        return jsonify({"error": "未知标的"}), 404
    return jsonify(sync_asset(code))


@app.get("/api/dashboard")
def dashboard():
    reload_asset_overrides()
    code = request.args.get("code", "881121")
    if code not in ASSETS:
        return jsonify({"error": "未知标的"}), 404
    try:
        status = sync_asset(code) if request.args.get("refresh") == "1" else {"status": "cached", "message": "使用本地缓存"}
        frame, realtime = load_asset(code)
        result = build(code, frame)
        result["asset"] = public_asset(ASSETS[code])
        result["sync"] = status
        result["realtime"] = realtime
        return jsonify(result)
    except Exception as exc:
        return jsonify({"asset": public_asset(ASSETS[code]), "sync": {"status": "error", "message": str(exc)}, "error": str(exc)}), 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
