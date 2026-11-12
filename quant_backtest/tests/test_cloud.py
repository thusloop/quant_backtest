from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from streamlit.testing.v1 import AppTest

from market_time import MARKET_TZ, market_now
from webapp.backend.cloud_service import (
    asset_metadata, export_configuration, import_configuration, initial_configuration,
)
from webapp.backend.data_service import load_asset, sync_asset
from webapp.backend.strategy_service import validate_strategy


class ConfigurationTests(unittest.TestCase):
    def test_session_changes_and_backup_do_not_mutate_shared_configuration(self):
        assets, definitions = initial_configuration()
        original_assets, original_definitions = initial_configuration()
        item = validate_strategy({"id": "custom_test", "name": "测试策略", "base_key": "macd_cross",
                                  "params": {"macd_fast": 8, "macd_slow": 21}}, definitions)
        assets["600000"] = asset_metadata("600000", "测试标的")
        definitions.append(item)
        restored_assets, restored_definitions = import_configuration(
            export_configuration(assets, definitions).encode("utf-8"), *initial_configuration())
        self.assertEqual(restored_assets["600000"]["name"], "测试标的")
        self.assertEqual(next(x for x in restored_definitions if x["id"] == "custom_test"), item)
        self.assertEqual(initial_configuration(), (original_assets, original_definitions))

    def test_invalid_backup_is_atomic_and_cannot_replace_builtin_strategy(self):
        assets, definitions = initial_configuration()
        before = deepcopy((assets, definitions))
        bad = {"version": 1, "assets": [{"code": "600000"}],
               "strategies": [{"id": "macd_adx", "name": "覆盖内置策略"}]}
        with self.assertRaisesRegex(ValueError, "内置策略"):
            import_configuration(json.dumps(bad).encode(), assets, definitions)
        self.assertEqual((assets, definitions), before)


class DataTests(unittest.TestCase):
    def test_market_clock_and_realtime_branch_use_shanghai_time(self):
        self.assertEqual(market_now().utcoffset(), timedelta(hours=8))
        for hour, expected_calls in ((14, 1), (15, 0)):
            with self.subTest(hour=hour), patch("webapp.backend.data_service.market_now",
                    return_value=datetime(2026, 11, 12, hour, tzinfo=MARKET_TZ)), patch(
                    "webapp.backend.data_service.web_strategy.fetch_realtime_snapshot",
                    return_value=(None, "模拟离线")) as fetch:
                frame, quote = load_asset("881121")
                self.assertFalse(frame.empty)
                self.assertEqual(fetch.call_count, expected_calls)
                self.assertEqual(quote["status"], "failed" if hour == 14 else "after_close")
        with patch("webapp.backend.data_service.web_strategy.fetch_realtime_snapshot") as fetch:
            load_asset("881121", include_realtime=False)
            fetch.assert_not_called()

    def test_custom_board_refresh_preserves_previous_years(self):
        with tempfile.TemporaryDirectory() as temporary:
            meta = asset_metadata("881145", "测试板块")
            meta["path"] = Path(temporary) / "history.csv"
            pd.DataFrame([
                {"date": day, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100, "money": 1000}
                for day in ("2025-12-31", "2026-01-05")
            ]).to_csv(meta["path"], index=False)
            response = Mock(status_code=200, text='callback({"data":"20260105,11,13,10,12,200,2000"})')
            with patch("requests.get", return_value=response) as request, patch(
                    "webapp.backend.data_service.market_now", return_value=datetime(2026, 11, 12, tzinfo=MARKET_TZ)):
                status = sync_asset("881145", meta=meta)
            self.assertEqual(status["status"], "synced")
            self.assertEqual(pd.read_csv(meta["path"])["date"].tolist(), ["2025-12-31", "2026-01-05"])
            self.assertEqual(pd.read_csv(meta["path"])["close"].tolist(), [11, 12])
            self.assertEqual(request.call_count, 1)
            self.assertTrue(request.call_args.args[0].endswith("/2026.js"))

    def test_failed_sync_leaves_existing_cache_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            meta = asset_metadata("884229")
            meta["path"] = Path(temporary) / "history.csv"
            original = b"date,close\n2025-12-31,11\n"
            meta["path"].write_bytes(original)
            with patch("requests.get", side_effect=TimeoutError("模拟超时")):
                status = sync_asset("884229", meta=meta)
            self.assertEqual(status["status"], "cached")
            self.assertIn("模拟超时", status["message"])
            self.assertEqual(meta["path"].read_bytes(), original)

    def test_original_flask_routes_still_work_offline(self):
        from webapp.backend.app import app
        client = app.test_client()
        with client.get("/") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/api/assets").status_code, 200)
        result = client.get("/api/dashboard?code=884229").get_json()
        self.assertNotIn("error", result)
        self.assertIn("close", result["curves"])


class StreamlitTests(unittest.TestCase):
    def app(self):
        return AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=60).run()

    @staticmethod
    def by_label(elements, label):
        return next(element for element in elements if element.label == label)

    def test_initial_render_asset_switch_ranges_and_trade_filter_without_network(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("首次打开不应联网")):
            app = self.app()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(app.multiselect[0].value, ["close", "buy_hold"])
            self.by_label(app.selectbox, "股票 / 指数").select("881145").run()
            self.assertEqual(list(app.exception), [])
            self.assertFalse(list(app.error))
            self.by_label(app.selectbox, "股票 / 指数").select("884229").run()
            self.assertEqual(list(app.exception), [])
            app.multiselect[0].set_value(["macd_cross", "buy_hold"]).run()
            self.by_label(app.selectbox, "快速区间").select("近1年").run()
            self.by_label(app.text_input, "筛选日期、方向或原因").set_value("BUY").run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("区间最大回撤" in item.value for item in app.caption))
            self.by_label(app.selectbox, "快速区间").select("自定义").run()
            app.date_input[0].set_value((datetime(2026, 1, 1).date(), datetime(2026, 6, 1).date())).run()
            self.assertEqual(list(app.exception), [])

    def test_strategy_create_edit_delete_and_session_isolation(self):
        app = self.app()
        self.by_label(app.text_input, "策略名称").set_value("会话专属策略")
        self.by_label(app.button, "保存策略").click().run()
        self.assertEqual(list(app.exception), [])
        saved = next(x for x in app.session_state.definitions if x["name"] == "会话专属策略")
        another = self.app()
        self.assertFalse(any(x["id"] == saved["id"] for x in another.session_state.definitions))
        self.by_label(app.selectbox, "编辑策略").select(saved["id"]).run()
        self.by_label(app.text_input, "策略名称").set_value("修改后的策略")
        self.by_label(app.button, "保存策略").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(any(x["name"] == "修改后的策略" for x in app.session_state.definitions))
        self.by_label(app.selectbox, "编辑策略").select(saved["id"]).run()
        self.by_label(app.button, "删除所选自定义策略").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertFalse(any(x["id"] == saved["id"] for x in app.session_state.definitions))


if __name__ == "__main__":
    unittest.main()
