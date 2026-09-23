"""Harness v4: v0.16.0's check at the two doors it ran, re-implemented and compared (S4.4 round 5).

The defect (2026-09-23). A third adversary on 6c24a97 found one P1 and seven
P2s, all of one kind: v0.16.0 refused them at the commit door and the branch
stored them. Dotted values under secret names (ya29., Discord, Mapbox sk.,
DB_PASSWORD=Winter.Is.Coming2024), Telegram bot tokens, xxd hex dumps of key
files, unarmored DER under PRIVATE_KEY_DATA and its siblings, numeric prose
passwords, primary and routing keys, hex AES keys, and Django SECRET_KEY
values with symbols. The tower confirmed the class is structural: v0.16.0
also refused PASSWORD_DB=, TOKEN_GITHUB=, API_KEY_RAW=, GITHUB_TOKEN_WRITE=
and VAULT_TOKEN=hvs., and the branch stored all five.

How it escaped. Rounds 2 to 4 replaced v0.16.0's check at the commit gate and
the promotion floor with the new detector, and compared the two only on
hand-picked shapes and frozen harnesses of known credentials. No test ran
v0.16.0's own code on the inputs it refuses, so a whole class of names (a
secret word anywhere but the last segment) moved from refused to stored
without a failing test.

The ruling (H1) and what this file holds, as harness v4:
(a) Equivalence. alicebot_api.legacy_credential_check re-implements
    v0.16.0's check in linear time. It is compared with v0.16.0's own code,
    kept verbatim in legacy_v0160_credential_oracle.py, on bounded generated
    inputs at both doors: the same verdict on every input.
(b) Carve-out safety. Each of the four carve-outs is tested both ways, the
    password rule is tested, and no carve-out excuses anything in the real
    key fixtures or in a generated grammar of published token shapes under
    secret names.
(c) Benign measurement. The repo's doc sentences, the promotion notes and
    the named adversary notes go through the commit door (at the store) and
    the import check; the refusals are counted against v0.16.0 and pinned.
Plus store-level tests for every round 5 shape and linear-time tests.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import string
import time
from uuid import UUID, uuid4

import pytest

from alicebot_api import legacy_credential_check as legacy
from alicebot_api import vnext_promotion_policy as policy
from alicebot_api.credential_floor import carries_credential_material, normalize_for_matching
from alicebot_api.onramp import _memory_record_credential_finding
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from alicebot_api.vnext_promotion_policy import PromotionCandidate, PromotionSettings, evaluate_promotion, hard_floor_hits

from tests.unit import legacy_v0160_credential_oracle as oracle
from tests.unit.fixtures_benign_corpus import CREDENTIAL_VOCABULARY_SENTENCES, DOC_SENTENCES
from tests.unit.fixtures_promotion_corpus import ALL_NOTES
from tests.unit.fixtures_throwaway_keys import PRIVATE_KEY_NAMES, PUBLIC_KEY_NAMES, throwaway_key
from tests.unit.test_credential_floor_design import _T3_FALSE


REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHES = "-" * 5
_BASE62 = string.ascii_letters + string.digits
# Twenty random-looking characters, built at run time so no scanner-shaped
# token sits in the source.
R = "".join(("Xq9mZt2L", "xP9wKc4B", "Vq7m"))
# Test ids must not change between runs.
FIXED_UUID = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"


def _store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "harness-v4@example.com")
    return SQLiteVNextStore(conn, user_id)


def _commit(store: SQLiteVNextStore, text: str, *, title: str = "Deploy note", source_refs: list[object] | None = None):
    payload: dict[str, object] = {"title": title, "canonical_text": text}
    if source_refs is not None:
        payload["source_refs"] = source_refs
    return VNextMemoryCommitService(store).commit(
        identity=None, request=memory_commit_request_from_payload(payload, user_id=store.user_id)
    )


def _rows(store: SQLiteVNextStore, table: str) -> str:
    return json.dumps([list(row) for row in store.conn.execute(f"SELECT * FROM {table}")], default=str)


def _nowhere(store: SQLiteVNextStore, secret: str) -> bool:
    """The secret is in no memory, no revision and no event."""

    return all(secret not in _rows(store, table) for table in ("memories", "memory_revisions", "event_log"))


def _memory_count(store: SQLiteVNextStore) -> int:
    return int(next(iter(store.conn.execute("SELECT count(*) FROM memories")))[0])


# ---------------------------------------------------------------------------
# The oracle is v0.16.0, and the re-implementation uses v0.16.0's patterns.
# ---------------------------------------------------------------------------

# SHA-256 of legacy_v0160_credential_oracle.py as generated from 880915a.
# Re-mint only with a dated note saying why the oracle had to change.
_ORACLE_SHA256 = "55c0b72ed751c64ad27e15f60ae33c1315583a9fc89af9acabb9262fdaedd7ba"


def test_harness_v4_the_oracle_is_unchanged() -> None:
    path = REPO_ROOT / "tests/unit/legacy_v0160_credential_oracle.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == _ORACLE_SHA256


def _patterns(compiled: tuple[object, ...] | list[object]) -> list[tuple[str, int]]:
    return [(pattern.pattern, pattern.flags) for pattern in compiled]  # type: ignore[attr-defined]


def test_harness_v4_the_reimplementation_reads_v0160s_own_patterns() -> None:
    assert legacy._CREDENTIAL_PHRASES == oracle._CREDENTIAL_PHRASES
    assert legacy._CREDENTIAL_TOKEN_MARKERS == oracle._CREDENTIAL_TOKEN_MARKERS
    assert _patterns(legacy._CREDENTIAL_TOKEN_PATTERNS) == _patterns(oracle._CREDENTIAL_TOKEN_PATTERNS)
    searched = [pattern for _rule, pattern in legacy._SEARCHED_PATTERNS]
    in_v0160_order = [*searched[:4], legacy._JWT_PATTERN, legacy._SSH_PUBLIC_KEY, *searched[4:]]
    assert [getattr(p, "pattern", p) for p in in_v0160_order] == [p.pattern for p in oracle._CREDENTIAL_PATTERNS]
    assert [p.flags for p in (*searched[:4], legacy._SSH_PUBLIC_KEY, *searched[4:])] == [
        p.flags for index, p in enumerate(oracle._CREDENTIAL_PATTERNS) if index != 4
    ]
    for name in ("_BASE64_TOKEN", "_HEX_TOKEN", "_SPACED_OUT_RUN"):
        assert _patterns([getattr(legacy, name)]) == _patterns([getattr(oracle, name)]), name
    assert legacy._RUN_SEPARATORS == oracle._RUN_SEPARATORS
    assert _patterns([legacy._PREFIX_SK, *legacy._PREFIX_PATTERNS]) == _patterns(oracle.SECRET_PREFIX_PATTERNS)
    # The NAME=value rule's parts are the old expression's parts.
    assert legacy._SECRET_NAME_EMBEDDABLE in oracle.SECRET_ASSIGNMENT_PATTERN.pattern
    # And the promotion policy re-exports the one copy of it.
    assert policy.secret_assignment_values is legacy.secret_assignment_values
    assert policy.looks_like_secret_value is legacy.looks_like_secret_value


def test_harness_v4_the_shared_normaliser_is_v0160s() -> None:
    sample = (
        "".join(source for source, _target in oracle._CONFUSABLE_PAIRS)
        + "\uff53\uff4b\uff0d\u200b\u200d\u00ad\u0301e\u0301\ufb01\u2126\u212a\u017f\u00df\u0130\u2460\U0001d42c"
    )
    for index in range(0, len(sample), 7):
        chunk = sample[index : index + 9]
        assert normalize_for_matching(chunk) == oracle.normalize_for_matching(chunk), repr(chunk)


# ---------------------------------------------------------------------------
# (a) Equivalence on bounded generated inputs.
# ---------------------------------------------------------------------------

_SECRET_WORDS = ("password", "passwd", "secret", "token", "credential", "credentials", "apikey", "key")
_OTHER_WORDS = (
    "db", "github", "raw", "write", "bot", "data", "vault", "api", "auth", "access", "private", "prod",
    "cache", "sort", "fact", "next", "page", "user", "id", "x", "v2", "monkey", "keys", "service", "aes",
    "primary", "routing", "content", "pem", "der", "material", "dedupe", "memory", "idempotency",
)
_OPERATORS = ("=", ":", ": ", " = ", '="', ": '", '": "', " := ", "=\n")
_CONTEXTS = ("{name}{op}{value}", "export {name}={value}", "{name}: {value}", '{{"{name}": "{value}"}}',
             "[prod]\n{name} = {value}", "the {name} is {value} for now", "{name}='{value}' # rotate")
_PROSE_KEYWORDS = ("password", "passphrase", "api key", "access key", "secret key", "private key", "auth token",
                   "access token", "credential", "API  Key", "Password")
_MARKERS = ("ghp_", "gho_", "github_pat_", "glpat-", "npm_", "sk-", "ssh-rsa ", "ssh-ed25519 ", "xoxb-", "xoxp-",
            "AKIA", "ASIA", "akia", "AIza", "eyJ", "Bearer ", "-----BEGIN RSA PRIVATE KEY-----",
            "-----begin ec private key-----", "begin private key", "sk-learn", "sk-ssh-ed25519@openssh.com")
_PLACEHOLDERS = ("changeme", "${DB_PASSWORD}", "<redacted>", "xxxxxxxx", "hunter2", "your_token_here", "none")
_CODE_ROOTS = ("settings", "process.env", "os.environ", "config", "self", "secrets", "ENV", "var", "app", "Settings")
_UNICODE_TWISTS = ("\u200b", "\uff53", "\u0455", "\u212a", "\u017f", "\u00e9", "\u0301", "\u00a0")
_SOUP = ("_", "-", "=", ":", " ", "\n", "\t", "'", '"', ".", "/", "+", ",", "|", ";", "\u00b7", "is", "was",
         "abc123", "Hunter22", "abcdef", "Winter.Is.Coming2024", "12345678", "a . b . c", "0123456789abcdef")


def _rand(rng: random.Random, size: int, alphabet: str = _BASE62) -> str:
    return "".join(rng.choice(alphabet) for _ in range(size))


def _name(rng: random.Random) -> str:
    segments = [rng.choice(_SECRET_WORDS + _OTHER_WORDS) for _ in range(rng.randint(1, 4))]
    style = rng.randrange(4)
    if style == 0:
        return "_".join(segments).upper()
    if style == 1:
        return "_".join(segments)
    if style == 2:
        return "-".join(segments)
    return segments[0] + "".join(segment.title() for segment in segments[1:])


def _value(rng: random.Random) -> str:
    kind = rng.randrange(10)
    if kind == 0:
        return _rand(rng, rng.randint(4, 32))
    if kind == 1:
        return rng.choice("_-.").join(rng.choice(("release", "mode", "user", "v2", "created", "at", "desc"))
                                      for _ in range(rng.randint(1, 3)))
    if kind == 2:
        return _rand(rng, rng.randint(3, 12), string.digits)
    if kind == 3:
        return rng.choice(_CODE_ROOTS) + "." + rng.choice(("OPENAI_API_KEY", "db_password", "get", "X"))
    if kind == 4:
        return _rand(rng, rng.randint(8, 64), "0123456789abcdef")
    if kind == 5:
        return rng.choice(_MARKERS) + _rand(rng, rng.randint(0, 36))
    if kind == 6:
        return rng.choice(_PLACEHOLDERS)
    if kind == 7:
        return rng.choice(("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "sk-learn", "sk-learn-bench-2024"))
    if kind == 8:
        return _rand(rng, rng.randint(6, 20), _BASE62 + "$!#@%^&*.")
    seeded = str(UUID(int=rng.getrandbits(128), version=4))
    return seeded if rng.random() < 0.5 else "agentic_memory.semantic." + seeded


def _assignment(rng: random.Random) -> str:
    context = rng.choice(_CONTEXTS)
    return context.format(name=_name(rng), op=rng.choice(_OPERATORS), value=_value(rng))


def _prose(rng: random.Random) -> str:
    filler = rng.choice(("", " for the vault", " we use", " on prod", " (rotated)"))
    verb = rng.choice(("is", "was", "are", "were", "is now", "IS"))
    return f"{rng.choice(('The', 'my', 'Our', ''))} {rng.choice(_PROSE_KEYWORDS)}{filler} {verb} {_value(rng)}"


def _jwt_like(rng: random.Random) -> str:
    """Three dot-separated runs after eyJ, some too short to be a JWT."""

    parts = [_rand(rng, rng.randint(2, 14), _BASE62 + "-_") for _ in range(3)]
    text = rng.choice(("", "-", "x ", "token=")) + "eyJ" + ".".join(parts)
    if rng.random() < 0.5:
        # Round 6: a short eyJ run before a well-formed one, so a scan that
        # gives up after its first failed start is seen.
        short = "eyJ" + _rand(rng, rng.randint(1, 7)) + "." + _rand(rng, rng.randint(1, 4))
        text = short + rng.choice((" ", ", ", "\n")) + text
    return text


def _marker(rng: random.Random) -> str:
    if rng.random() < 0.2:
        return _jwt_like(rng)
    return rng.choice(("", "use ", "token ", "x")) + rng.choice(_MARKERS) + _rand(rng, rng.randint(0, 40))


def _twisted(rng: random.Random, text: str) -> str:
    if not text:
        return text
    index = rng.randrange(len(text))
    return text[:index] + rng.choice(_UNICODE_TWISTS) + text[index + rng.randint(0, 1) :]


def _encoded(rng: random.Random, text: str) -> str:
    raw = text.encode("utf-8")
    kind = rng.randrange(4)
    if kind == 0:
        return base64.b64encode(raw).decode()
    if kind == 1:
        return base64.urlsafe_b64encode(raw).decode()
    if kind == 2:
        return raw.hex()
    return " ".join(text)


def _soup(rng: random.Random) -> str:
    return "".join(rng.choice(_SOUP + _SECRET_WORDS + _MARKERS) for _ in range(rng.randint(1, 10)))


def _sample(rng: random.Random) -> str:
    kind = rng.randrange(7)
    if kind == 0:
        text = _assignment(rng)
    elif kind == 1:
        text = _prose(rng)
    elif kind == 2:
        text = _marker(rng)
    elif kind == 3:
        text = _soup(rng)
    elif kind == 4:
        text = _encoded(rng, rng.choice((_assignment, _prose, _marker))(rng))
    elif kind == 5:
        text = _twisted(rng, rng.choice((_assignment, _prose, _marker))(rng))
    else:
        text = rng.choice(("We deploy on Thursdays.", "Prefer ssh-ed25519 keys over ssh-rsa keys",
                           "We use sk-learn for the churn model.", "Ring Bearer duties", "Meeting with Akia"))
    return text[:160]


def _fields(rng: random.Random) -> tuple[str, str, str | None, str | None, list[object]]:
    text = _sample(rng)
    title = rng.choice(("Note", "", _sample(rng)))
    if rng.random() < 0.15 and len(text) > 4:
        # A split across title and body, which only the floor's joins read.
        cut = rng.randrange(1, len(text))
        title, text = text[:cut], text[cut:]
    excerpt = _sample(rng) if rng.random() < 0.15 else None
    rationale = _sample(rng) if rng.random() < 0.15 else None
    refs: list[object] = []
    if rng.random() < 0.25:
        refs.append(_sample(rng))
    if rng.random() < 0.25:
        refs.append({_name(rng): _value(rng), "note": _sample(rng)})
    if rng.random() < 0.3:
        refs.append(_structured_ref(rng))
    return title, text, excerpt, rationale, refs


_QUIET = ("We deploy on Thursdays.", "note", "Ring Bearer duties", "see the runbook")


def _structured_ref(rng: random.Random) -> object:
    """Round 6: the shapes the two doors flatten differently. A mapping key
    with a list or mapping value (the commit gate reads the key alone, the
    floor never reads it), mappings nested in mappings, and lists of them."""

    probe = rng.choice((_marker, _assignment, _value))(rng)
    kind = rng.randrange(5)
    if kind == 0:
        return {probe: [rng.choice(_QUIET)]}
    if kind == 1:
        return {probe: {"k": rng.choice(_QUIET)}}
    if kind == 2:
        return {rng.choice(("a", "meta", _name(rng))): {rng.choice(("b", _name(rng))): probe}}
    if kind == 3:
        return {"outer": {"inner": {_name(rng): probe}}}
    return [{"a": [probe]}, {"b": {"c": [rng.choice(_QUIET)]}}]


def _quiet_fields(rng: random.Random) -> tuple[str, str, str | None, str | None, list[object]]:
    """A clean note with one probe in a structure, so the probe decides the verdict."""

    return ("Note", rng.choice(_QUIET), None, None, [_structured_ref(rng)])


def _equivalence_inputs(seed: int, count: int) -> list[tuple[str, str, str | None, str | None, list[object]]]:
    rng = random.Random(seed)
    return [_quiet_fields(rng) if rng.random() < 0.2 else _fields(rng) for _ in range(count)]


@pytest.mark.parametrize("seed", [20260923, 5, 17])
def test_harness_v4_same_verdict_as_v0160_at_both_doors(seed: int) -> None:
    commit_refused = floor_refused = 0
    inputs = _equivalence_inputs(seed, 1500)
    for title, text, excerpt, rationale, refs in inputs:
        expected = oracle.commit_gate_refuses(title, text, excerpt, rationale, refs)
        assert legacy.v0160_commit_gate_refuses(title, text, excerpt, rationale, refs) is expected, (
            title, text, excerpt, rationale, refs,
        )
        expected_floor = oracle.promotion_floor_refuses(title, text, excerpt, refs)
        assert legacy.v0160_promotion_floor_refuses(title, text, excerpt, refs) is expected_floor, (
            title, text, excerpt, refs,
        )
        commit_refused += expected
        floor_refused += expected_floor
    # Guards the guard: both verdicts occur often, so agreement is not
    # agreement on one answer.
    assert 0.3 * len(inputs) < commit_refused < 0.9 * len(inputs), commit_refused
    assert 0.3 * len(inputs) < floor_refused < 0.9 * len(inputs), floor_refused


# Round 6: the three shapes the round 5 generator never made, found by the
# confirmatory adversary as mutants the whole suite let through.
_GHP_36 = "gh" + "p_" + (R + R)[:36]


def test_round6_a_short_eyj_run_before_a_jwt_is_read_as_v0160_read_it() -> None:
    text = "eyJab.x eyJaaaaaaaaa.bbbbbbbbb.cccc"
    assert oracle.commit_gate_refuses("Note", text) and oracle.promotion_floor_refuses("Note", text)
    assert legacy.v0160_commit_gate_refuses("Note", text)
    assert legacy.v0160_promotion_floor_refuses("Note", text)


def test_round6_a_mapping_key_with_a_list_value_is_read_at_commit_only() -> None:
    refs = [{_GHP_36: ["x"]}]
    assert oracle.commit_gate_refuses("Note", "see refs", None, None, refs)
    assert legacy.v0160_commit_gate_refuses("Note", "see refs", None, None, refs)
    # v0.16.0's floor never read a key whose value is a list; nor does this.
    assert not oracle.promotion_floor_refuses("Note", "see refs", None, refs)
    assert not legacy.v0160_promotion_floor_refuses("Note", "see refs", None, refs)


def test_round6_a_nested_mapping_in_source_refs_is_read_at_the_floor() -> None:
    refs = [{"a": {"b": _GHP_36}}]
    assert oracle.promotion_floor_refuses("Note", "see refs", None, refs)
    assert legacy.v0160_promotion_floor_refuses("Note", "see refs", None, refs)
    assert legacy.v0160_commit_gate_refuses("Note", "see refs", None, None, refs)


def test_round6_the_generator_makes_the_three_shapes() -> None:
    """Guards the guard: the equivalence inputs hold each round 6 shape."""

    inputs = _equivalence_inputs(20260923, 1500)
    refs = [ref for *_fields, row_refs in inputs for ref in row_refs]
    texts = [text for _title, text, *_rest in inputs]
    assert any(isinstance(ref, dict) and any(isinstance(v, list) for v in ref.values()) for ref in refs)
    assert any(isinstance(ref, dict) and any(isinstance(v, dict) for v in ref.values()) for ref in refs)
    assert sum(1 for text in texts if text.count("eyJ") >= 2) > 5


# Reversed word order and trailing segments: the class the tower confirmed.
_NAME_ORDER_CASES = (
    "PASSWORD_DB", "TOKEN_GITHUB", "API_KEY_RAW", "GITHUB_TOKEN_WRITE", "VAULT_TOKEN", "SLACK_TOKEN_BOT",
    "KEY_DATA", "SECRET_GITHUB_DEPLOY", "PRIVATE_KEY_MATERIAL", "apikey-raw", "token-github-write",
)


@pytest.mark.parametrize("name", _NAME_ORDER_CASES)
def test_harness_v4_reversed_and_suffixed_names_are_refused_at_commit_as_v0160_did(name: str) -> None:
    text = f"{name}={R}"
    assert oracle.commit_gate_refuses("Note", text) is True
    assert legacy.v0160_commit_gate_refuses("Note", text) is True
    assert legacy.commit_gate_refuses("Note", text) is True
    store = _store()
    result = _commit(store, text, title="Note")
    assert result["status"] == "rejected"
    assert "unsafe_secret_storage" in result["reasons"]
    assert _nowhere(store, R)


# ---------------------------------------------------------------------------
# Store-level: every round 5 shape is refused at the commit door.
# ---------------------------------------------------------------------------


def _xxd_plain(text: str) -> str:
    hexed = text.encode().hex()
    return "\n".join(hexed[index : index + 60] for index in range(0, len(hexed), 60))


def _der_base64() -> str:
    return "".join(throwaway_key("pkcs8_rsa.pem").splitlines()[1:-1])[:200]


ROUND5_SHAPES = {
    "google oauth under a secret name": lambda: "GOOGLE_OAUTH_TOKEN=" + "ya" + "29.a0AfH6SMBx" + R + "Qe7",
    "discord token": lambda: "DISCORD_TOKEN=" + "MTE4NzY1" + "NDMyMTA5ODc2NTQzMg.GhI2Jk." + R + "abcdEF",
    "mapbox secret": lambda: "MAPBOX_TOKEN=" + "sk" + ".eyJ1IjoiZXhhbXBsZSJ9." + R,
    "dotted password": lambda: "DB_PASSWORD=" + "Winter.Is.Coming2024",
    "telegram bot token": lambda: "TELEGRAM_BOT_TOKEN=1234567890:AAH" + R + "Kq3LmN8pQrS",
    "xxd plain hex of a key file": lambda: _xxd_plain(throwaway_key("openssh_ed25519")),
    "DER under _DATA": lambda: "PRIVATE_KEY_DATA=" + _der_base64(),
    "DER under _CONTENT": lambda: "SIGNING_KEY_CONTENT=" + _der_base64(),
    "DER under _PEM": lambda: "PRIVATE_KEY_PEM=" + _der_base64(),
    "DER under _DER": lambda: "PRIVATE_KEY_DER=" + _der_base64(),
    "DER under _MATERIAL": lambda: "PRIVATE_KEY_MATERIAL=" + _der_base64(),
    "numeric prose password": lambda: "The wifi password is 84713920 for the guest network.",
    "primary key": lambda: "PRIMARY_KEY=" + R + "Ab3Cd4",
    "routing key": lambda: "routing_key: " + R.lower() + "9f2",
    "hex aes key": lambda: "aes_key=" + "0f1e2d3c4b5a6978" * 4,
    "django secret key": lambda: "SECRET_KEY = 'django-insecure-9a$k#2!x@w7^m&q*z(3)l_b8+r-t0e1y5u4i6o'",
    "vault token": lambda: "VAULT_TOKEN=" + "hv" + "s.CAESI" + R + "Gh4KHGh2cy5",
}


@pytest.mark.parametrize("shape", sorted(ROUND5_SHAPES))
def test_round5_every_adversary_shape_is_refused_at_the_commit_door(shape: str) -> None:
    text = ROUND5_SHAPES[shape]()
    # The shape is one v0.16.0 refused, by its own code.
    assert oracle.commit_gate_refuses("Deploy note", " ".join(text.split()))
    store = _store()
    result = _commit(store, text)
    assert result["status"] == "rejected", result.get("reasons")
    assert _memory_count(store) == 0
    assert _nowhere(store, text[-12:])


def test_round5_the_import_door_keeps_the_detector_alone() -> None:
    """Every other door keeps the new detector alone (ruling H1): the name
    order class passes import, as the residual docs say. Pinned so a change
    is deliberate."""

    record = {"id": "1", "title": "Note", "canonical_text": f"TOKEN_GITHUB={R}", "summary": f"TOKEN_GITHUB={R}"}
    assert _memory_record_credential_finding(record, line_no=1) is None
    # Guards the guard: the same row is refused at commit.
    assert _commit(_store(), f"TOKEN_GITHUB={R}")["status"] == "rejected"


# ---------------------------------------------------------------------------
# The promotion floor: v0.16.0's floor rule, at the store.
# ---------------------------------------------------------------------------

AUTHENTICATED_PERSONAL = {
    "settings": PromotionSettings(persona="personal"),
    "permission_profile": "trusted_local_agent",
    "writer_trust": "authenticated_agent",
}


def _candidate(text: str, **extra: object) -> PromotionCandidate:
    return PromotionCandidate(
        title="deploy", canonical_text=text, domain="professional", sensitivity="internal",
        source_type="trusted_agent", **extra,  # type: ignore[arg-type]
    )


def test_round5_the_floor_holds_back_what_v0160s_floor_held_back() -> None:
    candidate = _candidate(f"TOKEN_GITHUB={R}")
    # The detector alone lets it through; v0.16.0's floor rule does not.
    assert not carries_credential_material(candidate.title, candidate.canonical_text)
    assert "credential_material" in hard_floor_hits(candidate)
    assert evaluate_promotion(candidate=candidate, **AUTHENTICATED_PERSONAL).auto_promote is False  # type: ignore[arg-type]
    # Guards the guard: the clean twin promotes.
    assert evaluate_promotion(candidate=_candidate("Deploys go out on Tuesdays."), **AUTHENTICATED_PERSONAL).auto_promote  # type: ignore[arg-type]


def test_round5_the_floor_never_gets_the_commit_gates_prefix_patterns() -> None:
    # "akia" and twelve letters is a commit-gate prefix pattern only.
    text = "the akiaabcdefghijklm project"
    assert oracle.commit_gate_refuses("Note", text) and not oracle.promotion_floor_refuses("Note", text)
    assert "credential_material" not in hard_floor_hits(_candidate(text))


def test_round5_a_proposal_the_floor_holds_back_stays_a_review_candidate_at_the_store(monkeypatch) -> None:
    from alicebot_api import mcp_tools as mcp_tools_module
    from alicebot_api.mcp import memories as mcp_memories
    from alicebot_api.mcp import policy as mcp_policy
    from alicebot_api.mcp.types import MCPRuntimeContext

    from tests.unit.test_vnext_promotion_policy import _charter, _CharterStore, _live_sqlite_store_with_key

    def propose(text: str) -> dict[str, object]:
        sqlite_store, user_id, raw_key = _live_sqlite_store_with_key()
        store = _CharterStore(sqlite_store, _charter("personal"))

        @contextmanager
        def fake_store_context(_context: object):
            yield store

        for module in (mcp_memories, mcp_tools_module, mcp_policy):
            if hasattr(module, "_vnext_store_context"):
                monkeypatch.setattr(module, "_vnext_store_context", fake_store_context)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", raw_key or "")
        payload = mcp_memories._handle_alice_vnext_propose_memory(
            MCPRuntimeContext(database_url="postgresql://localhost/alicebot", user_id=user_id),  # type: ignore[arg-type]
            {"agent_id": "hermes", "canonical_text": text, "title": "Deploy cadence",
             "domain": "professional", "sensitivity": "internal"},
        )
        statuses = [row[0] for row in sqlite_store.conn.execute("SELECT status FROM memories")]
        return {"payload": payload, "statuses": statuses}

    held = propose(f"TOKEN_GITHUB={R}")
    assert held["payload"]["policy_decision"]["promotion"]["tier"] == "hard_floor"  # type: ignore[index]
    assert held["statuses"] == ["candidate"]
    # Guards the guard: the same agent and persona land a clean note active.
    assert propose("The team deploys on Thursdays.")["statuses"] == ["active"]


# ---------------------------------------------------------------------------
# (b) The carve-outs, each both ways, and the password rule.
# ---------------------------------------------------------------------------


def _findings(text: str, *, commit: bool = True) -> list[legacy.Finding]:
    if commit:
        return list(legacy._commit_findings("Note", text, None, None, ()))
    return list(legacy._floor_findings_for("Note", text, None, ()))


def _excused_by(carve_out, text: str) -> bool:  # type: ignore[no-untyped-def]
    """Every finding in the text is excused, and this carve-out excuses at least one."""

    findings = _findings(text)
    assert findings, text
    return all(legacy.excused(finding) for finding in findings) and any(carve_out(f) for f in findings)


_SSH_EXCUSED = (
    "Prefer ssh-ed25519 keys over ssh-rsa keys",
    "Deploy key: ssh-ed25519",
    "FIDO keys show as sk-ssh-ed25519@openssh.com in authorized_keys",
    "key: ecdsa-sha2-nistp256",
)
_SSH_NOT_EXCUSED = (
    "password: ssh-ed25519",
    # The header of a PuTTY private key file names the key that follows.
    "PuTTY-User-Key-File-2: ssh-rsa",
    "Deploy key: ssh-ed25519x9",
    "sk-ssh-ed25519@openssh.co",
)


@pytest.mark.parametrize("text", _SSH_EXCUSED)
def test_carve_out_a_excuses_ssh_public_keys_and_key_type_names(text: str) -> None:
    assert _excused_by(legacy.carve_out_ssh_public_key, text)
    assert not legacy.commit_gate_refuses("Note", text)


@pytest.mark.parametrize("text", _SSH_NOT_EXCUSED)
def test_carve_out_a_excuses_nothing_else(text: str) -> None:
    assert legacy.v0160_commit_gate_refuses("Note", text)
    assert legacy.commit_gate_refuses("Note", text)


@pytest.mark.parametrize("name", PUBLIC_KEY_NAMES)
def test_carve_out_a_lets_every_public_key_fixture_commit_at_the_store(name: str) -> None:
    key = throwaway_key(name)
    store = _store()
    assert _commit(store, f"Deploy key for the build host: {key}")["status"] != "rejected"
    assert _memory_count(store) == 1


_SK_EXCUSED = ("We use sk-learn for the churn model baseline.", "the sk-learn-bench-2024 run", "pip install sk-image")
_SK_NOT_EXCUSED = (
    "SK-LEARN is the old name",
    "Work is on branch SK-1482-fix-retry-backoff until review.",
    "sk-learn_v2 and friends",
    "key " + "sk" + "-proj-" + R + R,
    "sk-abcdefghijklmnopq",
)


@pytest.mark.parametrize("text", _SK_EXCUSED)
def test_carve_out_b_excuses_sk_and_lower_case_words(text: str) -> None:
    assert _excused_by(legacy.carve_out_sk_word_chain, text)
    assert not legacy.commit_gate_refuses("Note", text)


@pytest.mark.parametrize("text", _SK_NOT_EXCUSED)
def test_carve_out_b_excuses_nothing_else(text: str) -> None:
    assert legacy.v0160_commit_gate_refuses("Note", text)
    assert legacy.commit_gate_refuses("Note", text)


_STRUCTURAL_EXCUSED = (
    "fact_key=release_mode",
    "SORT_KEY=created_at-desc",
    "CACHE_KEY=user.v2",
    "partition_key: tenant-42",
    "memory_key=agentic_memory.semantic." + FIXED_UUID,
    "openclaw_dedupe_key=" + "9f86d081884c7d65" * 4,
    f"Send Idempotency-Key: {FIXED_UUID} with each POST.",
    '{"next_page_token": "' + 'CiAKGjBpNDd2"}',
    "nextPageToken=" + "CiAKGjBpNDd2",
    "subject_key=project.alice.release",
)
_STRUCTURAL_NOT_EXCUSED = (
    "session_key=" + R,
    "cache_key=" + R,
    "api_cache_key_secret=release_mode",
    "PRIMARY_KEY=" + R,
    "routing_key=" + R.lower(),
    "password_cache_key=abc_def",
    "sort_key=" + "sk" + "-proj-" + R,
    # A cursor is at most 128 characters.
    "next_page_token=" + R * 7,
)


@pytest.mark.parametrize("text", _STRUCTURAL_EXCUSED)
def test_carve_out_c_excuses_structural_names_with_identifier_values(text: str) -> None:
    assert _excused_by(legacy.carve_out_structural_key_name, text)
    assert not legacy.commit_gate_refuses("Note", text)


@pytest.mark.parametrize("text", _STRUCTURAL_NOT_EXCUSED)
def test_carve_out_c_excuses_nothing_else(text: str) -> None:
    assert legacy.v0160_commit_gate_refuses("Note", text)
    assert legacy.commit_gate_refuses("Note", text)


def test_carve_out_c_reads_a_name_in_its_own_field_on_the_floors_glued_surface() -> None:
    # "Release" + "fact_key=..." reads as the name "Releasefact_key" once the
    # floor joins its fields with nothing.
    assert not legacy.promotion_floor_refuses("Release", "fact_key=release_mode")
    assert legacy.v0160_promotion_floor_refuses("Release", "fact_key=release_mode")
    # Guards the guard: a glued password word still blocks every carve-out.
    assert legacy.promotion_floor_refuses("DB password", "cache_key=abc_def")


_CODE_EXCUSED = (
    "api_key = settings.OPENAI_API_KEY",
    "api_key=os.environ['OPENAI_API_KEY']",
    "token: secrets.GITHUB_TOKEN",
    "API_KEY=process.env.OPENAI_API_KEY",
    "secret = config.get('secret')",
    "self.token = self.api_token",
    "key = ENV.fetch('KEY')",
)
_CODE_NOT_EXCUSED = (
    "password: process.env.DB_PASSWORD",
    "DB_PASSWORD=settings.db",
    "api_key = var.api_key",
    "api_key = app.config2",
    "api_key = Settings.OPENAI_API_KEY",
    "api_key = settings." + R + "+/",
)


@pytest.mark.parametrize("text", _CODE_EXCUSED)
def test_carve_out_d_excuses_dotted_references_to_known_code_objects(text: str) -> None:
    assert _excused_by(legacy.carve_out_code_reference, text)
    assert not legacy.commit_gate_refuses("Note", text)


@pytest.mark.parametrize("text", _CODE_NOT_EXCUSED)
def test_carve_out_d_excuses_nothing_else(text: str) -> None:
    assert legacy.v0160_commit_gate_refuses("Note", text)
    assert legacy.commit_gate_refuses("Note", text)


@pytest.mark.parametrize(
    "text",
    [
        "db_password = settings.DB_PASSWORD",
        "PASSWORD: ssh-ed25519",
        "password_dedupe_key=abc_def",
        "admin_passwd: secrets.ADMIN",
        "Password: config.vault_path",
    ],
)
def test_no_carve_out_ever_applies_to_a_password_name(text: str) -> None:
    findings = [finding for finding in _findings(text) if finding.rule == "assignment"]
    assert findings and all(legacy.password_kind(finding) for finding in findings)
    # The same value under a non-password name would be excused by a
    # carve-out, so the password rule is what refuses it.
    assert any(any(carve(finding) for carve in legacy.CARVE_OUTS) for finding in findings)
    assert legacy.commit_gate_refuses("Note", text)


# Round 6: a carve-out ends at a boundary. Each probe glues a token straight
# onto something a carve-out excuses; v0.16.0 refused every one at commit,
# and d764c2b stored every one.
def _glued_probes() -> dict[str, tuple[str, str]]:
    """label -> (glued text, the same text with the token removed)."""

    rng = random.Random(6)
    tokens = {
        "sk-proj-": "sk" + "-proj-" + _rand(rng, 48),
        "ghp_": _GHP_36,
        "AKIA": "AK" + "IA" + _rand(rng, 16, string.ascii_uppercase + string.digits),
        "xoxb-": "xo" + "xb-" + _rand(rng, 12, string.digits) + "-" + _rand(rng, 13, string.digits) + "-" + _rand(rng, 24),
        "glpat-": "glp" + "at-" + _rand(rng, 20),
        "sk-ant-": "sk" + "-ant-api03-" + _rand(rng, 93, _BASE62 + "-_"),
    }
    fido = "sk-ssh-ed25519@openssh.com"
    body = throwaway_key("openssh_ed25519.pub").split()[1]
    probes = {f"FIDO key type, then {name}": (fido + token, fido) for name, token in tokens.items()}
    probes["public key body, then ghp_"] = ("ssh-ed25519 " + body + _GHP_36, "ssh-ed25519 " + body)
    # ssh-dss has no marker of its own, so only the public key rule reads it.
    probes["ssh-dss public key body, then ghp_"] = ("ssh-dss " + body + _GHP_36, "ssh-dss " + body)
    probes["key type marker, then keyssk-proj-"] = (
        "Prefer ssh-ed25519 keys" + tokens["sk-proj-"], "Prefer ssh-ed25519 keys over ssh-rsa keys",
    )
    probes["code reference, random attribute"] = ("api_key = settings." + R, "api_key = settings.OPENAI_API_KEY")
    cursor = "next_page_token=" + "CiAKGjBpNDd2"
    probes["page cursor, then ghp_"] = (cursor + _GHP_36, cursor)
    return probes


@pytest.mark.parametrize("label", sorted(_glued_probes()))
def test_round6_a_token_glued_to_an_excused_thing_is_refused_at_commit_and_the_floor(label: str) -> None:
    glued, clean = _glued_probes()[label]
    assert oracle.commit_gate_refuses("Deploy note", glued)
    store = _store()
    assert _commit(store, glued)["status"] == "rejected"
    assert _memory_count(store) == 0
    assert "credential_material" in hard_floor_hits(_candidate(glued))
    # Guards the guard: without the token the carve-out still applies, at
    # both doors.
    assert not legacy.commit_gate_refuses("Deploy note", clean)
    assert "credential_material" not in hard_floor_hits(_candidate(clean))


def test_findings_are_excused_one_at_a_time() -> None:
    token = "gh" + "p_" + R + R[:16]
    assert not legacy.commit_gate_refuses("Note", "We use sk-learn here.")
    assert legacy.commit_gate_refuses("Note", f"We use sk-learn here, token {token}")


# ---------------------------------------------------------------------------
# (b) No carve-out passes a real key or a generated token.
# ---------------------------------------------------------------------------

_SECRET_NAMES = (
    "GITHUB_TOKEN", "TOKEN_GITHUB", "GITHUB_TOKEN_WRITE", "API_KEY", "API_KEY_RAW", "PASSWORD_DB", "DB_PASSWORD",
    "SECRET_KEY", "VAULT_TOKEN", "AWS_SECRET_ACCESS_KEY", "SLACK_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
    "PRIVATE_KEY_DATA", "client_secret", "apiKey", "auth_token", "access_token", "api-key", "primary_key",
    "routing_key", "aes_key",
)


def _digits(rng: random.Random, size: int) -> str:
    return _rand(rng, size, string.digits)


_TOKEN_SHAPES = {
    "github classic": lambda r: "gh" + "p_" + _rand(r, 36),
    "github fine-grained": lambda r: "github" + "_pat_" + _rand(r, 22) + "_" + _rand(r, 59),
    "gitlab": lambda r: "glp" + "at-" + _rand(r, 20),
    "slack bot": lambda r: "xo" + "xb-" + _digits(r, 12) + "-" + _digits(r, 13) + "-" + _rand(r, 24),
    "openai project": lambda r: "sk" + "-proj-" + _rand(r, 48),
    "anthropic": lambda r: "sk" + "-ant-api03-" + _rand(r, 93, _BASE62 + "-_"),
    "stripe live": lambda r: "sk" + "_live_" + _rand(r, 24),
    "aws key id": lambda r: "AK" + "IA" + _rand(r, 16, string.ascii_uppercase + string.digits),
    "aws secret": lambda r: _rand(r, 40, _BASE62 + "+/"),
    "google api key": lambda r: "AI" + "za" + _rand(r, 35, _BASE62 + "-_"),
    "google oauth": lambda r: "ya" + "29." + _rand(r, 60, _BASE62 + "-_"),
    "jwt": lambda r: "ey" + "J" + _rand(r, 20) + ".eyJ" + _rand(r, 30) + "." + _rand(r, 43, _BASE62 + "-_"),
    "npm": lambda r: "np" + "m_" + _rand(r, 36),
    "vault": lambda r: "hv" + "s." + _rand(r, 24),
    "telegram": lambda r: _digits(r, 10) + ":AA" + _rand(r, 33, _BASE62 + "-_"),
    "discord": lambda r: _rand(r, 24) + "." + _rand(r, 6) + "." + _rand(r, 27),
    "mapbox": lambda r: "sk" + ".eyJ1Ijoi" + _rand(r, 20) + "." + _rand(r, 22),
    "sendgrid": lambda r: "SG" + "." + _rand(r, 22) + "." + _rand(r, 43),
    "twilio": lambda r: "SK" + _rand(r, 32, "0123456789abcdef"),
    "django": lambda r: "django-insecure-" + _rand(r, 40, _BASE62 + "!@#$%^&*(-_=+)"),
    "hex 64": lambda r: _rand(r, 64, "0123456789abcdef"),
    "generic 32": lambda r: _rand(r, 32),
}
_TOKEN_CONTEXTS = (
    "{name}={token}", 'export {name}="{token}"', "{name}: {token}", '{{"{name}": "{token}"}}',
    "[default]\n{name} = {token}", "{name}='{token}'", "the {name} is {token}",
)


def test_harness_v4_no_carve_out_passes_a_generated_token_under_a_secret_name() -> None:
    rng = random.Random(20260923)
    total = refused = 0
    for shape, make in sorted(_TOKEN_SHAPES.items()):
        for name in _SECRET_NAMES:
            for context in _TOKEN_CONTEXTS:
                token = make(rng)
                text = context.format(name=name, token=token)
                for fields in (("Note", text, None, None, ()), ("Note", "see refs", None, None, ({name: token},))):
                    total += 1
                    v0160 = legacy.v0160_commit_gate_refuses(*fields)
                    # No carve-out excuses anything a token put there.
                    assert legacy.commit_gate_refuses(*fields) is v0160, (shape, name, context)
                    refused += v0160
    # Guards the guard: the grammar is almost all refused by v0.16.0, so the
    # equality above is about refusals, not about nothing.
    assert total == len(_TOKEN_SHAPES) * len(_SECRET_NAMES) * len(_TOKEN_CONTEXTS) * 2
    assert refused > 0.9 * total, (refused, total)


def test_harness_v4_the_commit_door_refuses_one_of_every_token_shape_at_the_store() -> None:
    rng = random.Random(7)
    store = _store()
    for shape, make in sorted(_TOKEN_SHAPES.items()):
        token = make(rng)
        result = _commit(store, f"{rng.choice(_SECRET_NAMES)}={token}")
        assert result["status"] == "rejected", shape
        assert _nowhere(store, token[-16:]), shape
    assert _memory_count(store) == 0


def _key_placements(key: str) -> dict[str, str]:
    encoded = base64.b64encode(key.encode()).decode()
    dashless = key.replace(_DASHES, "")
    return {
        "raw": key,
        "folded": " ".join(key.split()),
        "dashless": dashless,
        "em dash": key.replace(_DASHES, "\u2014" * 5),
        "quoted": "\n".join("> " + line for line in dashless.splitlines()),
        "json": json.dumps(key)[1:-1],
        "base64 wrapped 64": "\n".join(encoded[index : index + 64] for index in range(0, len(encoded), 64)),
        "base64 under a name": "SSH_PRIVATE_KEY_B64=" + encoded,
        "xxd plain": _xxd_plain(key),
        "beside its public key": key + "\n" + throwaway_key("openssh_ed25519.pub"),
    }


# Hex dumps of these two keys pass every door, and passed v0.16.0: the first
# 60-digit line decodes to "-----BEGIN ENCRYPTED PRIVATE K" and "-----BEGIN
# PGP PRIVATE KEY BLO", which neither check reads. Round 5 was limited to two
# detector fixes, so this is a documented residual, pinned so a fix is seen.
_HEX_DUMP_RESIDUAL = frozenset({"pkcs8_rsa_encrypted.pem", "pgp_ed25519.asc", "pgp_rsa.asc"})


def _only_public_key_parts_excused(fields: tuple[object, ...]) -> bool:
    """Every finding a carve-out excuses is an SSH key type or public key.

    A PuTTY file carries its public key in the clear, and one placement pastes
    a public key beside the private one: excusing those is carve-out (a)'s
    job. The private material is what must not be excused.
    """

    findings = list(legacy._commit_findings(*fields))  # type: ignore[arg-type]
    excused = [finding for finding in findings if legacy.excused(finding)]
    return all(legacy.carve_out_ssh_public_key(finding) for finding in excused) and not any(
        legacy.carve_out_sk_word_chain(finding)
        or legacy.carve_out_structural_key_name(finding)
        or legacy.carve_out_code_reference(finding)
        for finding in excused
    )


@pytest.mark.parametrize("name", PRIVATE_KEY_NAMES)
def test_harness_v4_no_carve_out_passes_a_real_private_key(name: str) -> None:
    store = _store()
    for placement, text in _key_placements(throwaway_key(name)).items():
        for fields in (("Note", text, None, None, ()), ("Note", "see refs", None, None, ({"key": text},))):
            if legacy.commit_gate_refuses(*fields) is not legacy.v0160_commit_gate_refuses(*fields):
                assert _only_public_key_parts_excused(fields), (name, placement)
                # The key is still refused, by the detector.
                assert carries_credential_material(fields[1], list(fields[4])), (name, placement)
        if placement == "xxd plain" and name in _HEX_DUMP_RESIDUAL:
            assert not legacy.v0160_commit_gate_refuses("Deploy note", text)
            assert not carries_credential_material("Deploy note", text)
            continue
        assert _commit(store, text)["status"] == "rejected", (name, placement)
    assert _memory_count(store) == 0


# ---------------------------------------------------------------------------
# (c) Benign measurement: refusals counted against v0.16.0, pinned.
# ---------------------------------------------------------------------------


def _named_adversary_notes() -> list[tuple[str, str, list[object]]]:
    """The design round's named benign notes: T3, F4 and F6, as commit input."""

    notes: list[tuple[str, str, list[object]]] = []
    for _label, fields in sorted(_T3_FALSE.items()):
        strings = [field for field in fields if isinstance(field, str)]
        refs: list[object] = []
        for field in fields:
            if isinstance(field, list):
                # String refs name sources a bare store does not hold, so they
                # join the text: both checks read it the same way.
                strings.extend(item for item in field if isinstance(item, str))
            elif isinstance(field, dict):
                refs.append(field)
        title, text = (strings[0], " ".join(strings[1:])) if len(strings) > 1 else ("Note", strings[0] if strings else "note")
        notes.append((title, text, refs))
    for body in (
        {"fact_key": "release_mode"},
        {"key": "Q3-plan"},
        {"token_budget": 128000},
        {"subject_key": "project.alice.release", "subject": "alice"},
        {"openclaw_dedupe_key": "9f86d081884c7d65" * 4},
    ):
        notes.append(("Decision: release", "Structured decision body.", [body]))
    notes.append(("Note", "We use sk-learn for the churn model baseline.", []))
    notes.append(("Note", "Prefer ssh-ed25519 keys over ssh-rsa keys", []))
    return notes


def _corpus_groups() -> dict[str, list[tuple[str, str, list[object]]]]:
    return {
        "doc sentences": [("Doc note", sentence, []) for sentence in DOC_SENTENCES],
        "credential vocabulary": [("Doc note", sentence, []) for sentence in CREDENTIAL_VOCABULARY_SENTENCES],
        "promotion notes": [(title, text, []) for title, text, *_rest in ALL_NOTES],
        "named adversary notes": _named_adversary_notes(),
    }


# (rows, commit refused now, commit refused by v0.16.0, import refused now).
# v0.16.0 had no credential check at import, so it refused none there.
# Measured 2026-09-23 on this branch and on v0.16.0 (880915a) itself.
_BENIGN_PINS = {
    "doc sentences": (400, 0, 0, 0),
    "credential vocabulary": (474, 6, 6, 4),
    "promotion notes": (100, 0, 0, 0),
    "named adversary notes": (34, 16, 27, 0),
}


@pytest.mark.parametrize("group", sorted(_BENIGN_PINS))
def test_harness_v4_benign_refusals_against_v0160_are_pinned(group: str) -> None:
    store = _store()
    service = VNextMemoryCommitService(store)
    commit_now = commit_v0160 = import_now = rejected_any = 0
    rows = _corpus_groups()[group]
    for index, (title, text, refs) in enumerate(rows):
        payload: dict[str, object] = {"title": title, "canonical_text": text, "source_refs": refs}
        request = memory_commit_request_from_payload(payload, user_id=store.user_id)
        result = service.commit(identity=None, request=request)
        rejected_any += result["status"] == "rejected"
        refused = result["status"] == "rejected" and bool(
            {"unsafe_secret_storage", "unsafe_text_expansion"} & set(result.get("reasons") or ())
        )
        v0160 = oracle.commit_gate_refuses(
            request.title, request.canonical_text, request.conversation_excerpt, request.rationale, request.source_refs
        )
        # Every refusal now is either v0.16.0's or the detector's.
        if refused and not v0160:
            assert carries_credential_material(request.title, request.canonical_text, list(request.source_refs))
        commit_now += refused
        commit_v0160 += v0160
        record = {"id": str(index), "title": title, "canonical_text": text, "summary": text[:280],
                  "value": refs[0] if refs and isinstance(refs[0], dict) else None}
        import_now += _memory_record_credential_finding(record, line_no=index + 1) is not None
    # At the store: every accepted row is there, every refused one is not.
    assert _memory_count(store) == len(rows) - rejected_any
    assert (len(rows), commit_now, commit_v0160, import_now) == _BENIGN_PINS[group]


# ---------------------------------------------------------------------------
# Linear time, door-7 style: 50 KB and 200 KB through both doors.
# ---------------------------------------------------------------------------

_ADVERSARIAL_UNITS = {
    "underscore run": "a_",
    "key segments": "key_",
    "jwt starts": "eyJ-",
    "spaced underscores": "a _ ",
    "prose starts": "password is ",
    "spaced dots": "a . . . . . \x0b",
    "base64 of an underscore run": base64.b64encode(("a_" * 150).encode()).decode() + " ",
    "structural pairs": "cache_key=abc_def ",
    "code references": "api_key=settings.X ",
}
_BUDGET_SECONDS = 2.0


def _door_seconds(text: str) -> float:
    started = time.perf_counter()
    legacy.commit_gate_refuses("title", text)
    legacy.promotion_floor_refuses("title", text)
    return time.perf_counter() - started


@pytest.mark.parametrize("label", sorted(_ADVERSARIAL_UNITS))
def test_harness_v4_the_reimplementation_is_linear(label: str) -> None:
    unit = _ADVERSARIAL_UNITS[label]
    small = (unit * (50_000 // len(unit) + 1))[:50_000]
    large = (unit * (200_000 // len(unit) + 1))[:200_000]
    small_seconds = min(_door_seconds(small) for _ in range(3))
    large_seconds = min(_door_seconds(large) for _ in range(3))
    assert large_seconds < _BUDGET_SECONDS, (label, large_seconds)
    # Linear is 4x; quadratic is 16x.
    assert large_seconds < max(8 * small_seconds, 0.02), (label, small_seconds, large_seconds)
    # Guards the guard: the same path still finds a real assignment after it.
    assert legacy.commit_gate_refuses("title", large + f" TOKEN_GITHUB={R}")
