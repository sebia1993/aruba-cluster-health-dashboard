from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_demo.runtime import DemoRuntime
from portfolio_demo.scenario_runner import ScenarioRunner
from aruba_mini_dashboard.services.anomaly_detector import AnomalySettings
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


class ScenarioTests(unittest.TestCase):
    def test_incident_recovery_uses_configured_thresholds_and_snapshot_isolation(self):
        for a, recovery in ((3, 2), (4, 3), (1, 1)):
            runner = ScenarioRunner(
                AnomalySettings(
                    anomaly_confirmations=a, recovery_confirmations=recovery
                )
            )
            with patch(
                "socket.create_connection", side_effect=AssertionError("No network")
            ):
                run = runner.run_incident_recovery()
            snaps = run.snapshots
            self.assertTrue(run.completed)
            self.assertEqual(len(snaps), 1 + a + recovery)
            self.assertEqual(snaps[0].health.severity.value, "normal")
            for index in range(1, a):
                self.assertFalse(snaps[index].incidents)
                self.assertEqual(snaps[index].anomaly_count, index)
            self.assertTrue(snaps[a].incidents[0].active)
            self.assertEqual(snaps[a].anomaly_count, a)
            for index in range(1, recovery):
                self.assertTrue(snaps[a + index].incidents[0].active)
                self.assertEqual(snaps[a + index].recovery_count, index)
            self.assertEqual(snaps[-1].recovery_count, recovery)
            self.assertFalse(snaps[-1].incidents[0].active)
            self.assertIsNotNone(snaps[-1].incidents[0].recovered_at)
            self.assertEqual(snaps[-1].health.severity.value, "normal")
            self.assertEqual(snaps[-1].devices, runner.runtime.rows())
            self.assertEqual(len(runner.runtime.history), len(snaps))
            self.assertTrue(snaps[a].incidents[0].active)  # no mutation by later polls
            runner.runtime.poll(failure=True)
            self.assertEqual(snaps[-1].health.severity.value, "normal")

    def test_failure_preserves_unknown_without_fabricating_device_fault(self):
        runner = ScenarioRunner()
        run = runner.run_collection_failure()
        snap = run.snapshots[-1]
        self.assertEqual(snap.health.severity.value, "unknown")
        self.assertIsNone(snap.up)
        self.assertIsNone(snap.active_total)
        self.assertEqual(len(snap.devices), len(run.snapshots[0].devices))
        self.assertTrue(
            all(
                i.incident_type is IncidentType.COLLECTION_FAILURE
                for i in snap.incidents
            )
        )
        self.assertTrue(any(s.status == "failure" for s in snap.trace.steps))
        self.assertEqual(snap.incidents, runner.runtime.incidents.events())

    def test_normal_and_connection_baseline_remains_unaccepted(self):
        runner = ScenarioRunner()
        normal = runner.run_normal()
        self.assertGreater(len(normal.snapshots), 1)
        self.assertTrue(
            all(
                s.health.severity.value == "normal" and not s.incidents
                for s in normal.snapshots
            )
        )
        run = runner.run_connection_change()
        self.assertTrue(run.snapshots[-1].pending)
        for pending in run.snapshots[-1].pending:
            self.assertEqual(
                run.snapshots[0].baselines[pending.member_ip].normalized_value,
                run.snapshots[-1].baselines[pending.member_ip].normalized_value,
            )
            self.assertNotEqual(pending.previous_value, pending.current_value)
        self.assertTrue(
            all(not i.acknowledged for i in runner.runtime.incidents.events())
        )

    def test_every_poll_has_real_trace_and_matching_evidence(self):
        runner = ScenarioRunner()
        for key in (
            "incident_recovery",
            "normal",
            "collection_failure",
            "connection_change",
        ):
            run = runner.play(key)
            for snap in run.snapshots:
                ids = {s.id: s for s in snap.trace.steps}
                self.assertTrue(
                    {"collect", "parser", "correlation", "detector", "incident"}
                    <= ids.keys()
                )
                self.assertEqual(
                    ids["correlation"].evidence["controllers"], len(snap.health.devices)
                )
                self.assertEqual(
                    ids["incident"].evidence["active"],
                    sum(i.active for i in snap.incidents),
                )
                self.assertEqual(ids["detector"].evidence["after"], snap.detector)
                self.assertIsNotNone(snap.trace.elapsed_ms)
                self.assertTrue(all(s.status != "running" for s in snap.trace.steps))
                self.assertEqual(
                    len([s for s in snap.trace.steps if s.id == "collect"]), 3
                )
            self.assertIsNot(run.snapshots[0].trace, run.snapshots[-1].trace)

    def test_one_click_app_flow_and_rerun_persistence(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()
        next(
            b for b in app.button if b.label == "대표 장애 → 복구 시나리오 실행"
        ).click().run()
        self.assertFalse(app.exception)
        run = app.session_state.scenario_runner.run
        self.assertTrue(run.completed)
        self.assertEqual(app.session_state.runtime.health.severity.value, "normal")
        self.assertTrue(
            any(
                "Scenario Timeline" in m.value and "복구 완료" in m.value
                for m in app.markdown
            )
        )
        self.assertTrue(
            any(
                "Execution Trace" in m.value and "Detector" in m.value
                for m in app.markdown
            )
        )
        self.assertTrue(any("Incident / History" == s.value for s in app.subheader))
        self.assertTrue(any("IP" in list(df.value.columns) for df in app.dataframe))
        self.assertTrue(
            any(
                "상태" in df.value.columns and "Resolved" in df.value["상태"].tolist()
                for df in app.dataframe
            )
        )
        count = app.session_state.runtime.poll_count
        next(s for s in app.selectbox if s.label == "Poll별 Execution Trace").select(
            1
        ).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.runtime.poll_count, count)
        self.assertEqual(app.session_state.runtime.health.severity.value, "normal")
        self.assertTrue(any("고급 운영 / 수동 점검" == e.label for e in app.expander))
        for label in ("CLI 수집 실패", "정상 상태", "Connection-Type 변화"):
            next(b for b in app.button if b.label == label).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.session_state.scenario_runner.run.completed)
        next(b for b in app.button if b.label == "지금 점검").click().run()
        self.assertFalse(app.exception)
        self.assertNotIn("scenario_runner", app.session_state)


if __name__ == "__main__":
    unittest.main()
