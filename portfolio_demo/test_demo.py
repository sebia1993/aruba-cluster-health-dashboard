from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.logic import SCENARIOS, run_demo
from streamlit.testing.v1 import AppTest


class DemoTests(unittest.TestCase):
    def test_all_scenarios_through_ui_without_network(self):
        for scenario in SCENARIOS:
            with (
                self.subTest(scenario=scenario),
                patch(
                    "socket.create_connection", side_effect=AssertionError("No network")
                ),
            ):
                app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run(
                    timeout=20
                )
                self.assertFalse(app.exception)
                app.selectbox[0].select(scenario).run()
                next(b for b in app.button if b.label == "분석 실행").click().run(
                    timeout=20
                )
                self.assertFalse(app.exception)
                self.assertTrue(app.metric)
                self.assertTrue(app.dataframe)
                # Re-render must retain the result without re-running analysis.
                app.run()
                self.assertFalse(app.exception)
                self.assertIn("result", app.session_state)

    def test_health_classification_and_debounce(self):
        for scenario, expected in {
            "normal": "normal",
            "warning": "warning",
            "critical": "critical",
            "collection_failed": "unknown",
            "parse_failed": "unknown",
            "partial": "unknown",
        }.items():
            health, raw, timeline = run_demo(scenario)
            self.assertEqual(health.severity.value, expected)
            self.assertEqual(len(health.devices), 4)
            if expected == "unknown":
                self.assertTrue(
                    all(d.severity.value == "unknown" for d in health.devices)
                )
            if scenario == "warning":
                self.assertEqual(
                    [p["Status"] for p in timeline], ["normal", "normal", "warning"]
                )


if __name__ == "__main__":
    unittest.main()
