"""The credential floor's detector, as ruled by the S4.4 design round.

Why this file exists. The first shared check (41faa9a, 2026-09-22) joined
every field in reading order and paired every mapping key with its value. The
review of it confirmed it refused honest exports (a summary is a prefix of
the text, and the empty join glued the text's end to its start), refused
structural bodies such as {"fact_key": "release_mode"}, refused "Meeting with
Akia" + "Unfortunately...", and still missed a key split across a title and a
mapping body. A design round (three designs scored on a held-out corpus no
designer saw, adversaries, a judge) chose "precision-first plus keyed pairs".
These are its required tests T1 to T8 and T10, dated 2026-09-23, plus the
addendum's real-key tests (F4) and cross-seam offsets (F5). T9, the mutation
proofs, is run by the builder and reported, not asserted here.

How the earlier version escaped. Its tests fed hand-made strings; nobody fed
it a real OpenSSH private key (the addendum found the design missed every
unencrypted one), an import-shaped row, or a structural mapping. Every case
here is a shape the product actually writes or a key a tool actually emits.

Synthetic keys are built from parts so no literal in this file has the shape
secret scanners look for; the real throwaway keys live base64-encoded in
fixtures_throwaway_keys.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from alicebot_api import credential_floor
from alicebot_api.agent_key_format import AGENT_KEY_PREFIX
from alicebot_api.credential_floor import (
    SECRET_PREFIX_PATTERNS,
    VERDICT_CREDENTIAL,
    VERDICT_EXPANSION,
    carries_credential_material,
    credential_verdict,
    refuse_credential_material,
)

from tests.unit.fixtures_floor_regressions import floor_regressions
from tests.unit.fixtures_promotion_corpus import BUILDER_NOTES, REVIEWER_NOTES
from tests.unit.fixtures_throwaway_keys import (
    PGP_PUBLIC_KEY_NAMES,
    PRIVATE_KEY_NAMES,
    PUBLIC_KEY_NAMES,
    throwaway_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _k(*parts: str) -> str:
    return "".join(parts)


PAT = _k("ghp_", "0123456789abcdefghijklmnopqrstuvwxyz")
HEX64 = "3251d49217b3064ebffc565025e4e36f176ecd6003287505108d1a8ec2fb0d17"
UUID_TEXT = "7d95f1e4-16d1-4361-aa95-e2aaa3684807"
AWS_ID = _k("AKIA", "IOSFODNN7EXAMPLE")
STRIPE_LIVE = _k("sk_", "live_", "HtyWnjfc1gfyJ8UYMDfCXl1w")
STRIPE_TEST = _k("sk_", "test_", "HVMkB7DomldCTC1UHBo0FzsfYuQU")
STRIPE_RK_LIVE = _k("rk_", "live_", "fyK8vdxNk3VcFJ1b1Qe0o9Nx")
STRIPE_RK_TEST = _k("rk_", "test_", "aM7rEJipJmIOkrMqIHc7IbcV")
ALICE_KEY = _k(AGENT_KEY_PREFIX, "Zq8mP2vL9", "kXw4TbN7cRy1uJh6fDs3aGe0oKi5AbCdEf")
HF_TOKEN = _k("hf_", "Zq8mP2vL9kXw4TbN7cRy1uJh6fDs3aGe0o")
SENDGRID = _k("SG", ".", "Zq8mP2vL9kXw4TbN7cRy1u", ".", "Zq8mP2vL9kXw4TbN7cRy1uJh6fDs3aGe0oKi5AbCdEf")
SLACK_HOOK = _k("https://hooks.", "slack.com/services/", "T0123ABCD/B0456EFGH/Zq8mP2vL9kXw4TbN7cRy1uJh")
PYPI = _k("pypi-", "AgE", "IcHlwaS5vcmcCJDEyMzQ1Njc4LTEyMzQtMTIzNC0xMjM0LTEyMzQ1Njc4OTAxMg")


# ---------------------------------------------------------------------------
# T1: the confirmed review findings.
# ---------------------------------------------------------------------------


def test_t1_f1_a_split_across_a_title_and_a_mapping_body_is_caught() -> None:
    assert carries_credential_material("id AK", "IAIOSFODNN7EXAMPLE")
    assert carries_credential_material("id AK", {"text": "IAIOSFODNN7EXAMPLE"})


def test_t1_f3_an_honest_import_row_does_not_read_its_own_end_against_its_start() -> None:
    text = "Consolidation of the archived sources completed overnight. Trip with Akia"
    assert not carries_credential_material("Trip with Akia", text, text[:280], text[:40] + "…", {"text": text})


def test_t1_f3_a_summary_copy_does_not_glue_the_text_end_to_its_start() -> None:
    """The review's finding behind F3: import refused its own honest export.

    The text starts with an upper-case id and ends in AKIA. Read with its
    summary (text[:280]) and value.text after it, the empty join put the
    text's end against its own start and spelled an AWS id. The derived-copy
    break drops the copies from the join. (F3 as the spec words it, a text
    ending "Trip with Akia", no longer needs the break: the cross-field AWS
    shape is case-exact.)
    """

    text = "IOSFODNN7EXAMPLE rollout notes " + "rollout notes " * 3 + "AKIA"
    assert not carries_credential_material("Rollout", text, text[:280], {"text": text})
    # Guards the guard: without the copies, the same seam is a key.
    assert carries_credential_material("AKIA", "IOSFODNN7EXAMPLE rollout notes")


@pytest.mark.parametrize(
    "body",
    [
        {"fact_key": "release_mode"},
        {"key": "Q3-plan"},
        {"token_budget": 128000},
        {"subject_key": "project.alice.release", "subject": "alice"},
        {"openclaw_dedupe_key": HEX64},
    ],
)
def test_t1_f4_structural_bodies_are_not_secrets(body: dict[str, object]) -> None:
    assert not carries_credential_material("Decision: release", body)


@pytest.mark.parametrize(
    "fields",
    [
        ("Meeting with Akia", "Unfortunately we slipped"),
        ("Meeting with Akia Unfortunately we slipped",),
        ("Meeting with AkiaUnfortunately we slipped",),
        ("Ring Bearer", "Responsibilities include holding the rings."),
        ("Ring Bearer Responsibilities include holding the rings.",),
    ],
)
def test_t1_f5_names_next_to_words_are_not_keys(fields: tuple[str, ...]) -> None:
    assert not carries_credential_material(*fields)


@pytest.mark.parametrize(
    "text",
    ["We use sk-learn for the churn model baseline.", "Prefer ssh-ed25519 keys over ssh-rsa keys"],
)
def test_t1_f6_prefix_words_without_key_material_are_not_keys(text: str) -> None:
    assert not carries_credential_material(text)


def test_t1_f7_identifier_fields_are_read_by_value() -> None:
    base = ("title", "body", None, [], None)
    assert carries_credential_material(*base, PAT, None, None)  # idempotency_key
    assert carries_credential_material(*base, None, PAT, None)  # trace_id
    assert carries_credential_material(*base, None, None, ["alice", PAT])  # project_scope
    assert carries_credential_material({"a": {"b": ["note", STRIPE_LIVE]}})  # a metadata_json leaf
    assert carries_credential_material(_k("xoxb-", "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"))
    assert carries_credential_material({"api_key": _k("Xq9mZt2L", "xP9wKc4BVq7m")})
    assert not carries_credential_material(*base, UUID_TEXT, "trace-" + UUID_TEXT, [HEX64])


# ---------------------------------------------------------------------------
# T2: Stripe (owner ruling C3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [STRIPE_LIVE, STRIPE_TEST, STRIPE_RK_LIVE, STRIPE_RK_TEST])
def test_t2_stripe_keys_whole_split_and_in_a_list_leaf(key: str) -> None:
    assert carries_credential_material(key)
    third = len(key) // 3
    assert carries_credential_material(key[:third], key[third : 2 * third], key[2 * third :])
    assert carries_credential_material("config", ["note", f"creds: {key} for CI"])


# ---------------------------------------------------------------------------
# T3: adversary false positives that must stay False.
# ---------------------------------------------------------------------------

_K8S = "apiVersion: v1\nkind: Pod\nspec:\n  volumes:\n    - name: creds\n      secret:\n        secretName: app-db-credentials\n"

_T3_FALSE: dict[str, tuple[object, ...]] = {
    "password policy": ("The password policy is 12-character minimum with one symbol.",),
    "token lifetime": ("The access token lifetime is 15-minutes; refresh tokens last 30 days.",),
    "1password": ("Our team password manager is 1Password.",),
    "key rotation": ("In Q3 we begin private key rotation for the signing service.",),
    "provider status": ("Provider status after setup: OpenAI API key: Configured, Anthropic API key: Missing.",),
    "auth token status": ("Hermes status check: auth token: Expired, reconnect from settings.",),
    "api key required": ("Anthropic provider: API key: Required, base URL: optional.",),
    "wifi 1password": ("Where the wifi password is", "1Password, in the shared Home vault."),
    "os.environ": ("api_key=os.environ['OPENAI_API_KEY']",),
    "k8s secretName": (_K8S,),
    "ring bearer uuid": ("...Leo is Ring Bearer", str(uuid.UUID(int=7))),
    "vpn source ref": ("Find out what the new VPN password is", ["source:" + str(uuid.UUID(int=8))]),
    "sk-learn scope": ("title", "body", None, [], None, None, None, ["sk-learn-bench-2024"]),
    "SK branch": ("Work is on branch SK-1482-fix-retry-backoff until review.",),
    "idempotency header": (f"Send Idempotency-Key: {UUID_TEXT} with each POST.",),
    "secret provider": ("ALICE_SECRET_PROVIDER=encrypted_local",),
    "sort key": ("SORT_KEY=created_at-desc",),
    "cache key": ("CACHE_KEY=user.v2",),
    "next page token": ({"next_page_token": _k("CiAKGj", "BpNDd2")},),
    "csrf token": ({"csrf_token": "a8Fk2LmQ9x"},),
    "session key": ({"session_key": UUID_TEXT},),
    "template password": ("password=${DB_PASSWORD}",),
    "shell variable": ("PGPASSWORD=$DB_PASSWORD",),
    "vault url": ("password: https://vault.internal/item/42",),
    "npm config": ("set npm_config_registry to the mirror",),
    "prefix docs": ("Fine-grained tokens start with github_pat_ and classic ones with ghp_",),
    "caps akia split": ("MEETING WITH AKIA", "UNFORTUNATELY WE SLIPPED THE DATE"),
}


@pytest.mark.parametrize("name", sorted(_T3_FALSE))
def test_t3_adversary_false_positives_stay_clean(name: str) -> None:
    assert not carries_credential_material(*_T3_FALSE[name]), name


@pytest.mark.parametrize(
    "text",
    [
        _k("DB_PASSWORD=", "Winter.Is.Coming2024"),
        _k("db_password: ", "Winter.Is.Coming2024"),
        # Two T3 cases until round 5, now refused: a dotted value under a
        # password name reads the same as a dotted password.
        "password: process.env.DB_PASSWORD",
        "password = var.db_password",
    ],
)
def test_round5_a_dotted_value_under_a_password_name_is_refused(text: str) -> None:
    """S4.4 round 5 (2026-09-23): the dotted-reference exemption applied to
    every name kind, so a DB_PASSWORD set to Winter.Is.Coming2024 was read as a code
    reference and stored, where v0.16.0 refused it. The panel ruled that no
    exemption applies to a password-kind name; the two T3 notes that name a
    variable after a password: label are the price, documented as an
    over-refusal."""

    assert carries_credential_material(text)
    # Guards the guard: under an API key name a code reference still passes.
    assert not carries_credential_material("api_key = settings.OPENAI_API_KEY")


# ---------------------------------------------------------------------------
# T4: adversary misses that must be caught.
# ---------------------------------------------------------------------------

# A made-up value in a vendor key's shape. It is assembled at runtime so
# repository secret scanners do not read this fixture as a live key.
_FAKE_MISTRAL_KEY = "8fJ2kQzX" + "7bVn3LmP0wYcT5rHdG1sAeUo"

_T4_TRUE: dict[str, tuple[object, ...]] = {
    "mistral yaml": ("mistral_key: " + _FAKE_MISTRAL_KEY,),
    "cohere": (_k("cohere_key = '", "9aKd82LmQzX7bVn3LmP0wYcT5rHdG1s'"),),
    "fernet": (_k("fernet_key = ", "bXkZ9q2LmP0wYcT5rHdG1sAeUo8fJ2kQzX7bVn3LmP0="),),
    "mistral pair (weak tier)": ({"mistral_key": _FAKE_MISTRAL_KEY},),
    "apim header": (_k("Ocp-Apim-Subscription-Key: ", "3f2a9c7e1b4d4e8f9a0b1c2d3e4f5a6b"),),
    "functions header": (_k("x-functions-key: ", "Zq8mP2vL9kXw4TbN7cRy1uJh6fDs3aGe0oKi5"),),
    "DB_PASSWORD_RO": (_k("DB_PASSWORD_RO=", "Kd9xoYWu83nq"),),
    "MAPBOX_TOKEN_V2": (_k("MAPBOX_TOKEN_V2=", "pk9aKd82LmQzX7bVn3LmP0wYcT5"),),
    "JWT_SECRET_PREVIOUS": (_k("JWT_SECRET_PREVIOUS=", "Zq8mP2vL9kXw4TbN7cRy1u"),),
    "API_TOKEN_STG": (_k("API_TOKEN_STG=", "Zq8mP2vL9kXw4TbN7cRy1u"),),
    "SECRET_KEY_BASE": (_k("SECRET_KEY_BASE=", "Zq8mP2vL9kXw4TbN7cRy1uJh6fDs3aGe"),),
    "alice_sk_": (ALICE_KEY,),
    "hf_": (HF_TOKEN,),
    "sendgrid": (SENDGRID,),
    "slack webhook": (SLACK_HOOK,),
    "pypi": (PYPI,),
    "POSTGRES_PASSWORD punctuated": ("POSTGRES_PASSWORD=Kd!XoYWu83nq",),
    "database_password punctuated": ("database_password: UCx#9mZt2Lq",),
    "postgres url": ("postgres://app:Kd9xoYWu83nq@db.internal:5432/app",),
    "mongodb+srv url": ("mongodb+srv://app:67nMb5Sgiepu9dlB@cluster0.example.net/app",),
    "password pair": ({"password": "Tr0ub4dor&3"},),
    "API_KEY pair": ({"API_KEY": _k("abc123", "def456ghi789")},),
}


@pytest.mark.parametrize("name", sorted(_T4_TRUE))
def test_t4_adversary_misses_are_caught(name: str) -> None:
    assert carries_credential_material(*_T4_TRUE[name]), name


def test_product_rollup_key_is_exempt_only_where_the_product_writes_it() -> None:
    """The floor does not exempt rollup_key. Import unwraps two product paths.

    metadata_json unwraps a rollup_key whose value matches the producer:
    an optional scope:<16 hex>: prefix, then topic, entity, or semantic,
    then a label. A label passes when it has at least one letter or digit,
    no uppercase or titlecase character, no control, format, surrogate,
    private-use, or unassigned character, and no whitespace other than a
    plain space. A no-break space and an ideographic space are refused.
    Mutation: allow U+00A0. The no-break space case fails. Allowing a tab
    does not, because a tab is already refused as a control character.
    The import value column unwraps only
    value.rollup.rollup_key with that shape. A 64-hex scope is not that
    shape. A caller-supplied rollup_key over an opaque value is refused.
    The label is still read by value. rollupKey stays a weak name.
    """

    import hashlib

    from alicebot_api.credential_floor import _KEY_STRUCTURAL, _name_kind, is_product_rollup_key
    from alicebot_api.onramp import _without_product_value_rollup_key, _without_system_keys

    digest16 = hashlib.sha256(b"games").hexdigest()[:16]
    topic = "scope:" + digest16 + ":topic:games"
    entity = "scope:" + digest16 + ":entity:naïve"
    semantic = "semantic:kitchen"
    spaced = "entity:model-2024 labs"
    hyphenated = "topic:gpt-4o"
    digest64 = hashlib.sha256(b"games").hexdigest()
    full_value = "scope:" + digest64 + ":topic:games"
    assert any(character.isdigit() for character in topic)
    assert any(ord(character) > 127 and character.islower() for character in entity)
    assert "rollup" not in _KEY_STRUCTURAL
    assert _name_kind("rollup_key", "", 0) == "weak"
    assert _name_kind("rollupKey", "", 0) == "weak"
    opaque = "Xq9mZt2L" + "xP9wKc4BVq7m"
    one_and_one = "entity:1&1"
    ampersand = "scope:" + digest16 + ":entity:barnes&noble"
    cjk = "entity:" + "東京"
    dotted = "scope:" + digest16 + ":entity:" + "i\u0307stanbul.online"
    for product in (topic, entity, semantic, spaced, hyphenated, one_and_one, ampersand, cjk, dotted):
        assert is_product_rollup_key(product), product
        assert not carries_credential_material(_without_system_keys({"rollup_key": product})), product
    assert not is_product_rollup_key(full_value)
    assert not is_product_rollup_key("entity:Barnes")
    assert not is_product_rollup_key("entity:acme\x01labs")
    assert not is_product_rollup_key("topic:fy\t2024")
    assert not is_product_rollup_key("topic:fy\u00a02024")
    assert not is_product_rollup_key("topic:fy\u30002024")
    assert not is_product_rollup_key("entity:" + "\u01c5" + "z")
    assert not is_product_rollup_key("topic:a\u200bb")
    assert not is_product_rollup_key("semantic:&&&")
    assert carries_credential_material({"rollup_key": topic})
    assert carries_credential_material({"rollup_key": entity})
    assert carries_credential_material({"rollup_key": opaque})
    assert carries_credential_material({"rollup": {"rollup_key": opaque}})
    assert carries_credential_material(_without_system_keys({"rollup_key": full_value}))
    assert carries_credential_material(_without_system_keys({"rollup_key": opaque}))
    mixed = "wJalr" + "XUtn" + "FEMI7" + "K7MDENG"
    mixed_key = "scope:" + digest16 + ":topic:" + mixed
    assert not is_product_rollup_key(mixed_key)
    assert carries_credential_material({"rollup_key": mixed_key})
    sk_key = "scope:" + digest16 + ":topic:" + ("sk-" + "abcdefghijklmnopqrstuvwxyz12")
    xoxb_key = "topic:" + ("xoxb-" + "123456789012" + "-" + "abcdefghijklm")
    assert is_product_rollup_key(sk_key)
    assert is_product_rollup_key(xoxb_key)
    assert carries_credential_material(_without_system_keys({"rollup_key": sk_key}))
    assert carries_credential_material(_without_system_keys({"rollup_key": xoxb_key}))
    assert carries_credential_material(
        _without_product_value_rollup_key({"text": "A note.", "rollup": {"rollup_key": sk_key}})
    )
    card = {"text": "Played several games.", "rollup": {"rollup_key": topic, "group_kind": "topic"}}
    assert not carries_credential_material(_without_product_value_rollup_key(card))
    entity_card = {"text": "NVIDIA shipped a board.", "rollup": {"rollup_key": entity, "group_kind": "entity"}}
    assert not carries_credential_material(_without_product_value_rollup_key(entity_card))
    assert carries_credential_material(
        _without_product_value_rollup_key({"text": "A note.", "rollup": {"rollup_key": opaque}})
    )
    assert carries_credential_material(
        _without_product_value_rollup_key({"text": "A note.", "rollup_key": topic})
    )
    assert carries_credential_material("rollup_key=" + opaque)
    assert carries_credential_material({"rollupKey": opaque})
    assert carries_credential_material({"ROLLUP_KEY": opaque})
    assert carries_credential_material({"rollup-key": opaque})
    assert carries_credential_material({"Rollup-Key": opaque})


def test_a_routing_session_key_is_an_identifier_and_gpg_key_is_not() -> None:
    """session_key over a routing id is an identifier. A wider profile, a
    plus or UUID tail, and a numeric topic suffix are included. An uppercase
    profile and a Slack C04 tail are refused. gpg_key over a key id is
    refused. The signing-key exemption is only the name signingkey.
    """

    routing = "agent:main:telegram:dm:4471"
    negative = "agent:main:telegram:group:-1001234567890"
    opaque = "Xq9mZt2L" + "xP9wKc4BVq7m"
    key_id = "A1B2" + "C3D4" + "E5F6" + "7890"
    kept = (
        routing,
        negative,
        "agent:main-bot:telegram:dm:4471",
        "agent:work2:telegram:dm:4471",
        "agent:ops_bot:telegram:dm:4471",
        "agent:main:whatsapp:dm:+447911123456",
        "agent:main:subagent:run:12345678-9abc-def0-1234-56789abcdef0",
        "agent:main:telegram:dm:4471:topic:3",
        "agent:main-bot:whatsapp:dm:+447911123456:topic:12",
    )
    for value in kept:
        assert not carries_credential_material({"session_key": value}), value
    assert carries_credential_material({"session_key": "x" + routing})
    assert carries_credential_material({"session_key": "agent:main:telegram:dm:" + opaque})
    assert carries_credential_material({"SESSION_KEY": routing})
    assert carries_credential_material({"session_key": "agent:Main:telegram:dm:4471"})
    assert carries_credential_material({"session_key": "agent:main:slack:channel:" + "C04" + "ABCDEF12"})
    assert carries_credential_material(
        {"session_key": "agent:main:subagent:run:12345678-9ABC-DEF0-1234-56789ABCDEF0"}
    )
    assert carries_credential_material({"session_key": "agent:main:telegram:dm:4471:topic:games"})
    assert carries_credential_material({"session_key": opaque})
    assert carries_credential_material({"gpg_key": key_id})
    assert not carries_credential_material({"signingkey": key_id})


def test_mcp_review_provenance_schema_allows_only_five_keys() -> None:
    from alicebot_api.mcp.definitions import _REVIEW_PROVENANCE_SCHEMA

    assert _REVIEW_PROVENANCE_SCHEMA["additionalProperties"] is False
    assert set(_REVIEW_PROVENANCE_SCHEMA["properties"]) == {
        "source_id",
        "source_chunk_id",
        "evidence_role",
        "confidence",
        "quote",
    }


def test_keyed_reading_measurement_counts_notes_at_the_doors() -> None:
    from scripts.measure_keyed_credential_reading import count_notes, synthetic_notes

    counts = count_notes(synthetic_notes())
    assert "rollup_key text" in counts["commit"]
    assert "rollupKey" in counts["commit"]
    assert "stripeKey" in counts["commit"]
    assert "openaiKey" in counts["commit"]
    assert "ordinary" not in counts["commit"]
    assert "product rollup_key" not in counts["import_before"]
    assert "product rollup_key" not in counts["import_after"]
    assert "product entity rollup_key" not in counts["import_before"]
    assert "product entity rollup_key" not in counts["import_after"]
    assert "continuity body rollup_key" not in counts["import_before"]
    assert "continuity body rollup_key" in counts["import_after"]
    assert "rollupKey" in counts["import_after"]
    assert "rollupKey" not in counts["import_before"]
    assert "session_key" not in counts["import_after"]
    assert "gpg_key" in counts["import_after"]
    assert "gpg_key" not in counts["import_before"]
    assert "dedupe" not in counts["import_after"]
    assert len(counts["import_after"]) > len(counts["import_before"])


# ---------------------------------------------------------------------------
# T5: linear time. A generous absolute budget and a ratio, never a tight
# wall clock. Measured on the build machine (Apple M3 Max) after round 3, on
# 2026-09-23, best of three, over three runs after round 4: every one of the
# ten shapes below takes at most 0.34 s at 200 KB, and the 200 KB to 50 KB
# ratio is 3.8 to 5.0 (4 is linear, 16 quadratic).
# ---------------------------------------------------------------------------


def _t5_shapes(kb: int) -> dict[str, tuple[object, ...]]:
    n = kb * 1000
    fdfa = "ﷺ" * (n // 3)
    return {
        "a=": ("a=" * (n // 2),),
        "a = ": ("a = " * (n // 4),),
        "hive path": ("/".join(f"k{i}=v{i}" for i in range(n // 10)),),
        "a:": ("a:" * (n // 2),),
        "password=": ("password=" * (n // 9),),
        "U+FDFA one field": (fdfa,),
        "U+FDFA split": (fdfa[: len(fdfa) // 2], fdfa[len(fdfa) // 2 :]),
        "one-character fields": tuple("x" for _ in range(n // 4)),
        "16k pairs": ({f"k{i}": f"v{i}" for i in range(16_000 * kb // 200)},),
        "16k db password pairs": ({f"db{i}_password": "hunter" for i in range(16_000 * kb // 200)},),
    }


def _best_of_three(fields: tuple[object, ...]) -> float:
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        carries_credential_material(*fields)
        timings.append(time.perf_counter() - started)
    return min(timings)


@pytest.mark.parametrize("name", sorted(_t5_shapes(1)))
def test_t5_every_adversarial_shape_scans_in_linear_time(name: str) -> None:
    small = _best_of_three(_t5_shapes(50)[name])
    big = _best_of_three(_t5_shapes(200)[name])
    assert big < 2.0, (name, big)
    assert big / max(small, 1e-4) < 8, (name, small, big)


# ---------------------------------------------------------------------------
# T6: deep nesting returns, it does not raise RecursionError.
# ---------------------------------------------------------------------------


def test_t6_twenty_thousand_deep_lists_and_dicts_return() -> None:
    deep_list: object = "x"
    deep_dict: object = "x"
    for _ in range(20_000):
        deep_list = [deep_list]
        deep_dict = {"k": deep_dict}
    assert carries_credential_material(deep_list) is False
    assert carries_credential_material(deep_dict) is False
    assert credential_floor.string_values(deep_dict) == ["x"]
    nested_key: object = PAT
    for _ in range(20_000):
        nested_key = [nested_key]
    assert carries_credential_material(nested_key) is True


# ---------------------------------------------------------------------------
# T7: documented residuals, pinned so a later change is visible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        ("Office wifi password:", "Sunflower"),
        ("id ak", "iaiosfodnn7example"),
        ("Authorization: Bearer", "Zq8mP2vL9kXw4TbN7cRy1uJh6fDs3aGe"),
        ("API_KEY=", "Xq9mZt2LxP9wKc4BVq7m"),
        ("Prod DB password:", "Kd9xoYWu83nq"),
        ("Prod DB password", _k("Kd9xo", "YWu83nq")),
        ("The vault password", "is hunter2hunter2"),
    ],
)
def test_t7_cross_field_label_splits_are_a_documented_miss(fields: tuple[str, ...]) -> None:
    assert not carries_credential_material(*fields)


def test_t7_a_known_over_refusal_stays_refused() -> None:
    assert carries_credential_material("Bank portal password: Changed after the phishing scare.")


# ---------------------------------------------------------------------------
# T8: the 28 held-out regressions the design round released.
# ---------------------------------------------------------------------------

_REGRESSIONS = floor_regressions()


def test_t8_exactly_the_released_records_are_copied() -> None:
    assert len(_REGRESSIONS) == 28
    assert sum(1 for record in _REGRESSIONS if record["label"] == "credential") == 13


@pytest.mark.parametrize("record", _REGRESSIONS, ids=[str(record["id"]) for record in _REGRESSIONS])
def test_t8_held_out_regression(record: dict[str, object]) -> None:
    expected = record["label"] == "credential"
    assert carries_credential_material(*record["fields"]) is expected, record["id"]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T10: the repository's own prose, pinned by exact hit list.
# ---------------------------------------------------------------------------

# Every place the floor fires on the repo's own notes, docs paragraphs and
# commit bodies (up to the S4.4 base commit). All are NAME=value lines with a
# real secret name and a password-shaped value, which the owner ruled to keep
# refusing (addendum F3): the two toy assignments the judge named, and
# placeholder passwords in shell examples. Growth or shrinkage is visible.
_T10_BASE_COMMIT = "880915a"
_T10_EXPECTED_DOC_HITS = {
    "docs/alpha/backup-and-restore.md": 2,
    "docs/deployment/single-tenant-self-hosted.md": 1,
    "docs/memory/promotion-personas.md": 1,
    "docs/runbooks/disaster-recovery.md": 2,
}
_T10_EXPECTED_COMMIT_BODY_HITS = 1


def _doc_hits() -> dict[str, int]:
    hits: dict[str, int] = {}
    paths = sorted((REPO_ROOT / "docs").rglob("*.md")) + [REPO_ROOT / "README.md", REPO_ROOT / "CHANGELOG.md"]
    for path in paths:
        for paragraph in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            if paragraph.strip() and carries_credential_material(paragraph):
                relative = str(path.relative_to(REPO_ROOT))
                hits[relative] = hits.get(relative, 0) + 1
    return hits


def test_t10_the_promotion_corpus_raises_no_credential() -> None:
    notes = [(title, text) for title, text in BUILDER_NOTES] + [(row[0], row[1]) for row in REVIEWER_NOTES]
    assert len(notes) > 90
    assert [note for note in notes if carries_credential_material(*note)] == []


def test_t10_docs_paragraphs_hit_exactly_the_known_toy_assignments() -> None:
    assert _doc_hits() == _T10_EXPECTED_DOC_HITS


def test_t10_commit_bodies_up_to_the_base_hit_exactly_the_known_toy() -> None:
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{_T10_BASE_COMMIT}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("the S4.4 base commit is not in this clone's history")
    log = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", _T10_BASE_COMMIT, "--format=%B%x00"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    bodies = [body for body in log.split("\x00") if body.strip()]
    assert len(bodies) > 600
    assert sum(1 for body in bodies if carries_credential_material(body)) == _T10_EXPECTED_COMMIT_BODY_HITS


# ---------------------------------------------------------------------------
# Addendum F4: real private keys, every way they are pasted.
# ---------------------------------------------------------------------------


def _pastes(key: str) -> dict[str, tuple[object, ...]]:
    header, _, rest = key.partition("\n")
    escaped = json.dumps(key)[1:-1]
    return {
        "raw": (key,),
        "escaped newlines": (escaped,),
        "crlf": (key.replace("\n", "\r\n"),),
        "escaped crlf": (json.dumps(key.replace("\n", "\r\n"))[1:-1],),
        "one-line env": (f'PRIVATE_KEY="{escaped}"',),
        "service-account json as text": (
            json.dumps({"type": "service_account", "project_id": "demo", "private_key": key}),
        ),
        "header in title, body in text": (header, rest),
        "inside a note": ("Deploy key for the build box:", "Here it is:\n" + key),
        "inside a mapping body": ({"note": "signing key for releases", "key": key},),
    }


@pytest.mark.parametrize("name", PRIVATE_KEY_NAMES)
def test_f4_every_real_private_key_is_caught_however_it_is_pasted(name: str) -> None:
    for label, fields in _pastes(throwaway_key(name)).items():
        assert carries_credential_material(*fields), (name, label)


@pytest.mark.parametrize("name", PGP_PUBLIC_KEY_NAMES)
def test_f4_a_pgp_public_key_block_stays_benign_however_it_is_pasted(name: str) -> None:
    # The PGP private block is refused in every placement above; its public
    # block is published on purpose and must pass in every one of them.
    for label, fields in _pastes(throwaway_key(name)).items():
        assert not carries_credential_material(*fields), (name, label)


def test_f4_a_public_and_private_key_pasted_together_is_caught() -> None:
    both = throwaway_key("openssh_ed25519.pub") + "\n" + throwaway_key("openssh_ed25519")
    assert carries_credential_material(both)


_DASHES = "-" * 5


def _armor(kind: str, body: str | None = None) -> str:
    """A PEM armor line or block built at run time, so no armor line sits in
    this file for a secret scanner to flag (addendum F4 fixture rule)."""

    head = f"{_DASHES}BEGIN {kind} KEY{_DASHES}"
    if body is None:
        return head
    return f"{head}\n{body}\n{_DASHES}END {kind} KEY{_DASHES}"


@pytest.mark.parametrize(
    "text",
    [
        _armor("RSA PRIVATE", "[REDACTED]"),
        _armor("OPENSSH PRIVATE", "..."),
        _armor("PRIVATE", "x" * 24),
        _armor("PRIVATE", "A" * 64),
        _armor("ENCRYPTED PRIVATE", "A" * 62 + "=="),
        "grep for " + _armor("RSA PRIVATE") + " before you push",
        _armor("RSA PRIVATE"),
        _armor("EC PRIVATE"),
        _armor("DSA PRIVATE"),
        "> " + _armor("OPENSSH PRIVATE"),
        "# " + _armor("PRIVATE"),
        f"{_DASHES}BEGIN PGP PRIVATE KEY BLOCK{_DASHES}",
    ],
)
def test_round3_the_dashed_armor_line_is_refused_on_its_own(text: str) -> None:
    """S4.4 round 3 (tower ruling, 2026-09-23): for private key material,
    recall wins over precision. Round 2 read a header only with a key body
    after it, and the adversary review passed real keys behind "> " and
    "# ", double-escaped, as a JSON array of lines and joined with <br>. The
    dashed, case-exact armor line is refused on its own again, as v0.16.0
    did, filler and all."""

    assert carries_credential_material(text)


@pytest.mark.parametrize(
    "text",
    [
        "never paste a begin private key block into chat",
        "Begin by rotating the private key /certificate pair on the load balancer.",
        "Begin with the private key Ubuntu2204 images, then the web tier.",
        "Begin moving the private key Terraform1.9 module into the shared repo.",
        _armor("PUBLIC"),
        _armor("RSA PUBLIC"),
        f"{_DASHES}BEGIN CERTIFICATE{_DASHES}",
        f"{_DASHES}BEGIN PGP PUBLIC KEY BLOCK{_DASHES}",
        f"{_DASHES}BEGIN RSA PRIVATE",
        "begin rsa private key",
    ],
)
def test_round3_prose_public_keys_and_certificates_are_not_refused(text: str) -> None:
    """The case-insensitive dashless header path is gone (round 3, P2 item 6):
    it read "begin ... private key" then a token such as "/certificate",
    "Ubuntu2204" or "Terraform1.9" as a key body."""

    assert not carries_credential_material(text)


# ---------------------------------------------------------------------------
# Addendum F5: the long-prefix formats are read across a seam at every offset.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [ALICE_KEY, HF_TOKEN, SENDGRID, SLACK_HOOK, PYPI],
    ids=["alice_sk_", "hf_", "sendgrid", "slack-webhook", "pypi"],
)
def test_f5_long_prefix_formats_split_at_every_offset_are_caught(token: str) -> None:
    for offset in range(1, len(token)):
        assert carries_credential_material(token[:offset], token[offset:]), offset


# ---------------------------------------------------------------------------
# The expansion cap (ruling R3), and the exported prefix patterns.
# ---------------------------------------------------------------------------


def test_expansion_over_four_times_is_its_own_refusal_and_is_never_truncated() -> None:
    assert credential_verdict("ﷺ" * 100) == VERDICT_EXPANSION
    assert credential_verdict("Peace be upon him ﷺ and his family.") is None
    assert credential_verdict(PAT) == VERDICT_CREDENTIAL
    # Per field: one field over the cap is refused even when the call as a
    # whole is not, because the rest of the call is long ordinary text.
    assert credential_verdict("ﷺ" * 100, "plain notes " * 200) == VERDICT_EXPANSION
    # In total: many short fields each under the floor still add up.
    assert credential_verdict(*[f"ﷺ{index}" for index in range(2000)]) == VERDICT_EXPANSION

    class Refused(ValueError):
        pass

    with pytest.raises(Refused, match="expands more than four times"):
        refuse_credential_material("ﷺ" * 100, error=Refused)
    with pytest.raises(Refused, match="credential material"):
        refuse_credential_material(PAT, error=Refused)


def test_the_exported_prefix_patterns_include_the_agent_key_and_hugging_face() -> None:
    assert any(pattern.search(ALICE_KEY) for pattern in SECRET_PREFIX_PATTERNS)
    assert any(pattern.search(HF_TOKEN) for pattern in SECRET_PREFIX_PATTERNS)
    assert any(pattern.search(AWS_ID) for pattern in SECRET_PREFIX_PATTERNS)
    # An AWS id needs sixteen characters after AKIA or ASIA.
    assert not carries_credential_material(_k("akia", "a" * 12))
    assert carries_credential_material(AWS_ID)


def test_public_key_files_are_real_and_decodable() -> None:
    # Guards the guard: the fixture really holds tool output, not imitations.
    assert all(throwaway_key(name).startswith(("ssh-", "ecdsa-")) for name in PUBLIC_KEY_NAMES)
    assert len(PRIVATE_KEY_NAMES) == 16
    ppk = [name for name in PRIVATE_KEY_NAMES if name.endswith(".ppk")]
    assert ppk == ["putty_ed25519_v3.ppk", "putty_rsa_v2.ppk"]
    assert [throwaway_key(name).partition(":")[0] for name in ppk] == [
        "PuTTY-User-Key-File-3",
        "PuTTY-User-Key-File-2",
    ]
    pgp = [name for name in PRIVATE_KEY_NAMES if name.startswith("pgp_")]
    assert pgp == ["pgp_ed25519.asc", "pgp_rsa.asc"]
    assert all("PGP PRIVATE KEY BLOCK" in throwaway_key(name).partition("\n")[0] for name in pgp)
    assert all("PGP PUBLIC KEY BLOCK" in throwaway_key(name).partition("\n")[0] for name in PGP_PUBLIC_KEY_NAMES)


# ---------------------------------------------------------------------------
# Owner ruling R3 item 4: the import budget, pinned deterministically.
#
# Wall-clock time depends on the machine, so the pin is two counts the check
# reports through credential_floor.scan_cost: surfaces scanned per memory
# record, and characters scanned per input character. The reference
# wall-clock, measured 2026-09-23 on an Apple M3 Max with an export of 10,100
# memories built from the promotion corpus, best of three in one session:
# after round 3, import validation 3.8 s on this branch against 0.75 s on
# v0.16.0, the whole import about 7.0 s against 3.6 s (round 2 measured
# 3.24 s and 5.9 s; the key-file prefix pass costs the difference). The
# round 1 branch measured about 35 times v0.16.0; this is about 5 times. The
# absolute target, set from that measurement: validation under 10 s per
# 10,000 memory records on the reference machine.
# ---------------------------------------------------------------------------

# Re-pinned in round 3 (2026-09-23): the base64 key-file prefix pass reads
# every surface once more (was 36 and 2.25). Measured after round 4, with the
# import value column read by value: 35.0 surfaces per record and 2.86
# characters scanned per input character.
_BUDGET_SURFACES_PER_RECORD = 37.0
_BUDGET_CHARS_PER_INPUT_CHAR = 2.95
# Adversarial shapes, per input character. The worst measured was 8.60 (5.55
# before round 3), on a mapping of 4,000 short non-ASCII pairs: every value
# is scanned raw and normalised, as a pair, and on both seam sequences.
_ADVERSARIAL_CHARS_PER_INPUT_CHAR = 9.0


def _exported_memory_records(tmp_path: Path) -> list[dict[str, object]]:
    from alicebot_api.onramp import bootstrap_database, main as onramp_main
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload

    user_id = "00000000-0000-4000-8000-0000000000dd"
    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=user_id, user_email="budget@alice")
    with sqlite_user_connection(origin, user_id) as conn:
        service = VNextMemoryCommitService(SQLiteVNextStore(conn, user_id))
        for title, text in BUILDER_NOTES:
            service.commit(
                identity=None,
                request=memory_commit_request_from_payload(
                    {"title": title, "canonical_text": text, "source_refs": ["note:budget"], "rationale": "budget"},
                    user_id=user_id,
                ),
            )
    dump = tmp_path / "dump.jsonl"
    assert onramp_main(["export", "--db", str(origin), "--user-id", user_id, "--out", str(dump)]) == 0
    rows = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
    return [row["record"] for row in rows if row["record_type"] == "memory"]


def test_r3_the_import_cost_per_record_is_pinned(tmp_path: Path, capsys) -> None:
    from alicebot_api.onramp import _memory_record_credential_fields

    records = _exported_memory_records(tmp_path)
    capsys.readouterr()
    assert len(records) == len(BUILDER_NOTES)
    surfaces = scanned = inputs = 0
    for record in records:
        fields = [value for _name, value in _memory_record_credential_fields(record)]
        assert credential_floor.credential_verdict(*fields) is None
        record_surfaces, record_scanned, record_inputs = credential_floor.scan_cost(*fields)
        surfaces += record_surfaces
        scanned += record_scanned
        inputs += record_inputs
    assert surfaces / len(records) <= _BUDGET_SURFACES_PER_RECORD
    assert scanned / inputs <= _BUDGET_CHARS_PER_INPUT_CHAR
    # Guards the guard: the count is live, not a constant.
    assert scanned > inputs > 0


_ADVERSARIAL_COST_SHAPES = {
    "underscore run": ("title", "a_" * 25_000),
    "jwt starts": ("title", "eyJ-" * 12_500),
    "base64 tokens": (("QUJD" * 128 + " ") * 97,),
    "spaced run": ("title", "a " * 25_000),
    "literal escapes": ("title", "\\n" * 25_000),
    "short pairs": ({f"k{i}": f"v{i}" for i in range(5_000)},),
    "short non-ascii pairs": ({f"k\u00e9{i}": f"v\u00e9{i}" for i in range(4_000)},),
    "escaped non-ascii": ("\\n\u00e9 a b c d e f g h " * 3_000,),
    "list of mappings": ([{"k\u00e9": f"v{i}", "n": "a b c d e f g h"} for i in range(2_000)],),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL_COST_SHAPES))
def test_r3_the_adversarial_budget_is_per_input_character(name: str) -> None:
    fields = _ADVERSARIAL_COST_SHAPES[name]
    assert credential_floor.credential_verdict(*fields) is None
    _surfaces, scanned, inputs = credential_floor.scan_cost(*fields)
    assert scanned / inputs <= _ADVERSARIAL_CHARS_PER_INPUT_CHAR, (name, scanned / inputs)


@pytest.mark.parametrize(
    ("title", "text"),
    [
        (f"{_DASHES}BEGIN RSA PRIVATE", f"KEY{_DASHES}"),
        (f"{_DASHES}BEGIN OPENSSH ", f"PRIVATE KEY{_DASHES}"),
        (f"{_DASHES}BEGIN PGP PRIVATE KEY ", f"BLOCK{_DASHES}"),
        # Round 4: split right after BEGIN, where the door strips the space.
        (f"{_DASHES}BEGIN", f"RSA PRIVATE KEY{_DASHES}"),
        (f"{_DASHES}BEGIN", f"PGP PRIVATE KEY BLOCK{_DASHES}"),
    ],
)
def test_round3_an_armor_line_split_across_two_fields_is_refused(title: str, text: str) -> None:
    # Guards the guard: neither half is an armor line on its own.
    assert not carries_credential_material(title)
    assert not carries_credential_material(text)
    assert carries_credential_material(title, text)


@pytest.mark.parametrize("name", [name for name in PRIVATE_KEY_NAMES if name.startswith(("openssh_", "pgp_"))])
def test_round3_a_key_whose_armor_dashes_were_lost_is_refused_by_its_body(name: str) -> None:
    """The header-and-body rule, on its own. With the dashes gone (a Markdown
    renderer, a copy from a rendered page) no armor line is left, so this is
    the rule that refuses the key, raw and with escaped newlines. Pins the
    round 2 fixes the armor line otherwise masks: an OpenSSH body's runs of
    "A" are not a placeholder, literal \\n escapes are decoded, and a PGP
    secret key's header ends in "PRIVATE KEY BLOCK"."""

    dashless = throwaway_key(name).replace(_DASHES, "")
    assert carries_credential_material(dashless)
    assert carries_credential_material(json.dumps(dashless)[1:-1])


@pytest.mark.parametrize("text", [_k("dbPasswordV2: ", "Kd9xoYWu83nq"), _k("apiTokenOld=", "Kd9xoYWu83nqPlzRt5vW")])
def test_round3_a_camel_case_copy_suffix_does_not_hide_the_name(text: str) -> None:
    # The neutral suffix inside a camelCase name is stripped after splitting.
    assert carries_credential_material(text)


@pytest.mark.parametrize(
    "text",
    [
        "Tokens for the runner:\\n" + "ghp_" + "Qm7pX2rT9vK4nW8sL3zB6cD1fG5hJ0kLa9M2",
        "keys:\\r\\n" + "AKIA" + "Z7Q3XK2M9P4WN8VB",
    ],
    ids=["github token", "aws id"],
)
def test_round4_a_token_at_the_start_of_an_escaped_line_is_read(text: str) -> None:
    """A one-line JSON or .env value puts a literal backslash-n right before
    the token, and the "n" reads as part of the word, hiding the token's
    left edge. The escapes are decoded first, so the token starts a line."""

    assert carries_credential_material(text)
