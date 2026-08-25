from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, before: str, after: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    count = text.count(before)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}")
    file.write_text(text.replace(before, after, 1), encoding="utf-8")


# normalize_connection_type removes spaces, hyphens and underscores.
tests = ROOT / "tests/test_correlation_engine.py"
text = tests.read_text(encoding="utf-8")
text = text.replace('normalized_value == "type a"', 'normalized_value == "typea"')
text = text.replace('normalized_value == "type b"', 'normalized_value == "typeb"')
text = text.replace('normalized_value="type b"', 'normalized_value="typeb"')
tests.write_text(text, encoding="utf-8")

# Generic incident acknowledgement must not silently accept a Connection-Type
# baseline. Only the explicit, confirmed UI operation may promote that value.
replace_once(
    "src/aruba_mini_dashboard/main.py",
    '''    def acknowledge_ip(self, ip: str) -> None:
        with self._lock:
            transitions = self.incident_manager.acknowledge_ip(ip)
            if self.engine.acknowledge_connection_change(ip):
                self._pending_connection_acknowledgements.add(ip)
            self._persist_incidents(transitions)
''',
    '''    def acknowledge_ip(self, ip: str) -> None:
        with self._lock:
            transitions: list[Any] = []
            for incident in self.incident_manager.active_incidents():
                if (
                    incident.ip != ip
                    or incident.incident_type is IncidentType.CONNECTION_TYPE_CHANGED
                ):
                    continue
                transition = self.incident_manager.acknowledge(
                    incident.incident_id,
                    now=datetime.now(timezone.utc),
                )
                if transition is not None:
                    transitions.append(transition)
            self._persist_incidents(transitions)
''',
)

contract = ROOT / "tests/test_connection_baseline_acceptance_contract.py"
contract_text = contract.read_text(encoding="utf-8")
if "test_generic_acknowledgement_does_not_accept_connection_baseline" not in contract_text:
    contract_text += '''\n\ndef test_generic_acknowledgement_does_not_accept_connection_baseline() -> None:\n    source = (ROOT / "src/aruba_mini_dashboard/main.py").read_text(encoding="utf-8")\n    start = source.index("    def acknowledge_ip(self, ip: str) -> None:")\n    end = source.index("    def acknowledge_global(self) -> None:", start)\n    body = source[start:end]\n    assert "acknowledge_connection_change" not in body\n    assert "IncidentType.CONNECTION_TYPE_CHANGED" in body\n'''
    contract.write_text(contract_text, encoding="utf-8")

Path(__file__).unlink()
