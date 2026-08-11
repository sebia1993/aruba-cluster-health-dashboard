from __future__ import annotations

from aruba_mini_dashboard.models import ClientDistributionRow, DetectionMode
from aruba_mini_dashboard.services.anomaly_detector import AnomalyDetector, AnomalySettings


IPS = tuple(f"192.0.2.{number}" for number in range(11, 15))


def rows(values: list[tuple[int, int]]) -> list[ClientDistributionRow]:
    return [ClientDistributionRow(ip, active, standby) for ip, (active, standby) in zip(IPS, values)]


BALANCED = rows([(250, 260), (260, 250), (245, 255), (260, 250)])
ONE_LOW = rows([(250, 260), (0, 5), (245, 255), (260, 250)])
ALL_LOW = rows([(3, 2), (1, 2), (4, 3), (2, 1)])


def test_balanced_members_are_normal() -> None:
    detector = AnomalyDetector()
    result = detector.evaluate_client_distribution(
        BALANCED, IPS, data_complete=True, total_active=1015
    )
    assert not any(item.active or item.condition_met for item in result.values())


def test_one_low_sample_does_not_activate_incident() -> None:
    detector = AnomalyDetector()
    result = detector.evaluate_client_distribution(ONE_LOW, IPS, data_complete=True, total_active=755)
    target = result["192.0.2.12"]
    assert target.condition_met is True
    assert target.anomaly_streak == 1
    assert target.active is False


def test_three_consecutive_low_samples_activate_exact_member() -> None:
    detector = AnomalyDetector()
    for _ in range(3):
        result = detector.evaluate_client_distribution(
            ONE_LOW, IPS, data_complete=True, total_active=755
        )
    assert result["192.0.2.12"].active is True
    assert result["192.0.2.12"].activated is True
    assert result["192.0.2.12"].anomaly_streak == 3
    assert not any(result[ip].active for ip in IPS if ip != "192.0.2.12")


def test_all_low_cluster_skips_specific_member_judgment() -> None:
    detector = AnomalyDetector()
    for _ in range(5):
        result = detector.evaluate_client_distribution(
            ALL_LOW, IPS, data_complete=True, total_active=10
        )
    assert all(item.low_usage and item.deferred for item in result.values())
    assert all(not item.active and item.anomaly_streak == 0 for item in result.values())


def test_two_consecutive_normal_samples_recover_active_incident() -> None:
    detector = AnomalyDetector()
    for _ in range(3):
        detector.evaluate_client_distribution(ONE_LOW, IPS, data_complete=True, total_active=755)
    first = detector.evaluate_client_distribution(BALANCED, IPS, data_complete=True, total_active=1015)
    assert first["192.0.2.12"].active is True
    assert first["192.0.2.12"].recovery_streak == 1
    second = detector.evaluate_client_distribution(BALANCED, IPS, data_complete=True, total_active=1015)
    assert second["192.0.2.12"].active is False
    assert second["192.0.2.12"].recovered is True


def test_deferred_cycle_breaks_pending_client_anomaly_sequence() -> None:
    for deferred_rows, complete, total_active in (
        (ONE_LOW[:-1], False, None),
        (ALL_LOW, True, 10),
    ):
        detector = AnomalyDetector()
        for _ in range(2):
            detector.evaluate_client_distribution(
                ONE_LOW, IPS, data_complete=True, total_active=755
            )
        paused = detector.evaluate_client_distribution(
            deferred_rows,
            IPS,
            data_complete=complete,
            total_active=total_active,
        )
        assert paused["192.0.2.12"].deferred is True
        assert paused["192.0.2.12"].anomaly_streak == 0
        restarted = detector.evaluate_client_distribution(
            ONE_LOW, IPS, data_complete=True, total_active=755
        )
        assert restarted["192.0.2.12"].anomaly_streak == 1
        assert restarted["192.0.2.12"].active is False


def test_incomplete_data_freezes_an_already_active_client_anomaly() -> None:
    detector = AnomalyDetector()
    for _ in range(3):
        detector.evaluate_client_distribution(ONE_LOW, IPS, data_complete=True, total_active=755)
    paused = detector.evaluate_client_distribution((), IPS, data_complete=False)
    assert paused["192.0.2.12"].deferred is True
    assert paused["192.0.2.12"].active is True
    assert paused["192.0.2.12"].anomaly_streak == 3
    assert paused["192.0.2.12"].recovery_streak == 0


def test_deferred_client_cycle_breaks_pending_recovery_confirmation() -> None:
    for deferred_rows, complete, total_active in (
        ((), False, None),
        (ALL_LOW, True, 10),
    ):
        detector = AnomalyDetector()
        for _ in range(3):
            detector.evaluate_client_distribution(
                ONE_LOW, IPS, data_complete=True, total_active=755
            )
        first_normal = detector.evaluate_client_distribution(
            BALANCED, IPS, data_complete=True, total_active=1015
        )
        assert first_normal["192.0.2.12"].recovery_streak == 1

        paused = detector.evaluate_client_distribution(
            deferred_rows,
            IPS,
            data_complete=complete,
            total_active=total_active,
        )
        assert paused["192.0.2.12"].active is True
        assert paused["192.0.2.12"].anomaly_streak == 3
        assert paused["192.0.2.12"].recovery_streak == 0

        restarted = detector.evaluate_client_distribution(
            BALANCED, IPS, data_complete=True, total_active=1015
        )
        assert restarted["192.0.2.12"].active is True
        assert restarted["192.0.2.12"].recovery_streak == 1
        recovered = detector.evaluate_client_distribution(
            BALANCED, IPS, data_complete=True, total_active=1015
        )
        assert recovered["192.0.2.12"].recovered is True


def test_low_usage_does_not_false_recover_an_active_incident() -> None:
    detector = AnomalyDetector()
    for _ in range(3):
        detector.evaluate_client_distribution(ONE_LOW, IPS, data_complete=True, total_active=755)
    result = detector.evaluate_client_distribution(ALL_LOW, IPS, data_complete=True, total_active=10)
    assert result["192.0.2.12"].active is True
    assert result["192.0.2.12"].recovery_streak == 0


def test_absolute_only_mode_does_not_require_peer_median() -> None:
    skewed = rows([(100, 100), (0, 5), (5, 5), (5, 5)])
    combined = AnomalyDetector(
        AnomalySettings(detection_mode=DetectionMode.ABSOLUTE_AND_RELATIVE)
    )
    absolute = AnomalyDetector(AnomalySettings(detection_mode=DetectionMode.ABSOLUTE_ONLY))
    combined_result = combined.evaluate_client_distribution(
        skewed, IPS, data_complete=True, total_active=110
    )
    absolute_result = absolute.evaluate_client_distribution(
        skewed, IPS, data_complete=True, total_active=110
    )
    assert combined_result["192.0.2.12"].condition_met is False
    assert absolute_result["192.0.2.12"].condition_met is True


def test_missing_member_activates_after_three_and_recovers_after_two() -> None:
    detector = AnomalyDetector()
    present = IPS[:1] + IPS[2:]
    for count in range(1, 4):
        result = detector.evaluate_missing("load", present, IPS, data_complete=True)
        assert result["192.0.2.12"].missing_streak == count
        assert result["192.0.2.12"].active is (count == 3)
    first = detector.evaluate_missing("load", IPS, IPS, data_complete=True)
    assert first["192.0.2.12"].active is True
    second = detector.evaluate_missing("load", IPS, IPS, data_complete=True)
    assert second["192.0.2.12"].recovered is True
    assert second["192.0.2.12"].active is False


def test_partial_parse_breaks_pending_missing_streak_for_every_source() -> None:
    for source in ("mm", "load", "membership"):
        detector = AnomalyDetector()
        present = IPS[:1] + IPS[2:]
        detector.evaluate_missing(source, present, IPS, data_complete=True)
        detector.evaluate_missing(source, present, IPS, data_complete=True)
        paused = detector.evaluate_missing(source, (), IPS, data_complete=False)
        assert paused["192.0.2.12"].deferred is True
        assert paused["192.0.2.12"].missing_streak == 0
        restarted = detector.evaluate_missing(source, present, IPS, data_complete=True)
        assert restarted["192.0.2.12"].missing_streak == 1
        assert restarted["192.0.2.12"].active is False


def test_partial_parse_freezes_already_active_missing_incident_for_every_source() -> None:
    for source in ("mm", "load", "membership"):
        detector = AnomalyDetector()
        present = IPS[:1] + IPS[2:]
        for _ in range(3):
            detector.evaluate_missing(source, present, IPS, data_complete=True)
        paused = detector.evaluate_missing(source, (), IPS, data_complete=False)
        assert paused["192.0.2.12"].deferred is True
        assert paused["192.0.2.12"].active is True
        assert paused["192.0.2.12"].missing_streak == 3
        assert paused["192.0.2.12"].recovery_streak == 0


def test_partial_parse_breaks_pending_missing_recovery_for_every_source() -> None:
    for source in ("mm", "load", "membership"):
        detector = AnomalyDetector()
        present = IPS[:1] + IPS[2:]
        for _ in range(3):
            detector.evaluate_missing(source, present, IPS, data_complete=True)
        first_present = detector.evaluate_missing(source, IPS, IPS, data_complete=True)
        assert first_present["192.0.2.12"].recovery_streak == 1

        paused = detector.evaluate_missing(source, (), IPS, data_complete=False)
        assert paused["192.0.2.12"].active is True
        assert paused["192.0.2.12"].missing_streak == 3
        assert paused["192.0.2.12"].recovery_streak == 0

        restarted = detector.evaluate_missing(source, IPS, IPS, data_complete=True)
        assert restarted["192.0.2.12"].active is True
        assert restarted["192.0.2.12"].recovery_streak == 1
        recovered = detector.evaluate_missing(source, IPS, IPS, data_complete=True)
        assert recovered["192.0.2.12"].recovered is True


def test_detector_state_round_trip_preserves_streaks() -> None:
    first = AnomalyDetector()
    first.evaluate_client_distribution(ONE_LOW, IPS, data_complete=True, total_active=755)
    restored = AnomalyDetector(state=first.dump_state())
    result = restored.evaluate_client_distribution(ONE_LOW, IPS, data_complete=True, total_active=755)
    assert result["192.0.2.12"].anomaly_streak == 2
