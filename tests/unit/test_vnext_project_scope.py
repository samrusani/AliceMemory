from __future__ import annotations

import pytest

from alicebot_api.vnext_project_scope import (
    canonical_memory_metadata,
    memory_project_scope,
    normalize_project_identifier,
    normalize_project_scope,
    project_edge_target,
    project_identifier_identity,
    project_scope_identity,
    resolve_project_scope,
    resolve_source_metadata_project_scope,
    source_project_scope,
)


def test_project_scope_finite_integral_numbers_share_one_canonical_identity() -> None:
    assert normalize_project_scope([1, 1.0, 1e0, 1e3, -0.0, True, False]) == (
        "1",
        "1000",
        "0",
        "True",
        "False",
    )
    assert project_scope_identity([1, 1.0, 1e0]) == ("1",)


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), float("-inf"), {"project": 1}, None])
def test_project_scope_rejects_non_integral_nonfinite_and_nonscalar_values(value: object) -> None:
    assert normalize_project_identifier(value) == ""
    assert normalize_project_scope([value]) == ()


def test_legacy_nested_agentic_scope_precedes_singular_project_fallbacks() -> None:
    memory = {
        "project_id": "stale-singular-project",
        "metadata_json": {
            "project_id": "stale-metadata-project",
            "agentic_memory": {
                "project_scope": [" alicebot ", "hermes", "alicebot"],
            },
        },
    }

    assert memory_project_scope(memory) == ("alicebot", "hermes")
    assert canonical_memory_metadata(memory)["project_scope"] == ["alicebot", "hermes"]


def test_nested_agentic_and_identity_canonical_scopes_accept_supported_scalars() -> None:
    resolution = resolve_project_scope(
        {
            "project_id": "stale-project",
            "metadata_json": {
                "project_id": "stale-project",
                "agentic_memory": {"project_scope": " Alpha "},
                "agent_identity": {"project_scope": [7, 1e1, True]},
            },
        }
    )

    assert resolution.present is True
    assert resolution.values == ("Alpha", "7", "10", "True")
    assert resolution.identity == ("10", "7", "alpha", "true")


@pytest.mark.parametrize("nested_key", ["agentic_memory", "agent_identity"])
@pytest.mark.parametrize("canonical_value", [[], None, {"leak": "stale-project"}, 1.5])
def test_nested_canonical_presence_fails_closed_before_stale_singular_aliases(
    nested_key: str,
    canonical_value: object,
) -> None:
    resolution = resolve_project_scope(
        {
            "project_id": "stale-project",
            "metadata_json": {
                "project_id": "stale-project",
                nested_key: {"project_scope": canonical_value},
            },
        }
    )

    assert resolution.present is True
    assert resolution.values == ()
    assert resolution.identity == ()


def test_canonical_top_level_scope_cannot_be_widened_by_stale_nested_scope() -> None:
    memory = {
        "project_id": "alicebot",
        "metadata_json": {
            "project_scope": ["alicebot"],
            "agentic_memory": {"project_scope": ["alicebot", "stale-other-project"]},
        },
    }

    assert memory_project_scope(memory) == ("alicebot",)


def test_explicit_empty_canonical_scope_suppresses_all_legacy_fallbacks() -> None:
    memory = {
        "project_scope": [],
        "project_id": "stale-singular-project",
        "metadata_json": {
            "project_scope": ["stale-metadata-project"],
            "project_id": "stale-metadata-project",
            "agentic_memory": {"project_scope": ["stale-nested-project"]},
        },
    }

    assert memory_project_scope(memory) == ()
    assert canonical_memory_metadata(memory)["project_scope"] == []


def test_explicit_empty_metadata_scope_suppresses_singular_and_nested_fallbacks() -> None:
    memory = {
        "project_id": "stale-singular-project",
        "metadata_json": {
            "project_scope": [],
            "project_id": "stale-metadata-project",
            "agentic_memory": {"project_scope": ["stale-nested-project"]},
        },
    }

    assert memory_project_scope(memory) == ()
    assert canonical_memory_metadata(memory)["project_scope"] == []


def test_source_row_scope_honors_authoritative_embedded_canonical_envelope() -> None:
    empty = {
        "project_id": "pre-envelope-row-alias",
        "metadata_json": {
            "project_id": "stale",
            "metadata_json": {"project_scope": []},
        },
    }
    populated = {
        "project_id": "pre-envelope-row-alias",
        "metadata_json": {
            "project_id": "stale",
            "metadata_json": {"project_scope": ["real"]},
        },
    }

    assert source_project_scope(empty) == ()
    assert source_project_scope(populated) == ("real",)


def test_source_row_scope_retains_pre_envelope_top_level_alias_as_final_fallback() -> None:
    source = {
        "project_id": "legacy-project",
        "metadata_json": {"raw_text": "Legacy adapter source"},
    }

    assert source_project_scope(source) == ("legacy-project",)


def test_scope_identity_collapses_case_whitespace_order_and_duplicates() -> None:
    assert project_scope_identity([" Beta ", "ALICE", "alice", "beta"]) == (
        "alice",
        "beta",
    )


def test_scope_identity_is_ascii_case_insensitive_but_unicode_case_exact() -> None:
    assert project_identifier_identity("  ALICE\tBOT\n") == "alice bot"
    assert project_scope_identity(["İ", "i"]) == ("i", "İ")
    assert project_scope_identity(["Straße", "STRASSE"]) == ("Straße", "strasse")
    assert project_scope_identity(["Σ", "σ", "ς"]) == ("Σ", "ς", "σ")
    assert project_identifier_identity("Straße") == "Straße"
    assert project_identifier_identity("Σ") == "Σ"


def test_scope_identity_normalizes_only_the_declared_ascii_whitespace_set() -> None:
    assert normalize_project_identifier("\t Alice\n\r Bot\f\v") == "Alice Bot"
    assert project_identifier_identity("\u00a0Alice\u00a0") == "\u00a0Alice\u00a0"
    assert project_identifier_identity("\u2003Alice\u2003") == "\u2003Alice\u2003"
    assert project_identifier_identity("\u00a0Alice\u00a0") != project_identifier_identity("\u00a0alice\u00a0")


def test_scope_identity_mixed_ascii_unicode_order_is_codepoint_deterministic() -> None:
    assert project_scope_identity(["é", "Z", "Ä", "a", "z", "İ", "i"]) == (
        "a",
        "i",
        "z",
        "Ä",
        "é",
        "İ",
    )


def test_malformed_present_canonical_scope_fails_closed_without_legacy_fallback() -> None:
    resource = {
        "project_id": "stale-direct",
        "metadata_json": {
            "project_scope": "stale-string",
            "agentic_memory": {"project_scope": ["stale-nested"]},
        },
    }

    resolution = resolve_project_scope(resource)

    assert resolution.present is True
    assert resolution.values == ()
    assert resolution.identity == ()


def test_resolver_precedence_matrix_covers_every_canonical_and_legacy_tier() -> None:
    lower = {
        "project_id": "root-id",
        "project": "root-project",
        "projects": ["root-projects"],
        "metadata_json": {
            "project_scope": ["metadata-canonical"],
            "project_id": "metadata-id",
            "agentic_memory": {
                "project_scope": ["metadata-agentic-scope"],
                "project_id": "metadata-agentic-id",
            },
            "agent_identity": {"project_scope": ["metadata-identity-scope"]},
        },
        "scope_json": {
            "project_scope": ["scope-canonical"],
            "project_id": "scope-id",
            "agentic_memory": {
                "project_scope": ["scope-agentic-scope"],
                "project_id": "scope-agentic-id",
            },
            "agent_identity": {"project_scope": ["scope-identity-scope"]},
        },
    }
    assert resolve_project_scope({"project_scope": ["root-canonical"], **lower}).values == ("root-canonical",)
    assert resolve_project_scope(lower).values == ("metadata-canonical",)
    without_metadata_canonical = {
        **lower,
        "metadata_json": {key: value for key, value in lower["metadata_json"].items() if key != "project_scope"},
    }
    assert resolve_project_scope(without_metadata_canonical).values == ("scope-canonical",)

    nested_only = {
        **without_metadata_canonical,
        "scope_json": {key: value for key, value in lower["scope_json"].items() if key != "project_scope"},
    }
    assert resolve_project_scope(nested_only).values == (
        "metadata-agentic-scope",
        "metadata-identity-scope",
        "scope-agentic-scope",
        "scope-identity-scope",
    )

    aliases_only = {
        "project_id": "root-id",
        "project": "root-project",
        "projects": ["root-projects"],
        "metadata_json": {"project_id": "metadata-id"},
    }
    assert resolve_project_scope(aliases_only).values == (
        "root-id",
        "root-project",
        "root-projects",
    )

    final_aliases = {
        "metadata_json": {
            "project_id": "metadata-id",
            "project": "metadata-project",
            "projects": ["metadata-projects"],
            "agentic_memory": {
                "project_id": "metadata-agentic-id",
                "project": "metadata-agentic-project",
                "projects": ["metadata-agentic-projects"],
            },
            "agent_identity": {"project_id": "ignored-identity-id"},
        },
        "scope_json": {
            "project_id": "scope-id",
            "project": "scope-project",
            "projects": ["scope-projects"],
            "agentic_memory": {
                "project_id": "scope-agentic-id",
                "project": "scope-agentic-project",
                "projects": ["scope-agentic-projects"],
            },
            "agent_identity": {"project_id": "ignored-scope-identity-id"},
        },
    }
    assert resolve_project_scope(final_aliases).values == (
        "metadata-id",
        "metadata-project",
        "metadata-projects",
        "metadata-agentic-id",
        "metadata-agentic-project",
        "metadata-agentic-projects",
        "scope-id",
        "scope-project",
        "scope-projects",
        "scope-agentic-id",
        "scope-agentic-project",
        "scope-agentic-projects",
    )


def test_source_metadata_adapter_exposes_envelope_and_direct_legacy_nested_forms() -> None:
    assert resolve_source_metadata_project_scope(
        {
            "project_id": "stale-root-alias",
            "agent_identity": {"project_scope": ["direct-identity-scope"]},
        }
    ).values == ("direct-identity-scope",)
    assert resolve_source_metadata_project_scope(
        {
            "project_id": "stale-root-alias",
            "scope_json": {"project_scope": ["scope-container-canonical"]},
            "agentic_memory": {"project_scope": ["stale-direct-agentic"]},
        }
    ).values == ("scope-container-canonical",)


PROJECT_UUID = "0fcdd69c-67e1-47c9-a2fe-2f873c484e68"


@pytest.mark.parametrize(
    "value",
    [
        PROJECT_UUID,
        PROJECT_UUID.upper(),
        "0FCDD69C-67e1-47C9-a2fe-2F873c484E68",
        f" {PROJECT_UUID} ",
        f"\t{PROJECT_UUID.upper()}\n",
        f"\r\n  {PROJECT_UUID.upper()}\f\v",
    ],
)
def test_project_edge_target_folds_a_hyphenated_uuid_to_its_canonical_form(value: str) -> None:
    assert project_edge_target(value) == PROJECT_UUID
    # The edge names the same project identity the stored scope holds for that spelling.
    assert project_identifier_identity(project_edge_target(value)) == project_identifier_identity(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("alpha-team", "alpha-team"),
        ("  alpha-team  ", "alpha-team"),
        ("\talpha-team\n", "alpha-team"),
        ("ALPHA-TEAM", "ALPHA-TEAM"),
        ("alpha   team", "alpha team"),
        ("prj_0123456789abcdef", "prj_0123456789abcdef"),
        ("Café", "Café"),
        ("\u00a0alpha\u00a0", "\u00a0alpha\u00a0"),
    ],
)
def test_project_edge_target_keeps_the_stored_scope_text_for_any_other_name(value: str, expected: str) -> None:
    assert project_edge_target(value) == expected
    assert normalize_project_scope([value]) == (expected,)


@pytest.mark.parametrize(
    "value",
    [
        "{" + PROJECT_UUID.upper() + "}",
        "URN:UUID:" + PROJECT_UUID.upper(),
        PROJECT_UUID.upper().replace("-", ""),
        PROJECT_UUID.upper()[:-1],
        PROJECT_UUID.upper() + "0",
        "Z" + PROJECT_UUID.upper()[1:],
        "xx " + PROJECT_UUID.upper(),
    ],
)
def test_project_edge_target_reads_only_the_hyphenated_spelling_as_a_uuid(value: str) -> None:
    # Any other spelling is a different identity in the scope helpers, so it stays the text the scope holds.
    assert project_edge_target(value) == normalize_project_identifier(value)
    assert project_edge_target(value) != value.lower()


@pytest.mark.parametrize("value", ["", " ", "\t\n  ", None])
def test_project_edge_target_is_empty_when_no_project_is_named(value: object) -> None:
    assert project_edge_target(value) == ""

