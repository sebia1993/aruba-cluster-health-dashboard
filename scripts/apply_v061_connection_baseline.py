from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, before: str, after: str) -> None:
    text = read(path)
    count = text.count(before)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}")
    write(path, text.replace(before, after, 1))


def replace_all_required(path: str, before: str, after: str) -> None:
    text = read(path)
    if before not in text:
        raise SystemExit(f"{path}: required text not found: {before!r}")
    write(path, text.replace(before, after))


# Source version and versioned operator documentation.
replace_once('pyproject.toml', 'version = "0.6.0"', 'version = "0.6.1"')
replace_once(
    'src/aruba_mini_dashboard/__init__.py',
    '__version__ = "0.6.0"',
    '__version__ = "0.6.1"',
)
replace_once(
    'README.md',
    '.\\scripts\\package_release.ps1 -Version 0.6.0',
    '.\\scripts\\package_release.ps1 -Version 0.6.1',
)
replace_once(
    'docs/README.txt',
    'Aruba Mini Dashboard 0.6.0\n',
    'Aruba Mini Dashboard 0.6.1\n',
)
replace_all_required(
    'docs/RELEASE_PROCESS_KO.md',
    '0.6.0',
    '0.6.1',
)

changelog = read('CHANGELOG.md')
if '## 0.6.1 - 2026-08-25' not in changelog:
    heading = '# 변경 이력\n\n'
    if not changelog.startswith(heading):
        raise SystemExit('CHANGELOG.md: unexpected header')
    section = '''## 0.6.1 - 2026-08-25

### Connection-Type 정상 기준 확정

- 최초 정상 수집값만 자동 baseline으로 저장하고, 이후 변화값은 운영자가 확인하기 전까지
  기존 정상 baseline을 유지하도록 변경했습니다.
- `현재 Connection-Type 정상 기준 설정`을 선택하면 최신 완료 점검의 현재 값을 새로운
  정상 baseline으로 원자 저장하고, 그 시점 이후 다른 값으로 바뀔 때만 다시 주의합니다.
- 확인 전 값이 기존 정상 baseline으로 되돌아오면 변화 사건과 주의를 자동 복구합니다.
- v0.6.0 이하에서 변화값이 자동 baseline으로 승격된 저장 상태는 pending 사건의
  이전값을 이용해 안전하게 복원한 뒤 운영자 확인을 기다립니다.
- Connection-Type 정상 기준 설정은 같은 IP의 Client 분배 등 다른 장애 알림을
  일괄 확인 처리하지 않도록 별도 작업으로 분리했습니다.

'''
    write('CHANGELOG.md', heading + section + changelog[len(heading):])

readme = read('README.md')
anchor = '''### v0.6.0 구조·안정성 개선

- 보고서와 조치 시각은 항상 KST(UTC+09:00)입니다.
- 재분배 직전 동일 Leader SSH 세션에서 Membership과 MM 전체 Up을 최종 확인합니다.
- SQLite 원자적 상태 전이, 3회 연속 정상 잠금 해제, Cluster 냉각시간과 24시간
  Controller별 실행 한도를 적용합니다.
- MainWindow monkey patch를 제거하고 UI·Workflow·Persistence·시간·SSH 수명주기를
  명시적 컴포넌트로 분리했습니다.
- HTML 보고서 실패는 다음 실행에서 재생성하며 실행 설정 지문과 명령 쓰기 단계를 기록합니다.

'''
addition = anchor + '''### v0.6.1 Connection-Type 정상 기준

- 변화가 감지돼도 운영자가 확인하기 전에는 기존 정상 baseline을 자동 변경하지 않습니다.
- 장비 행을 선택한 뒤 `현재 Connection-Type 정상 기준 설정`을 누르면 현재 값을 새 정상
  기준으로 확정하고 이후 그 값에서 다시 달라질 때만 주의합니다.
- 확인하지 않은 변화가 기존 baseline으로 되돌아오면 변화 주의는 자동 복구됩니다.

'''
if addition not in readme:
    if anchor not in readme:
        raise SystemExit('README.md: v0.6.0 anchor missing')
    write('README.md', readme.replace(anchor, addition, 1))

detection = read('docs/DETECTION_LOGIC_KO.md')
old_detection = '''표의 `Connection-Type` 열만 첫 정상 baseline으로 사용하고 이후 값이 달라지면
변화 사건을 생성합니다. 다음 `STATUS` 열의 `CONNECTED`, `last HBT_RSP`, `RTD`는
실시간 상태 정보이므로 Connection-Type 비교에 포함하지 않습니다.

```text
기존: L2-Connected
현재: L3-Connected
→ Connection-Type 변화 사건
```

운영자가 변화를 확인하면 반복 알림 대상에서는 제외할 수 있지만, 확인 자체가 원래 baseline으로 자동 복귀시키는 것은 아닙니다.

v0.4.2로 처음 실행할 때 이전 버전이 STATUS까지 붙여 저장한 값은 로컬 SQLite
v5 마이그레이션에서 분리합니다. 분리 후 이전값과 현재값이 같은 오탐만 자동으로
종료하고 실제 타입 변화와 과거 사건 이력은 유지합니다.
'''
new_detection = '''표의 `Connection-Type` 열만 정상 baseline과 비교합니다. 다음 `STATUS` 열의
`CONNECTED`, `last HBT_RSP`, `RTD`는 실시간 상태 정보이므로 비교에 포함하지 않습니다.

최초로 완전하게 수집한 값은 자동으로 정상 baseline이 됩니다. 이후 값이 달라지면
변화 사건을 생성하지만, **운영자가 정상으로 확정하기 전에는 baseline을 새 값으로
자동 이동시키지 않습니다.**

```text
확정 baseline: L2-Connected
현재: L3-Connected
→ Connection-Type 변화 주의

운영자가 현재 값을 정상 기준으로 설정
→ 새 baseline: L3-Connected
→ 이후 L3-Connected가 아닌 값으로 바뀔 때만 다시 주의
```

운영자는 해당 장비 행을 선택하고 `현재 Connection-Type 정상 기준 설정`을 눌러
최신 완료 점검의 현재 값을 새 정상 baseline으로 확정할 수 있습니다. 이 작업은
Connection-Type 사건만 확인 처리하며 같은 IP의 Client 분배 등 다른 장애 사건은
그대로 유지합니다.

확인하지 않은 변화가 기존 정상 baseline으로 되돌아오면 변화 사건은 자동 복구됩니다.
v0.6.0 이하에서 변화값이 이미 baseline으로 자동 저장된 상태는 pending 변화 사건의
이전값을 이용해 원래 정상 baseline을 복원한 뒤 운영자 확인을 기다립니다.

v0.4.2로 처음 실행할 때 이전 버전이 STATUS까지 붙여 저장한 값은 로컬 SQLite
v5 마이그레이션에서 분리합니다. 분리 후 이전값과 현재값이 같은 오탐만 자동으로
종료하고 실제 타입 변화와 과거 사건 이력은 유지합니다.
'''
replace_once('docs/DETECTION_LOGIC_KO.md', old_detection, new_detection)

# Correlation: accepted baseline remains stable until explicit operator acceptance.
engine_path = 'src/aruba_mini_dashboard/services/correlation_engine.py'
old_engine_init = '''        self._pending_connection_changes: dict[str, ConnectionChange] = {
            change.member_ip: change
            for change in (pending_connection_changes or ())
        }
        self._monitoring_scope_ips: tuple[str, ...] = ()

    def dump_known_mm_devices(self) -> dict[str, str | None]:
'''
new_engine_init = '''        self._pending_connection_changes: dict[str, ConnectionChange] = {
            change.member_ip: change
            for change in (pending_connection_changes or ())
        }
        self._resolved_connection_change_members: set[str] = set()
        self._monitoring_scope_ips: tuple[str, ...] = ()
        self._restore_legacy_auto_promoted_baselines()

    def _restore_legacy_auto_promoted_baselines(self) -> None:
        """Restore the last operator-accepted value for pre-v0.6.1 state.

        Older releases moved the baseline to the observed changed value before
        the operator acknowledged the event. A durable pending event contains
        both the prior accepted value and the current candidate, so it is the
        authoritative migration source. The repaired baseline is persisted by
        the existing atomic domain-state flush.
        """

        for change in self._pending_connection_changes.values():
            baseline = self.baseline_store.get(change.member_ip)
            previous_normalized = normalize_connection_type(change.previous_value)
            current_normalized = normalize_connection_type(change.current_value)
            if baseline is None:
                self.baseline_store.set(
                    ConnectionBaseline(
                        collector_ip=change.collector_ip,
                        member_ip=change.member_ip,
                        display_value=change.previous_value,
                        normalized_value=previous_normalized,
                        observed_at=change.first_detected_at,
                    )
                )
                continue
            if (
                baseline.normalized_value == current_normalized
                and previous_normalized != current_normalized
            ):
                self.baseline_store.set(
                    replace(
                        baseline,
                        display_value=change.previous_value,
                        normalized_value=previous_normalized,
                        observed_at=change.first_detected_at,
                    )
                )

    def dump_known_mm_devices(self) -> dict[str, str | None]:
'''
replace_once(engine_path, old_engine_init, new_engine_init)

old_ack = '''    def acknowledge_connection_change(self, ip: str, collector_ip: str | None = None) -> bool:
        change = self._pending_connection_changes.get(ip)
        if change is None or (collector_ip is not None and change.collector_ip != collector_ip):
            return False
        del self._pending_connection_changes[ip]
        return True

    def acknowledge_all_connection_changes(self) -> None:
        self._pending_connection_changes.clear()

    def reconcile_monitoring_scope(self, expected_ips: Iterable[str]) -> set[str]:
'''
new_ack = '''    def acknowledge_connection_change(self, ip: str, collector_ip: str | None = None) -> bool:
        """Accept the currently observed Connection-Type as the new normal."""

        change = self._pending_connection_changes.get(ip)
        if change is None or (collector_ip is not None and change.collector_ip != collector_ip):
            return False
        existing = self.baseline_store.get(ip)
        accepted_collector = (
            collector_ip
            or (existing.collector_ip if existing is not None else "")
            or change.collector_ip
        )
        self.baseline_store.set(
            ConnectionBaseline(
                collector_ip=accepted_collector,
                member_ip=ip,
                display_value=change.current_value,
                normalized_value=normalize_connection_type(change.current_value),
                observed_at=change.last_confirmed_at,
            )
        )
        del self._pending_connection_changes[ip]
        self._resolved_connection_change_members.discard(ip)
        return True

    def acknowledge_all_connection_changes(self) -> None:
        for member_ip in tuple(self._pending_connection_changes):
            self.acknowledge_connection_change(member_ip)

    def drain_connection_change_resolutions(self) -> set[str]:
        """Return members whose unaccepted change returned to the baseline."""

        resolved = set(self._resolved_connection_change_members)
        self._resolved_connection_change_members.clear()
        return resolved

    def reconcile_monitoring_scope(self, expected_ips: Iterable[str]) -> set[str]:
'''
replace_once(engine_path, old_ack, new_ack)

old_reconcile = '''        for member_ip in list(self._pending_connection_changes):
            if member_ip not in allowed:
                removed.add(member_ip)
                del self._pending_connection_changes[member_ip]

        prune = getattr(self.baseline_store, "prune", None)
'''
new_reconcile = '''        for member_ip in list(self._pending_connection_changes):
            if member_ip not in allowed:
                removed.add(member_ip)
                del self._pending_connection_changes[member_ip]
                self._resolved_connection_change_members.discard(member_ip)

        prune = getattr(self.baseline_store, "prune", None)
'''
replace_once(engine_path, old_reconcile, new_reconcile)

old_membership = '''        collector_ip = cycle.actual_cluster_controller_ip
        for ip, row in membership_by_ip.items():
            device = devices[ip]
            device.membership_present = True
            device.connection_type = row.connection_type
            device.last_seen = cycle.checked_at
            if not device.is_registered or not complete or not collector_ip:
                continue
            normalized = normalize_connection_type(row.connection_type)
            baseline = self.baseline_store.get(ip)
            if baseline is None:
                self.baseline_store.set(
                    ConnectionBaseline(
                        collector_ip=collector_ip,
                        member_ip=ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
            elif baseline.normalized_value != normalized:
                change = ConnectionChange(
                    collector_ip=collector_ip,
                    member_ip=ip,
                    previous_value=baseline.display_value,
                    current_value=row.connection_type,
                    first_detected_at=cycle.checked_at,
                    last_confirmed_at=cycle.checked_at,
                )
                self._pending_connection_changes[ip] = change
                self.baseline_store.set(
                    ConnectionBaseline(
                        collector_ip=collector_ip,
                        member_ip=ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
            else:
                self.baseline_store.set(
                    replace(
                        baseline,
                        collector_ip=collector_ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
                pending = self._pending_connection_changes.get(ip)
                if pending is not None and normalize_connection_type(pending.current_value) == normalized:
                    self._pending_connection_changes[ip] = replace(
                        pending,
                        last_confirmed_at=cycle.checked_at,
                    )

        for change in self._pending_connection_changes.values():
'''
new_membership = '''        collector_ip = cycle.actual_cluster_controller_ip
        for ip, row in membership_by_ip.items():
            device = devices[ip]
            device.membership_present = True
            device.connection_type = row.connection_type
            device.last_seen = cycle.checked_at
            if not device.is_registered or not complete or not collector_ip:
                continue
            normalized = normalize_connection_type(row.connection_type)
            baseline = self.baseline_store.get(ip)
            if baseline is None:
                self.baseline_store.set(
                    ConnectionBaseline(
                        collector_ip=collector_ip,
                        member_ip=ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
                continue

            pending = self._pending_connection_changes.get(ip)
            if baseline.normalized_value == normalized:
                # Refresh harmless formatting/source metadata without changing
                # the accepted semantic value. Returning to the accepted value
                # is trusted recovery of an unacknowledged change.
                self.baseline_store.set(
                    replace(
                        baseline,
                        collector_ip=collector_ip,
                        display_value=row.connection_type,
                        normalized_value=normalized,
                        observed_at=cycle.checked_at,
                    )
                )
                if pending is not None:
                    del self._pending_connection_changes[ip]
                    self._resolved_connection_change_members.add(ip)
                continue

            # Keep the accepted baseline value stable. Only source metadata is
            # refreshed while the operator decides whether the new value is
            # normal. A third value supersedes the prior pending event.
            self.baseline_store.set(
                replace(
                    baseline,
                    collector_ip=collector_ip,
                )
            )
            if (
                pending is not None
                and normalize_connection_type(pending.current_value) == normalized
            ):
                self._pending_connection_changes[ip] = replace(
                    pending,
                    last_confirmed_at=cycle.checked_at,
                )
                continue
            self._pending_connection_changes[ip] = ConnectionChange(
                collector_ip=collector_ip,
                member_ip=ip,
                previous_value=baseline.display_value,
                current_value=row.connection_type,
                first_detected_at=cycle.checked_at,
                last_confirmed_at=cycle.checked_at,
            )

        for change in self._pending_connection_changes.values():
'''
replace_once(engine_path, old_membership, new_membership)

# Runtime: persist automatic return-to-baseline recovery and expose a precise
# baseline acceptance operation that does not acknowledge unrelated incidents.
main_path = 'src/aruba_mini_dashboard/main.py'
old_correlate = '''            health = active_engine.correlate(cycle)
            transitions = manager.process(health, now=health.checked_at)
'''
new_correlate = '''            health = active_engine.correlate(cycle)
            drain_resolutions = getattr(
                active_engine,
                "drain_connection_change_resolutions",
                None,
            )
            if callable(drain_resolutions):
                self._pending_connection_acknowledgements.update(
                    drain_resolutions()
                )
            transitions = manager.process(health, now=health.checked_at)
'''
replace_once(main_path, old_correlate, new_correlate)

old_runtime_ack = '''    def acknowledge_ip(self, ip: str) -> None:
        with self._lock:
            transitions = self.incident_manager.acknowledge_ip(ip)
            if self.engine.acknowledge_connection_change(ip):
                self._pending_connection_acknowledgements.add(ip)
            self._persist_incidents(transitions)

    def acknowledge_global(self) -> None:
'''
new_runtime_ack = '''    def accept_connection_type_baseline(self, ip: str) -> bool:
        """Accept only the current Connection-Type event as the new baseline."""

        with self._lock:
            if not self.engine.acknowledge_connection_change(ip):
                return False
            transitions: list[Any] = []
            for incident in self.incident_manager.active_incidents():
                if (
                    incident.ip == ip
                    and incident.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
                ):
                    transition = self.incident_manager.acknowledge(
                        incident.incident_id,
                        now=datetime.now(timezone.utc),
                    )
                    if transition is not None:
                        transitions.append(transition)
            self._pending_connection_acknowledgements.add(ip)
            self._persist_incidents(transitions)
            return True

    def acknowledge_ip(self, ip: str) -> None:
        with self._lock:
            transitions = self.incident_manager.acknowledge_ip(ip)
            if self.engine.acknowledge_connection_change(ip):
                self._pending_connection_acknowledgements.add(ip)
            self._persist_incidents(transitions)

    def acknowledge_global(self) -> None:
'''
replace_once(main_path, old_runtime_ack, new_runtime_ack)

old_connections = '''    window.acknowledge_requested.connect(runtime.acknowledge_ip)
    window.acknowledge_global_requested.connect(runtime.acknowledge_global)
    window.acknowledge_requested.connect(notifications.acknowledge_ip)
'''
new_connections = '''    window.acknowledge_requested.connect(runtime.acknowledge_ip)
    window.acknowledge_global_requested.connect(runtime.acknowledge_global)
    window.connection_type_baseline_requested.connect(
        runtime.accept_connection_type_baseline
    )
    window.acknowledge_requested.connect(notifications.acknowledge_ip)
    window.connection_type_baseline_requested.connect(notifications.acknowledge_ip)
'''
replace_once(main_path, old_connections, new_connections)

# UI: use an explicit, contextual operation rather than overloading generic
# acknowledgement of every active incident on the selected member.
ui_path = 'src/aruba_mini_dashboard/ui/main_window.py'
replace_once(
    ui_path,
    '''class MainWindow(QMainWindow):
    acknowledge_requested = Signal(str)
    acknowledge_global_requested = Signal()
''',
    '''class MainWindow(QMainWindow):
    acknowledge_requested = Signal(str)
    acknowledge_global_requested = Signal()
    connection_type_baseline_requested = Signal(str)
''',
)

old_ui_methods = '''    @Slot()
    def _selection_changed(self) -> None:
        if self._current_view is None or self.coordinator.busy:
            self.ack_button.setEnabled(False)
            self.compact_ack_action.setEnabled(False)
            return
        selected_ip = self._selected_ip()
        if selected_ip:
            selected_device = next(
                (device for device in self._current_view.devices if device.ip == selected_ip),
                None,
            )
            enabled = bool(
                selected_device is not None
                and self._device_is_registered(selected_device)
                and (
                    selected_ip in self._current_view.problem_ips
                    or self._has_active_incident_for_ip(selected_ip)
                )
            )
        else:
            enabled = (
                len(self._current_view.problem_ips) == 1
                or self._has_active_collection_incident()
            )
        self.ack_button.setEnabled(enabled)
        self.compact_ack_action.setEnabled(enabled)

    @Slot()
    def _acknowledge_selected(self) -> None:
        if self.coordinator.busy:
            self.statusBar().showMessage("점검이 끝난 뒤 알림을 확인 처리하세요.", 5000)
            return
        ip = self._selected_ip()
        if ip:
            selected_device = next(
                (device for device in self._current_view.devices if device.ip == ip),
                None,
            ) if self._current_view else None
            if self._current_view and selected_device is not None and self._device_is_registered(
                selected_device
            ) and (ip in self._current_view.problem_ips or self._has_active_incident_for_ip(ip)):
                self.acknowledge_requested.emit(ip)
                self.statusBar().showMessage(f"{ip}의 현재 알림을 확인 처리했습니다.", 5000)
                return
            self.statusBar().showMessage("선택한 행에는 확인 처리할 활성 문제가 없습니다.", 5000)
            return
        if self._current_view and len(self._current_view.problem_ips) == 1:
            ip = self._current_view.problem_ips[0]
            self.acknowledge_requested.emit(ip)
            self.statusBar().showMessage(f"{ip}의 현재 알림을 확인 처리했습니다.", 5000)
            return
        if self._has_active_collection_incident():
            self.acknowledge_global_requested.emit()
            self.statusBar().showMessage("현재 수집 오류 알림을 확인 처리했습니다.", 5000)
            return
        self.statusBar().showMessage("확인 처리할 문제 IP를 선택하세요.", 5000)
'''
new_ui_methods = '''    def _device_for_ip(self, ip: str) -> DeviceView | None:
        if self._current_view is None:
            return None
        return next(
            (device for device in self._current_view.devices if device.ip == ip),
            None,
        )

    def _connection_type_change_device(self, ip: str) -> DeviceView | None:
        device = self._device_for_ip(ip)
        if device is None:
            return None
        return (
            device
            if bool(value(device.source, "connection_type_changed", False))
            else None
        )

    def _set_acknowledgement_mode(self, connection_type_change: bool) -> None:
        text = (
            "Connection-Type 정상 기준 설정"
            if connection_type_change
            else "알림 확인"
        )
        self.ack_button.setText(text)
        self.compact_ack_action.setText(text)

    @Slot()
    def _selection_changed(self) -> None:
        self._set_acknowledgement_mode(False)
        if self._current_view is None or self.coordinator.busy:
            self.ack_button.setEnabled(False)
            self.compact_ack_action.setEnabled(False)
            return
        selected_ip = self._selected_ip()
        target_ip = selected_ip
        if selected_ip:
            selected_device = self._device_for_ip(selected_ip)
            enabled = bool(
                selected_device is not None
                and self._device_is_registered(selected_device)
                and (
                    selected_ip in self._current_view.problem_ips
                    or self._has_active_incident_for_ip(selected_ip)
                )
            )
        else:
            enabled = (
                len(self._current_view.problem_ips) == 1
                or self._has_active_collection_incident()
            )
            if len(self._current_view.problem_ips) == 1:
                target_ip = self._current_view.problem_ips[0]
        if enabled and target_ip and self._connection_type_change_device(target_ip):
            self._set_acknowledgement_mode(True)
        self.ack_button.setEnabled(enabled)
        self.compact_ack_action.setEnabled(enabled)

    def _confirm_connection_type_baseline(
        self,
        ip: str,
        device: DeviceView,
    ) -> bool:
        current = device.connection_type
        previous = display(
            value(device.source, "previous_connection_type", ""),
            "",
        )
        name = device.alias or device.hostname or ip
        message = (
            f"{name} ({ip})의 현재 Connection-Type을 정상 기준으로 저장합니다.\n\n"
            f"기존 기준: {previous or '-'}\n"
            f"새 기준: {current or '-'}\n\n"
            "이후 새 기준에서 다른 값으로 바뀔 때만 다시 주의 알림을 표시합니다. "
            "같은 IP의 Client 분배 등 다른 장애 알림은 확인 처리하지 않습니다."
        )
        return (
            QMessageBox.question(
                self,
                "현재 Connection-Type 정상 기준 설정",
                message,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    @Slot()
    def _acknowledge_selected(self) -> None:
        if self.coordinator.busy:
            self.statusBar().showMessage("점검이 끝난 뒤 알림을 확인 처리하세요.", 5000)
            return
        ip = self._selected_ip()
        if not ip and self._current_view and len(self._current_view.problem_ips) == 1:
            ip = self._current_view.problem_ips[0]
        if ip:
            selected_device = self._device_for_ip(ip)
            actionable = bool(
                self._current_view
                and selected_device is not None
                and self._device_is_registered(selected_device)
                and (
                    ip in self._current_view.problem_ips
                    or self._has_active_incident_for_ip(ip)
                )
            )
            if not actionable or selected_device is None:
                self.statusBar().showMessage(
                    "선택한 행에는 확인 처리할 활성 문제가 없습니다.",
                    5000,
                )
                return
            connection_change = self._connection_type_change_device(ip)
            if connection_change is not None:
                if not self._confirm_connection_type_baseline(ip, connection_change):
                    self.statusBar().showMessage(
                        "Connection-Type 정상 기준 설정을 취소했습니다.",
                        5000,
                    )
                    return
                self.connection_type_baseline_requested.emit(ip)
                self.statusBar().showMessage(
                    f"{ip}의 현재 Connection-Type을 새 정상 기준으로 저장했습니다. "
                    "최신 상태를 다시 확인합니다.",
                    8000,
                )
                QTimer.singleShot(0, self.coordinator.check_now)
                return
            self.acknowledge_requested.emit(ip)
            self.statusBar().showMessage(f"{ip}의 현재 알림을 확인 처리했습니다.", 5000)
            return
        if self._has_active_collection_incident():
            self.acknowledge_global_requested.emit()
            self.statusBar().showMessage("현재 수집 오류 알림을 확인 처리했습니다.", 5000)
            return
        self.statusBar().showMessage("확인 처리할 문제 IP를 선택하세요.", 5000)
'''
replace_once(ui_path, old_ui_methods, new_ui_methods)

# Update the existing behavior tests and add the migration/acceptance contract.
test_path = 'tests/test_correlation_engine.py'
old_return_test = '''def test_connection_return_to_previous_value_creates_a_new_change_event() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    first = engine.correlate(cycle(membership="group_membership_changed.txt"))
    first_token = next(signal.event_token for signal in first.signals if signal.event_token)
    returned = engine.correlate(cycle(membership="group_membership_initial.txt"))
    returned_signal = next(
        signal for signal in returned.signals if signal.ip == "192.0.2.12" and signal.event_token
    )
    assert returned_signal.event_token != first_token
    assert "Type-B" in returned_signal.reason and "Type-A" in returned_signal.reason
'''
new_return_test = '''def test_connection_return_to_accepted_baseline_recovers_pending_change() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    changed = engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert changed.problem_ips == ["192.0.2.12"]
    assert len(engine.pending_connection_changes()) == 1

    returned = engine.correlate(cycle(membership="group_membership_initial.txt"))

    assert returned.problem_ips == []
    assert engine.pending_connection_changes() == ()
    assert engine.drain_connection_change_resolutions() == {"192.0.2.12"}
'''
replace_once(test_path, old_return_test, new_return_test)

old_ack_test = '''def test_acknowledged_connection_change_leaves_monitoring_active_but_clears_warning() -> None:
    engine = CorrelationEngine()
    engine.correlate(cycle())
    engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert engine.acknowledge_connection_change("192.0.2.12") is True
    health = engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert health.problem_ips == []
    assert health.device_by_ip("192.0.2.12").connection_type == "Type-B"  # type: ignore[union-attr]
'''
new_ack_test = '''def test_accepted_connection_change_becomes_the_new_normal_baseline() -> None:
    store = InMemoryConnectionBaselineStore()
    engine = CorrelationEngine(baseline_store=store)
    engine.correlate(cycle())
    changed = engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert changed.problem_ips == ["192.0.2.12"]
    assert store.get("192.0.2.12").normalized_value == "type a"  # type: ignore[union-attr]

    assert engine.acknowledge_connection_change("192.0.2.12") is True
    accepted = store.get("192.0.2.12")
    assert accepted is not None
    assert accepted.display_value == "Type-B"
    assert accepted.normalized_value == "type b"

    stable = engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert stable.problem_ips == []
    returned = engine.correlate(cycle(membership="group_membership_initial.txt"))
    signal = next(
        item
        for item in returned.signals
        if item.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
    )
    assert "Type-B" in signal.reason and "Type-A" in signal.reason
'''
replace_once(test_path, old_ack_test, new_ack_test)

append_tests = r'''


def test_changed_value_does_not_move_baseline_before_operator_acceptance() -> None:
    store = InMemoryConnectionBaselineStore()
    engine = CorrelationEngine(baseline_store=store)
    engine.correlate(cycle())

    engine.correlate(cycle(membership="group_membership_changed.txt"))
    baseline = store.get("192.0.2.12")

    assert baseline is not None
    assert baseline.display_value == "Type-A"
    assert baseline.normalized_value == "type a"
    assert engine.pending_connection_changes()[0].current_value == "Type-B"


def test_legacy_auto_promoted_baseline_is_restored_from_pending_event() -> None:
    change = ConnectionChange(
        collector_ip="192.0.2.13",
        member_ip="192.0.2.12",
        previous_value="Type-A",
        current_value="Type-B",
        first_detected_at=NOW,
        last_confirmed_at=NOW + timedelta(minutes=1),
    )
    store = InMemoryConnectionBaselineStore(
        (
            ConnectionBaseline(
                collector_ip="192.0.2.11",
                member_ip="192.0.2.12",
                display_value="Type-B",
                normalized_value="type b",
                observed_at=NOW,
            ),
        )
    )

    engine = CorrelationEngine(
        baseline_store=store,
        pending_connection_changes=(change,),
    )

    restored = store.get("192.0.2.12")
    assert restored is not None
    assert restored.display_value == "Type-A"
    assert restored.normalized_value == "type a"
    health = engine.correlate(cycle(membership="group_membership_changed.txt"))
    assert health.problem_ips == ["192.0.2.12"]
'''
test_text = read(test_path)
if 'test_changed_value_does_not_move_baseline_before_operator_acceptance' not in test_text:
    write(test_path, test_text.rstrip() + append_tests + '\n')

contract_path = ROOT / 'tests/test_connection_baseline_acceptance_contract.py'
contract_path.write_text(
    '''from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_main_window_exposes_explicit_connection_type_baseline_action() -> None:
    source = (ROOT / "src/aruba_mini_dashboard/ui/main_window.py").read_text(
        encoding="utf-8"
    )
    assert "connection_type_baseline_requested = Signal(str)" in source
    assert "Connection-Type 정상 기준 설정" in source
    assert "같은 IP의 Client 분배 등 다른 장애 알림은 확인 처리하지 않습니다." in source


def test_runtime_connects_the_explicit_baseline_action() -> None:
    source = (ROOT / "src/aruba_mini_dashboard/main.py").read_text(encoding="utf-8")
    assert "def accept_connection_type_baseline" in source
    assert "runtime.accept_connection_type_baseline" in source
    assert "drain_connection_change_resolutions" in source
''',
    encoding='utf-8',
)

# The patch materializes itself once; the temporary workflow is removed through
# the connected GitHub API after the resulting commit is verified.
Path(__file__).unlink()
