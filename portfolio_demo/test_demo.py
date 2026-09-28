from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.runtime import DemoRuntime
from aruba_mini_dashboard.models import IncidentType
from streamlit.testing.v1 import AppTest


class DemoTests(unittest.TestCase):
    def test_production_timeline_ack_baseline_recovery(self):
        with patch(
            "socket.create_connection", side_effect=AssertionError("No network")
        ):
            r = DemoRuntime()
            self.assertEqual(
                [r.poll().severity.value for _ in range(4)],
                ["normal", "normal", "normal", "warning"],
            )
            r.acknowledge("192.0.2.12")
            self.assertEqual(r.health.severity.value, "warning")
            self.assertTrue(r.incidents.active_incidents()[0].acknowledged)
            r.poll()
            before = r.engine.baseline_store.get("192.0.2.12")
            r.acknowledge("192.0.2.12")
            self.assertEqual(before, r.engine.baseline_store.get("192.0.2.12"))
            # Stage 5 must not silently accept the connection baseline.
            self.assertEqual(r.poll().severity.value, "critical")
            self.assertTrue(r.engine.pending_connection_changes())
            self.assertTrue(r.accept_baseline("192.0.2.12"))
            self.assertNotEqual(before, r.engine.baseline_store.get("192.0.2.12"))
            self.assertEqual(r.poll().severity.value, "warning")
            self.assertTrue(
                any(
                    i.incident_type is IncidentType.CLIENT_DISTRIBUTION
                    for i in r.incidents.active_incidents()
                )
            )
            self.assertEqual(r.poll().severity.value, "normal")
            self.assertFalse(r.incidents.active_incidents())
            self.assertEqual(r.poll().severity.value, "normal")

    def test_timeout_is_unknown_and_does_not_recover(self):
        r = DemoRuntime()
        for _ in range(4):
            r.poll()
        ids = {i.incident_id for i in r.incidents.active_incidents()}
        self.assertEqual(r.poll(failure=True).severity.value, "unknown")
        self.assertTrue(ids <= {i.incident_id for i in r.incidents.active_incidents()})
        self.assertTrue(all(d.mm_status is None for d in r.health.devices))
        self.assertEqual(r.poller.index, 4)

    def test_ui_poll_filter_ack_pause_reset_and_isolation(self):
        with patch(
            "socket.create_connection", side_effect=AssertionError("No network")
        ):
            app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()

            def click(label):
                next(b for b in app.button if b.label == label).click().run()
                self.assertFalse(app.exception)

            self.assertIsNone(app.session_state.runtime.health)
            click("자동 점검 시작")
            for _ in range(3):
                click("다음 Poll")
            app.selectbox[1].select("192.0.2.12").run()
            click("알림 확인 · ACK")
            self.assertEqual(app.session_state.runtime.health.severity.value, "warning")
            click("일시정지")
            self.assertTrue(
                next(b for b in app.button if b.label == "다음 Poll").disabled
            )
            app.run()
            self.assertEqual(app.session_state.runtime.poll_count, 4)
            other = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()
            self.assertIsNone(other.session_state.runtime.health)
            click("Demo Reset")
            self.assertEqual(app.session_state.runtime.poll_count, 0)
            self.assertFalse(app.session_state.runtime.incidents.events())
            click("지금 점검")
            self.assertEqual(app.session_state.runtime.health.severity.value, "normal")
            click("Demo Reset")
            click("▶ 대표 장애 시나리오 1-click")
            self.assertEqual(app.session_state.runtime.poll_count, 6)
            self.assertIsNotNone(app.session_state.runtime.health)


if __name__ == "__main__":
    unittest.main()
