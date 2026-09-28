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

    def test_timeout_ui_never_reports_zero_up(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()
        app.session_state.runtime.poll(failure=True)
        app.run()
        self.assertFalse(app.exception)
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual(metrics["Controller Up"], "확인 불가 4 / 4")
        self.assertEqual(metrics["전체 Active Client"], "확인 불가")

    def test_ui_poll_filter_ack_pause_reset_and_isolation(self):
        with patch(
            "socket.create_connection", side_effect=AssertionError("No network")
        ):
            app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()

            def click(label):
                next(b for b in app.button if b.label == label).click().run()
                self.assertFalse(app.exception)

            self.assertIsNone(app.session_state.runtime.health)
            click("자동 시작")
            for _ in range(3):
                click("다음 Poll")
            next(box for box in app.selectbox if box.label == "선택 Controller").select(
                "192.0.2.12"
            ).run()
            click("알림 확인")
            self.assertEqual(app.session_state.runtime.health.severity.value, "warning")
            click("일시정지")
            self.assertFalse(app.session_state.runtime.running)
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
            click("대표 장애 상태까지 자동 재생")
            self.assertEqual(app.session_state.runtime.poll_count, 6)
            self.assertIsNotNone(app.session_state.runtime.health)

    def test_execution_trace_counts_unknown_and_live_updates(self):
        r = DemoRuntime()
        updates = []
        r.execution.on_change = lambda: updates.append(
            [s.status for s in r.execution.steps]
        )
        health = r.poll()
        steps = {s.id: s for s in r.execution.steps}
        self.assertTrue(
            {"collect", "parser", "correlation", "incident"} <= steps.keys()
        )
        self.assertEqual(steps["parser"].evidence["rows"], [len(health.devices)] * 3)
        self.assertEqual(
            steps["correlation"].evidence["controllers"], len(health.devices)
        )
        self.assertTrue(any("running" in update for update in updates))
        self.assertIsNotNone(r.execution.elapsed_ms)
        self.assertTrue(all(s.status != "running" for s in r.execution.steps))
        r.poll(failure=True)
        steps = {s.id: s for s in r.execution.steps}
        self.assertEqual(steps["parser"].evidence["rows"], [None, None, None])
        self.assertEqual(
            steps["correlation"].evidence["unknown"], len(r.health.devices)
        )
        self.assertTrue(any(s.status == "failure" for s in r.execution.steps))
        app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()
        next(b for b in app.button if b.label == "지금 점검").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(
            any(
                "실행 과정" in m.value and "Production Parser" in m.value
                for m in app.markdown
            )
        )
        self.assertEqual(
            next(m.value for m in app.metric if m.label == "Controller Up"), "4 / 4"
        )
        app.run()
        self.assertTrue(any("실행 과정" in m.value for m in app.markdown))


if __name__ == "__main__":
    unittest.main()
