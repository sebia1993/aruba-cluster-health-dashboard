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
        self.assertEqual(metrics["정상 응답 장비"], "확인 불가 4 / 4")
        self.assertEqual(metrics["현재 연결 단말 수"], "확인 불가")

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
            next(m.value for m in app.metric if m.label == "정상 응답 장비"), "4 / 4"
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
        self.assertTrue(
            any("이 프로젝트는 무엇을 해결하나요?" in m.value for m in app.markdown)
        )
        self.assertTrue(
            any("ENTERPRISE WLAN OPERATIONS" in m.value for m in app.markdown)
        )
        self.assertTrue(
            any("CLUSTER TOPOLOGY" in m.value for m in app.markdown)
        )
        self.assertTrue(
            any("OPERATIONS TIMELINE" in m.value for m in app.markdown)
        )
        self.assertFalse(
            any(e.label == "프로젝트 목적과 체험 안내" for e in app.expander)
        )
        next(b for b in app.button if b.label == "시나리오 자동 실행").click().run()
        self.assertFalse(app.exception)
        run = app.session_state.scenario_runner.run
        self.assertTrue(run.completed)
        self.assertEqual(app.session_state.runtime.health.severity.value, "normal")
        self.assertTrue(
            any(
                "data-scene" in m.proto.body and "복구 완료" in m.proto.body
                for m in app.get("html")
            )
        )
        self.assertTrue(
            any(
                "Execution Trace" in m.value and "Detector" in m.value
                for m in app.markdown
            )
        )
        self.assertTrue(any("이상 기록 / History" == s.value for s in app.subheader))
        self.assertTrue(any("IP" in list(df.value.columns) for df in app.dataframe))
        self.assertTrue(
            any(
                "상태" in df.value.columns and "복구 완료" in df.value["상태"].tolist()
                for df in app.dataframe
            )
        )
        count = app.session_state.runtime.poll_count
        next(s for s in app.selectbox if s.label == "점검 단계별 Execution Trace").select(
            1
        ).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.runtime.poll_count, count)
        self.assertEqual(app.session_state.runtime.health.severity.value, "normal")
        self.assertTrue(any("고급 운영 / 수동 점검" == e.label for e in app.expander))
        for key in ("collection_failure", "normal", "connection_change"):
            next(s for s in app.selectbox if s.label == "예시 상황").select(
                key
            ).run()
            next(b for b in app.button if b.label == "시나리오 자동 실행").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.session_state.scenario_runner.run.completed)
        next(b for b in app.button if b.label == "지금 점검").click().run()
        self.assertFalse(app.exception)
        self.assertNotIn("scenario_runner", app.session_state)

    def test_raw_and_parsed_are_preserved_inside_advanced_area(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()
        next(b for b in app.button if b.label == "시나리오 자동 실행").click().run()
        self.assertFalse(app.exception)
        advanced = next(e for e in app.expander if e.label == "고급 운영 / 수동 점검")
        self.assertTrue(
            {"파싱 결과", "원본 출력"} <= {t.label for t in advanced.get("tab")}
        )
        self.assertTrue(advanced.get("code"))
        self.assertEqual(app.session_state.runtime.health.severity.value, "normal")


if __name__ == "__main__":
    unittest.main()


class GuidedFlowTests(unittest.TestCase):
    def test_navigation_keeps_execution_identity_and_results_on_rerun(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py"))).run()
        next(b for b in app.button if b.label == "시나리오 자동 실행").click().run()
        self.assertFalse(app.exception)
        token = app.session_state.guided_run_id
        runner = app.session_state.scenario_runner
        runtime = app.session_state.runtime
        html = next(
            h.proto.body for h in app.get("html") if 'id="guided-flow"' in h.proto.body
        )
        self.assertIn('data-phase="result"', html)
        self.assertIn("data-toggle", html)
        self.assertIn("SCENARIO PLAYBACK", html)
        self.assertIn("data-progress-fill", html)
        self.assertIn("story-analysis", html)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(token, app.session_state.guided_run_id)
        self.assertIs(runner, app.session_state.scenario_runner)
        self.assertIs(runtime, app.session_state.runtime)
        next(b for b in app.button if b.label == "시나리오 자동 실행").click().run()
        self.assertNotEqual(token, app.session_state.guided_run_id)


class StoryTests(unittest.TestCase):
    def test_story_uses_each_snapshot_not_final_runtime(self):
        from portfolio_demo.story_view import snapshot_card, render_story

        runner = ScenarioRunner()
        run = runner.run_incident_recovery()
        anomaly = snapshot_card(run.snapshots[1], run.snapshots[0])
        self.assertIn("연결 단말 260 → 0", anomaly)
        self.assertIn("이상 관측 · 확정 대기", anomaly)
        self.assertIn("연결 0 · 대기 4", anomaly)
        self.assertNotIn("복구 완료</h4>", anomaly)
        confirmed = snapshot_card(run.snapshots[3], run.snapshots[2])
        self.assertIn("3/3", confirmed)
        self.assertIn("현재 이상 / 복구<b>1 / 0</b>", confirmed)
        recovery = snapshot_card(run.snapshots[4], run.snapshots[3])
        self.assertIn("연결 단말 0 → 260", recovery)
        self.assertIn("1/2", recovery)
        final = render_story(run).split("data-summary", 1)[1]
        self.assertIn("이번에 확인한 과정", final)
        self.assertIn("이상 확정 · 주의 기록 생성", final)
        self.assertIn("현재 이상 / 복구<b>0 / 1</b>", final)
        self.assertEqual(render_story(run).count("data-scene"), len(run.snapshots))

    def test_unknown_baseline_change_and_custom_threshold_narration(self):
        from portfolio_demo.story_view import render_story

        runner = ScenarioRunner()
        failed = render_story(runner.run_collection_failure())
        self.assertIn("응답 장비<b>확인 불가 / 4", failed)
        self.assertIn("장비가 꺼졌다는 뜻이 아닙니다", failed)
        changed = render_story(runner.run_connection_change())
        self.assertIn("Type-A → Type-B", changed)
        self.assertIn("정상 기준은 자동 변경하지 않습니다", changed)
        runner = ScenarioRunner(
            AnomalySettings(anomaly_confirmations=4, recovery_confirmations=3)
        )
        custom = render_story(runner.run_incident_recovery())
        self.assertIn("4/4", custom)
        self.assertIn("3/3", custom)
        self.assertEqual(custom.count("data-scene"), 8)

    def test_interrupted_story_preserves_evidence_and_escapes_text(self):
        from portfolio_demo.story_view import render_story

        runner = ScenarioRunner()
        # Inject at the real poll boundary after play resets its runtime.
        with patch.object(DemoRuntime, "poll", autospec=True) as poll:
            poll.side_effect = RuntimeError("interrupted")
            with self.assertRaises(RuntimeError):
                runner.run_normal()
        self.assertFalse(runner.run.completed)
        self.assertIn("완료된 관측이 없습니다", render_story(runner.run))
        run = ScenarioRunner().run_normal()
        run.completed = False
        run.error = "failure"
        run.snapshots = run.snapshots[:1]
        run.snapshots[0].explanation = '<script>alert("x")</script>'
        html = render_story(run)
        self.assertIn("중단 전 마지막 관측", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
