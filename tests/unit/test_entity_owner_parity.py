"""The complete default and explicit owner/admin responses retain main's bytes."""

import json
from pathlib import Path

from tests.unit.entity_owner_parity_support import (
    UNADMITTED_ENTITIES,
    drop_documented_delta,
    reader_grounding,
    record,
    record_orphans,
)


def test_entity_owner_and_unbound_admin_full_response_parity(tmp_path):
    output = tmp_path / "actual.json"
    record(tmp_path / "vault", output)
    expected = Path(__file__).with_name("fixtures_entity_owner_parity.json")
    assert output.read_bytes() == expected.read_bytes()


def test_owner_and_unbound_admin_keep_main_grounding_for_unlinked_entities(tmp_path):
    """An entity with no readable link is still known to the keyless owner and an unbound admin key.

    The vault holds Marcus Chen with no link, Elena Voss linked only to a memory the default sensitivity selection
    hides, and Meridian with a readable link. The query names all three and Tobias Wren, who is in no row. On main
    both callers get a grounding block that flags Tobias Wren alone, because the entity table knows the other two. The
    fixture is main's recorded answer. Nothing but the two unlinked rows (not listed for any caller by design) and
    the token counters that price them may differ, so the whole response is compared, not only the grounding.

    Mutations, each one alone: in ``compile_context_pack`` pass ``allow_entity_lookup=False``; make
    ``SourceReadFence.entity_read_fenced`` always true. Both flag Marcus Chen and Elena Voss for the owner and admin.
    """
    vault = tmp_path / "vault"
    output = tmp_path / "actual.json"
    record_orphans(vault, output)
    actual = json.loads(output.read_text())
    expected = json.loads(Path(__file__).with_name("fixtures_entity_orphan_parity.json").read_text())
    assert sorted(actual) == sorted(expected)
    assert len(actual) == 6, "two callers through recall, the MCP pack and the HTTP pack"
    main_grounding = {"checked": 4, "unsupported_entities": ["Tobias Wren"]}
    for key, response in actual.items():
        assert [row["name"] for row in expected[key]["entities"]] == ["Meridian", *UNADMITTED_ENTITIES], key
        assert [row["name"] for row in response["entities"]] == ["Meridian"], key
        if key.endswith("alice_recall"):
            assert "grounding" not in response, key
        else:
            assert response["grounding"] == expected[key]["grounding"] == main_grounding, key
    assert drop_documented_delta(actual) == drop_documented_delta(expected)
    # The fence is what separates the callers: a read-only key on the same vault gets the unlinked names flagged.
    assert reader_grounding(vault) == {
        "checked": 4,
        "unsupported_entities": [*UNADMITTED_ENTITIES, "Tobias Wren"],
    }
