"""The complete default and explicit owner/admin responses retain main's bytes."""

from pathlib import Path

from tests.unit.entity_owner_parity_support import record


def test_entity_owner_and_unbound_admin_full_response_parity(tmp_path):
    output = tmp_path / "actual.json"
    record(tmp_path / "vault", output)
    expected = Path(__file__).with_name("fixtures_entity_owner_parity.json")
    assert output.read_bytes() == expected.read_bytes()
