"""Private key material is refused however it is pasted, at the store (S4.4 round 3).

Why this file exists (2026-09-23). The independent adversary review of
8e00cc5 found two regressions against v0.16.0 at the commit door, and the
tower reproduced both with its own throwaway keys. Round 2 dropped the
dashed armor-line refusal and read a header only with a key body after it,
so real keys behind "> " or "# ", double-escaped JSON, a JSON array of lines
and <br>-joined lines all passed. And base64 was decoded only in tokens of
at most 512 characters, which no key file is, so a key as kubeconfig
client-key-data, a Kubernetes tls.key or SSH_PRIVATE_KEY_B64= was never
decoded. On ten more placements a frozen harness measured v0.16.0 at 414 of
420 and the port at 68 of 420.

How it escaped. Round 2's real-key tests pasted each key eight ways, all of
them with the armor line intact and unprefixed and none of them encoded, so
the body rule looked sufficient. The ruling for this round: for private key
material, recall wins over precision.

Every placement here goes through the detector, the commit door and import,
and asserts at the store. The same file pins the other round 3 detector
fixes: PuTTY PPK files, the B64 suffix, prose that is not a key, git's
signingkey, labelled SSH public keys, URL userinfo and Slack.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.credential_floor import carries_credential_material
from alicebot_api.onramp import bootstrap_database, main as onramp_main
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload

from tests.unit.fixtures_throwaway_keys import PRIVATE_KEY_NAMES, PUBLIC_KEY_NAMES, throwaway_key
from tests.unit.fixtures_v0160_export import v0160_prose_export


USER_ID = "00000000-0000-4000-8000-0000000000f3"
# The twelve OpenSSH, PEM and PKCS#8 keys and the two OpenPGP secret keys.
ARMORED_KEYS = tuple(name for name in PRIVATE_KEY_NAMES if not name.endswith(".ppk"))
PPK_KEYS = tuple(name for name in PRIVATE_KEY_NAMES if name.endswith(".ppk"))
_DASHES = "-" * 5
# A certificate is public: a kubeconfig carries one beside the key.
CERTIFICATE_B64 = base64.b64encode(
    f"{_DASHES}BEGIN CERTIFICATE{_DASHES}\nMIIBszCCAVmgAwIBAgIUQ7xK\n{_DASHES}END CERTIFICATE{_DASHES}\n".encode()
).decode()


def _b64(key: str) -> str:
    return base64.b64encode(key.encode()).decode()


def _kubeconfig(key: str) -> str:
    return (
        "apiVersion: v1\nkind: Config\nusers:\n- name: admin\n  user:\n"
        f"    client-certificate-data: {CERTIFICATE_B64}\n    client-key-data: {_b64(key)}\n"
    )


def _tls_secret(key: str) -> str:
    return (
        "apiVersion: v1\nkind: Secret\ntype: kubernetes.io/tls\nmetadata:\n  name: web-tls\ndata:\n"
        f"  tls.crt: {CERTIFICATE_B64}\n  tls.key: {_b64(key)}\n"
    )


def _wrapped(key: str) -> str:
    encoded = _b64(key)
    return "\n".join(encoded[index : index + 76] for index in range(0, len(encoded), 76))


PLACEMENTS = {
    "kubeconfig client-key-data": _kubeconfig,
    "k8s tls.key": _tls_secret,
    "NAME_B64=": lambda key: f"SSH_PRIVATE_KEY_B64={_b64(key)}",
    "base64 wrapped at 76": _wrapped,
    "blockquote": lambda key: "\n".join("> " + line for line in key.splitlines()),
    "hash comment": lambda key: "\n".join("# " + line for line in key.splitlines()),
    "double-escaped JSON": lambda key: json.dumps(json.dumps({"private_key": key})),
    "JSON array of lines": lambda key: json.dumps(key.splitlines()),
    "<br>-joined": lambda key: "<br>".join(key.splitlines()),
}


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
@pytest.mark.parametrize("name", ARMORED_KEYS)
def test_every_real_key_in_every_round3_placement_is_caught(name: str, placement: str) -> None:
    assert carries_credential_material("Deploy note", PLACEMENTS[placement](throwaway_key(name)))


@pytest.mark.parametrize(
    "text",
    [
        _kubeconfig("").replace("    client-key-data: \n", ""),
        f"tls.crt: {CERTIFICATE_B64}",
        "PUBLIC=" + _b64(f"{_DASHES}BEGIN PUBLIC KEY{_DASHES}\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n"),
        "blob " + base64.b64encode(bytes(range(256)) * 2).decode(),
        "pub " + _b64(throwaway_key("openssh_ed25519.pub")),
    ],
    ids=["kubeconfig certificate only", "tls.crt", "public key file", "random bytes", "ssh public key file"],
)
def test_base64_that_is_not_a_private_key_passes(text: str) -> None:
    # Guards the guard: the prefix decode reads the armor, not "long base64".
    assert not carries_credential_material(text)


def _store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "recall@example.com")
    return SQLiteVNextStore(conn, user_id)


def _commit(store: SQLiteVNextStore, text: str) -> dict[str, object]:
    return VNextMemoryCommitService(store).commit(
        identity=None,
        request=memory_commit_request_from_payload({"title": "Deploy note", "canonical_text": text}, user_id=store.user_id),
    )


def _memory_count(store: SQLiteVNextStore) -> int:
    return int(next(iter(store.conn.execute("SELECT count(*) FROM memories")))[0])


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_every_placement_is_refused_at_the_commit_door(placement: str) -> None:
    store = _store()
    for name in ARMORED_KEYS:
        result = _commit(store, PLACEMENTS[placement](throwaway_key(name)))
        assert result["status"] == "rejected", (name, result.get("reasons"))
    assert _memory_count(store) == 0
    # Guards the guard: the same door commits an ordinary note.
    assert _commit(store, "Deploys go out on Tuesdays.")["status"] == "committed"


def _crafted_export(tmp_path: Path, items: list[dict[str, object]]) -> tuple[Path, list[str]]:
    """A real export whose one memory is cloned once per item, footer resealed."""

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        VNextMemoryCommitService(SQLiteVNextStore(conn, USER_ID)).commit(
            identity=None,
            request=memory_commit_request_from_payload(
                {"title": "deploy", "canonical_text": "Deploys go out on Tuesdays."}, user_id=USER_ID
            ),
        )
    dump = tmp_path / "dump.jsonl"
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(dump)]) == 0
    envelopes = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
    header, body, footer = envelopes[0], envelopes[1:-1], envelopes[-1]
    template = next(payload for payload in body if payload["record_type"] == "memory")
    ids = []
    for index, fields in enumerate(items):
        record = dict(template["record"])
        record.update({"id": str(uuid4()), "memory_key": f"crafted.{index}", "commit_digest": None, **fields})
        ids.append(record["id"])
        body.append({**template, "record": record})
    digest = hashlib.sha256()
    for payload in body:
        digest.update(
            (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        )
    footer["record"]["sha256"] = digest.hexdigest()
    footer["record"]["record_count"] = len(body)
    footer["record"]["record_counts"] = {
        key: sum(1 for payload in body if payload["record_type"] == key) for key in footer["record"]["record_counts"]
    }
    crafted = tmp_path / "crafted.jsonl"
    crafted.write_text("".join(json.dumps(payload) + "\n" for payload in [header, *body, footer]), encoding="utf-8")
    return crafted, ids


def _import(tmp_path: Path, export: Path) -> tuple[int, Path]:
    target = tmp_path / f"target-{uuid4().hex[:8]}.db"
    return onramp_main(["import", "--in", str(export), "--db", str(target), "--user-id", USER_ID]), target


IMPORT_PLACEMENTS = {
    **{
        placement: (lambda render: lambda key: {"canonical_text": render(key), "summary": None})(render)
        for placement, render in PLACEMENTS.items()
    },
    "import value holding the base64 form": lambda key: {"value": {"client-key-data": _b64(key)}},
}


@pytest.mark.parametrize("placement", sorted(IMPORT_PLACEMENTS))
def test_every_placement_is_refused_at_import(tmp_path: Path, capsys, placement: str) -> None:
    items = [IMPORT_PLACEMENTS[placement](throwaway_key(name)) for name in ARMORED_KEYS]
    crafted, ids = _crafted_export(tmp_path, items)
    capsys.readouterr()
    code, target = _import(tmp_path, crafted)
    err = capsys.readouterr().err
    assert code == 1
    assert not target.exists()
    assert all(f"memory {memory_id} carries credential material" in err for memory_id in ids)
    assert "PRIVATE KEY" not in err


def test_a_crafted_export_without_keys_imports(tmp_path: Path) -> None:
    # Guards the guard: the resealed export itself is valid.
    crafted, _ids = _crafted_export(tmp_path, [{"canonical_text": "Deploys go out on Fridays.", "summary": None}])
    code, target = _import(tmp_path, crafted)
    assert code == 0 and target.exists()


# ---------------------------------------------------------------------------
# PuTTY PPK (P2 item 4).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PPK_KEYS)
def test_a_putty_private_key_file_is_refused_raw_and_encoded(name: str) -> None:
    key = throwaway_key(name)
    assert carries_credential_material(key)
    assert carries_credential_material("key for the jump box", _kubeconfig(key))
    store = _store()
    assert _commit(store, key)["status"] == "rejected"
    assert _memory_count(store) == 0


def test_a_putty_header_alone_is_prose() -> None:
    assert not carries_credential_material("PuTTY-User-Key-File-3: is the first line of a .ppk file")


# ---------------------------------------------------------------------------
# Prose that is not a key (P2 item 6): v0.16.0 stored these, round 2 refused
# them at commit and at import.
# ---------------------------------------------------------------------------


def test_a_v0160_export_holding_prose_about_private_keys_restores(tmp_path: Path) -> None:
    export = tmp_path / "v0160.jsonl"
    export.write_text(v0160_prose_export(), encoding="utf-8")
    assert "Begin by rotating the private key /certificate pair" in export.read_text(encoding="utf-8")
    code, target = _import(tmp_path, export)
    assert code == 0
    with sqlite_user_connection(target, "00000000-0000-4000-8000-0000000000ee") as conn:
        texts = {str(row["canonical_text"]) for row in conn.execute("SELECT canonical_text FROM memories")}
    assert "Begin with the private key Ubuntu2204 images, then the web tier." in texts
    # Round 4: notes describing the PuTTY format restore too.
    assert any(text.startswith("A .ppk file starts with PuTTY-User-Key-File-3:") for text in texts)
    assert len(texts) == 5


# ---------------------------------------------------------------------------
# Names and values (P2 item 3, P3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD_B64=" + _b64("Kd9xoYWu83nq"),
        "GITHUB_TOKEN_B64=" + _b64("ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"),
        "DB_PASSWORD_B2=" + "Kd9xoYWu83nq",
        "export SSH_PRIVATE_KEY_B64=" + "Kd9xoYWu83nqPlzRt5vW",
    ],
    ids=["password b64", "token b64", "b2 suffix", "private key b64 name"],
)
def test_a_copy_suffix_does_not_hide_the_secret_name(text: str) -> None:
    assert carries_credential_material(text)


@pytest.mark.parametrize(
    "text",
    [
        "[user]\n\tname = Alex\n\tsigningkey = 3AA5C34371567BD2\n[commit]\n\tgpgsign = true",
        '{"user": {"signingKey": "0x4AA5C34371567BD24AA5C34371567BD24AA5C343"}}',
        "git config --global user.signingkey 3AA5C343",
    ],
    ids=["gitconfig", "json fingerprint", "cli"],
)
def test_a_gpg_key_id_under_signingkey_is_not_a_secret(text: str) -> None:
    assert not carries_credential_material(text)


def test_a_real_value_under_signingkey_is_still_one() -> None:
    assert carries_credential_material("signingkey = " + "Kd9xoYWu83nqPlzRt5vW")


@pytest.mark.parametrize("name", ["openssh_ed25519.pub", "openssh_ecdsa.pub"])
def test_a_labelled_ssh_public_key_is_not_a_secret(name: str) -> None:
    assert name in PUBLIC_KEY_NAMES
    assert not carries_credential_material(f"Deploy key: {throwaway_key(name).strip()}")
    assert not carries_credential_material({"deploy_key": throwaway_key(name).strip()})


def test_url_userinfo_ends_before_the_query() -> None:
    assert not carries_credential_material("Open http://localhost:5173?invite=alex@example.com to join.")
    assert carries_credential_material("postgres://app:Kd9xoYWu83nq@db.internal:5432/app")


def test_slack_tokens_are_case_exact_and_carry_digits() -> None:
    assert not carries_credential_material("Congrats, xoxo-SarahAndMike")
    assert not carries_credential_material("XOXB-" + "123456789012-abcdefghijklm")
    assert carries_credential_material("xoxb-" + "123456789012-abcdefghijklm")


# ---------------------------------------------------------------------------
# The import value column and metadata_json are read with their keys.
# Import unwraps rollup_key only in metadata_json and at
# value.rollup.rollup_key, and only when the value matches the producer:
# an optional scope:<16 hex>: prefix, then topic, entity, or semantic,
# then a label. A label passes when it has at least one letter or digit,
# no uppercase or titlecase character, no control, format, surrogate,
# private-use, or unassigned character, and no whitespace other than a
# plain space. The label is still read by value. A
# caller-supplied rollup_key stays a secret name.
# ---------------------------------------------------------------------------

_GAMES = (
    "I played The Witcher 3 for 120 hours",
    "I played Assassin's Creed Odyssey for 70 hours",
    "I played Hollow Knight for 25 hours",
    "I played Stardew Valley for 85 hours",
    "I played Celeste for 10 hours",
)


def test_a_rollup_card_exported_and_imported_on_sqlite_restores(tmp_path: Path, capsys) -> None:
    from alicebot_api.vnext_rollups import ROLLUP_CANDIDATE_KIND, VNextRollupService

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for index, text in enumerate(_GAMES):
            store.create_memory(
                {
                    "memory_key": f"memory.game-{index}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "episode",
                    "title": text[:80],
                    "canonical_text": text,
                    "summary": text[:80],
                    "domain": "personal",
                    "sensitivity": "internal",
                    "project_id": "alpha",
                    "metadata_json": {"session_date": f"2023-0{index + 1}-10", "project_scope": ["alpha"]},
                }
            )
        VNextRollupService(store).propose_rollups()
        cards = [
            row
            for row in store.list_memories(status="candidate")
            if isinstance(row.get("metadata_json"), dict)
            and row["metadata_json"].get("candidate_kind") == ROLLUP_CANDIDATE_KIND
        ]
    assert len(cards) == 1
    assert str(cards[0]["metadata_json"]["rollup_key"]).startswith("scope:")
    dump = tmp_path / "dump.jsonl"
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(dump)]) == 0
    capsys.readouterr()
    code, target = _import(tmp_path, dump)
    assert code == 0, capsys.readouterr().err
    assert target.exists()


_KITCHEN_VECTORS = (
    [0.9, 0.42, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    [0.9, 0.0, 0.42, 0.0, 0.0, 0.0, 0.0, 0.0],
    [0.9, 0.0, 0.0, 0.42, 0.0, 0.0, 0.0, 0.0],
)
_UNRELATED_VECTORS = (
    [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
)


class _PrecomputedEmbeddings:
    provider = "test_embeddings"
    model = "test-embed-3"

    def embed_batch(self, texts: object) -> list[list[float]]:
        raise AssertionError(texts)


def _producer_memory(index: int, text: str, session: str, scope: list[str] | None) -> dict[str, object]:
    metadata: dict[str, object] = {"session_date": session}
    if scope is not None:
        metadata["project_scope"] = scope
    return {
        "memory_key": f"memory.producer-{index}",
        "value": {"text": text},
        "status": "active",
        "memory_type": "episode",
        "title": text[:80],
        "canonical_text": text,
        "summary": text[:80],
        "domain": "personal",
        "sensitivity": "internal",
        "project_id": scope[0] if scope else None,
        "metadata_json": metadata,
    }


def _rollup_keys(store: SQLiteVNextStore) -> dict[str, str]:
    from alicebot_api.vnext_rollups import ROLLUP_CANDIDATE_KIND

    keys: dict[str, str] = {}
    for row in store.list_memories(status="candidate"):
        metadata = row.get("metadata_json")
        if not isinstance(metadata, dict) or metadata.get("candidate_kind") != ROLLUP_CANDIDATE_KIND:
            continue
        key = str(metadata["rollup_key"])
        value = row.get("value")
        assert isinstance(value, dict)
        rollup = value["rollup"]
        assert isinstance(rollup, dict)
        assert rollup["rollup_key"] == key
        keys[str(row["id"])] = key
    return keys


def test_producer_rollup_cards_of_every_kind_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Scoped and unscoped topic, entity, and semantic cards from the real producer restore.

    Labels cover digits (gpt-4o, 2024), a space, a hyphen, and a non-ASCII letter.
    """

    from alicebot_api.credential_floor import is_product_rollup_key
    from alicebot_api.vnext_rollups import RollupOptions, VNextRollupService

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    scoped = ["alpha"]
    specs: list[tuple[str, str, list[str] | None, list[float] | None]] = []
    for index, (month, amount) in enumerate((("March", "$12"), ("June", "$40"), ("October", "$7"))):
        specs.append(
            (
                f"NVIDIA shipped a reference board in {month} and the invoice was {amount}",
                f"2024-0{index + 1}-02",
                scoped,
                None,
            )
        )
    specs.extend(
        [
            (
                "Model-2024 Labs closed its March methods review and paid $80",
                "2023-03-14",
                None,
                None,
            ),
            (
                "A hiring plan at Model-2024 Labs slipped in July after a $15 parts order",
                "2023-07-02",
                None,
                None,
            ),
            (
                "One customer of Model-2024 Labs renewed in November and sent $200",
                "2023-11-19",
                None,
                None,
            ),
            (
                "the gpt-4o billing draft finished beside a workshop fee of $120",
                "2024-09-01",
                scoped,
                None,
            ),
            (
                "a checklist named gpt-4o covered onboarding before a license invoice of $45",
                "2024-09-02",
                scoped,
                None,
            ),
            (
                "launch notes about gpt-4o followed a booth deposit of $60",
                "2024-09-03",
                scoped,
                None,
            ),
            (
                "the fy2024budgetnote binder sat beside the red ledger",
                "2022-05-01",
                None,
                None,
            ),
            (
                "a fy2024budgetnote appendix followed the blue folder",
                "2022-05-02",
                None,
                None,
            ),
            (
                "our fy2024budgetnote annex preceded the green box",
                "2022-05-03",
                None,
                None,
            ),
        ]
    )
    kitchen = (
        "Swapped the leaky kitchen faucet for $120",
        "The toaster in the kitchen died; its replacement cost $45",
        "Hung floating shelves over the counter, $60 in brackets",
    )
    unrelated = (
        "The staging database resets on Sunday nights",
        "Passport renewal appointment is confirmed",
        "The maple sapling doubled its height",
    )
    for scope, month in ((scoped, "03"), (None, "08")):
        for index, text in enumerate(kitchen):
            specs.append((text, f"2026-{month}-0{index + 2}", scope, _KITCHEN_VECTORS[index]))
        for index, text in enumerate(unrelated):
            specs.append((text, f"2026-{month}-1{index + 2}", scope, _UNRELATED_VECTORS[index]))

    precomputed: dict[str, list[float]] = {}
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        store.create_entity(
            {
                "entity_type": "organization",
                "name": "NVIDIA",
                "normalized_name": "naïve",
                "aliases": ["nvidia"],
            }
        )
        for index, (text, session, scope, vector) in enumerate(specs):
            row = store.create_memory(_producer_memory(index, text, session, scope))
            if vector is not None:
                assert store.update_memory_embedding(memory_id=str(row["id"]), vector=vector) is not None
                precomputed[str(row["id"])] = vector
        outcome = VNextRollupService(
            store,
            embedding_provider=_PrecomputedEmbeddings(),
            precomputed_embeddings=precomputed,
        ).propose_rollups(options=RollupOptions(max_rollups=40))
        produced = _rollup_keys(store)
    assert produced, outcome.skipped
    for key in produced.values():
        assert is_product_rollup_key(key), key
    kinds = set()
    for key in produced.values():
        body = key.split(":", 2)[-1] if key.startswith("scope:") else key
        kind, label = body.split(":", 1)
        kinds.add(("scoped" if key.startswith("scope:") else "unscoped", kind))
        assert label == label.casefold()
        assert not any(character.isupper() for character in label)
    assert kinds == {
        ("scoped", "topic"),
        ("unscoped", "topic"),
        ("scoped", "entity"),
        ("unscoped", "entity"),
        ("scoped", "semantic"),
        ("unscoped", "semantic"),
    }
    joined = " ".join(produced.values())
    assert "gpt-4o" in joined
    assert "2024" in joined
    assert " " in joined
    assert "-" in joined
    assert any(ord(character) > 127 and character.islower() for character in joined)

    dump = tmp_path / "dump.jsonl"
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(dump)]) == 0
    capsys.readouterr()
    code, target = _import(tmp_path, dump)
    assert code == 0, capsys.readouterr().err
    with sqlite_user_connection(target, USER_ID) as conn:
        restored = _rollup_keys(SQLiteVNextStore(conn, USER_ID))
    assert restored == produced


def test_producer_scoped_cards_for_ampersand_cjk_and_dotted_i_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three scoped cards from the real producer restore.

    An entity row whose normalized name contains ``&``, an entity row whose
    normalized name is CJK, and an ``İstanbul.online`` domain taken through
    normal extraction. The domain is not a seeded row.
    """

    from alicebot_api.credential_floor import is_product_rollup_key
    from alicebot_api.vnext_entity_names import normalize_entity_name
    from alicebot_api.vnext_rollups import RollupOptions, VNextRollupService

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    scope = ["alpha"]
    domain = "İstanbul.online"
    groups = (
        (
            {
                "entity_type": "organization",
                "name": "Acme Labs",
                "normalized_name": "barnes&noble",
                "aliases": ["acme labs"],
            },
            (
                "Acme Labs shipped a reference board in March and the invoice was $12",
                "Acme Labs closed a methods review in June and paid $40",
                "Acme Labs renewed a customer in October and sent $7",
            ),
        ),
        (
            {
                "entity_type": "organization",
                "name": "Widget Co",
                "normalized_name": "東京",
                "aliases": ["widget co"],
            },
            (
                "Widget Co shipped a reference board in March and the invoice was $18",
                "Widget Co closed a methods review in June and paid $55",
                "Widget Co renewed a customer in October and sent $9",
            ),
        ),
        (
            None,
            (
                f"The booth at {domain} cost $12 in March",
                f"A license from {domain} cost $40 in June",
                f"A renewal at {domain} cost $7 in October",
            ),
        ),
    )
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        index = 0
        for entity, texts in groups:
            if entity is not None:
                store.create_entity(entity)
            for offset, text in enumerate(texts):
                store.create_memory(_producer_memory(index, text, f"2024-0{offset + 1}-02", scope))
                index += 1
        outcome = VNextRollupService(store).propose_rollups(options=RollupOptions(max_rollups=40))
        produced = _rollup_keys(store)
    assert produced, outcome.skipped
    keys = set(produced.values())
    assert any(key.startswith("scope:") and key.endswith(":entity:barnes&noble") for key in keys)
    assert any(key.startswith("scope:") and key.endswith(":entity:東京") for key in keys)
    normalized_domain = normalize_entity_name(domain)
    assert "\u0307" in normalized_domain
    assert any(key.startswith("scope:") and key.endswith(":entity:" + normalized_domain) for key in keys)
    for key in keys:
        assert is_product_rollup_key(key), key
    dump = tmp_path / "dump.jsonl"
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(dump)]) == 0
    capsys.readouterr()
    code, target = _import(tmp_path, dump)
    assert code == 0, capsys.readouterr().err
    with sqlite_user_connection(target, USER_ID) as conn:
        restored = _rollup_keys(SQLiteVNextStore(conn, USER_ID))
    assert restored == produced


def test_import_refuses_rollup_key_outside_value_rollup_and_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A product-shaped rollup_key that is not under value.rollup stays a keyed pair.

    metadata_json does not hold it either. Swapping the value-column unwrap
    for the metadata unwrap would accept this record.
    """

    digest = hashlib.sha256(b"alpha").hexdigest()[:16]
    product = "scope:" + digest + ":entity:nvidia"
    crafted, ids = _crafted_export(
        tmp_path,
        [{"value": {"text": "Operator note.", "rollup_key": product}}],
    )
    capsys.readouterr()
    code, target = _import(tmp_path, crafted)
    err = capsys.readouterr().err
    assert code == 1
    assert not target.exists()
    assert f"memory {ids[0]} carries credential material" in err
    assert product not in err


def test_import_refuses_a_rollup_label_that_reads_as_a_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A label that matches the producer grammar is still read by value."""

    digest = hashlib.sha256(b"label").hexdigest()[:16]
    sk_key = "scope:" + digest + ":topic:" + ("sk-" + "abcdefghijklmnopqrstuvwxyz12")
    xoxb_key = "topic:" + ("xoxb-" + "123456789012" + "-" + "abcdefghijklm")
    crafted, ids = _crafted_export(
        tmp_path,
        [
            {"value": {"text": "A note.", "rollup": {"rollup_key": sk_key, "group_kind": "topic"}}},
            {"value": {"text": "Another note.", "rollup": {"rollup_key": xoxb_key, "group_kind": "topic"}}},
        ],
    )
    capsys.readouterr()
    code, target = _import(tmp_path, crafted)
    err = capsys.readouterr().err
    assert code == 1
    assert not target.exists()
    assert f"memory {ids[0]} carries credential material" in err
    assert f"memory {ids[1]} carries credential material" in err
    assert "sk-" not in err
    assert "xoxb-" not in err


def test_the_import_value_column_and_metadata_are_read_with_their_keys(tmp_path: Path, capsys) -> None:
    stripe_key = "sk_" + "live_" + "51Hq8wLkT2mN9pQ4rS7vX3yZ"
    opaque = "Xq9mZt2L" + "xP9wKc4BVq7m"
    crafted, ids = _crafted_export(
        tmp_path,
        [
            # A structural dedupe key over a digest restores.
            {"value": {"text": "Deploys go out on Tuesdays.", "openclaw_dedupe_key": "a" * 8 + "3251d492" * 7}},
            # A self-identifying value under any name is still caught.
            {"value": {"text": "Billing notes.", "billing": stripe_key}},
            # metadata_json stays keyed: a password under a secret name is caught.
            {"metadata_json": {"db_password": "Kd9xo" + "YWu83nq"}},
            # A secret name in the value column, whose value does not identify itself.
            {"value": {"text": "Operator note.", "api_key": opaque}},
        ],
    )
    capsys.readouterr()
    code, _target = _import(tmp_path, crafted)
    err = capsys.readouterr().err
    assert code == 1
    assert f"memory {ids[0]}" not in err
    assert f"memory {ids[1]} carries credential material" in err
    assert f"memory {ids[2]} carries credential material" in err
    assert f"memory {ids[3]} carries credential material" in err
    assert opaque not in err


def test_every_flagged_key_a_memory_writer_uses_is_accounted_for() -> None:
    """The system keys are enumerated from the writers, not guessed: every
    string key the vNext memory writers put in a dict that the name rule would
    read as a secret name is either exempt at import or never reaches a memory
    row's metadata_json."""

    import ast

    from alicebot_api import credential_floor, onramp

    package = Path(credential_floor.__file__).parent
    writers = (
        "vnext_rollups.py", "vnext_capture.py", "vnext_brain.py", "vnext_consolidation.py",
        "vnext_connectors.py", "vnext_projects.py", "vnext_queue.py", "vnext_memory_commit.py",
        "vnext_memory_propose.py", "vnext_currency.py", "routers/vnext_memories.py", "mcp/review.py",
        "mcp/memories.py",
    )
    flagged: set[str] = set()
    for relative in writers:
        for node in ast.walk(ast.parse((package / relative).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        if credential_floor._name_kind(key.value, "", 0) is not None:
                            flagged.add(key.value)
    # "secret" is a sensitivity label in a lookup table (vnext_memory_commit);
    # "slot_key" is a read-time currency annotation (vnext_currency), never stored.
    # rollup_key is flagged. Import exempts that exact key via SYSTEM_METADATA_KEYS.
    never_in_memory_metadata = {"secret", "slot_key"}
    assert "rollup_key" in flagged
    assert credential_floor._name_kind("rollup_key", "", 0) == "weak"
    assert flagged == set(onramp.SYSTEM_METADATA_KEYS) | never_in_memory_metadata
    assert onramp.SYSTEM_METADATA_KEYS == frozenset({"rollup_key"})


@pytest.mark.parametrize(
    "text",
    [
        "machine api.example.com login deploy password Kd9xoYWu83nq",
        "aws configure set aws_secret_access_key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "curl -u deploy:" + "Kd9xoYWu83nq https://api.example.com/v1/items",
        "docker login -u deploy -p Kd9xoYWu83nq registry.example.com",
        "mysql -u root -pKd9xoYWu83nq appdb",
        "db.internal:5432:app:deploy:Kd9xoYWu83nq",
        "Authorization: Basic dXNlcjpLZDl4b1lXdTgzbnE=",
    ],
    ids=["netrc", "aws configure set", "curl -u", "docker login -p", "mysql -p", "pgpass", "basic auth"],
)
def test_config_shapes_no_rule_reads_are_a_documented_residual(text: str) -> None:
    """Pinned so a change is deliberate: these pass, and the not-covered list
    in docs/memory/promotion-personas.md says so (round 3, P3)."""

    assert not carries_credential_material(text)


# ---------------------------------------------------------------------------
# S4.4 round 4 (2026-09-23). A fresh adversary on 2dbac28 found four narrow
# gaps: a base64 key file wrapped at 40 or 44 characters passed the commit
# door, which folds line breaks to single spaces before the check; armor
# mangled on real keys (dash-like characters, or no dashes with "> "-quoted
# body lines) passed; the PuTTY rule refused notes describing the format; and
# a docs claim about title limits was false. Measured on 2dbac28 at the
# detector: wrapped at 40 and folded, 5 of 16 keys caught; em-dash armor 2 of
# 16; dashless with a quoted body 2 of 16.
# ---------------------------------------------------------------------------

ALL_PRIVATE_KEYS = PRIVATE_KEY_NAMES


def _wrapped_at(key: str, width: int) -> str:
    encoded = _b64(key)
    return "\n".join(encoded[index : index + width] for index in range(0, len(encoded), width))


@pytest.mark.parametrize("width", [40, 44, 64, 76])
def test_r4_a_wrapped_key_file_is_refused_at_the_commit_door(width: int) -> None:
    store = _store()
    for name in ALL_PRIVATE_KEYS:
        result = _commit(store, _wrapped_at(throwaway_key(name), width))
        assert result["status"] == "rejected", (name, width, result.get("reasons"))
    assert _memory_count(store) == 0


_DASH_LIKE = ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"]


@pytest.mark.parametrize("dash", _DASH_LIKE, ids=[f"U+{ord(dash):04X}" for dash in _DASH_LIKE])
def test_r4_armor_with_dash_like_characters_is_refused_by_its_body(dash: str) -> None:
    store = _store()
    for name in ARMORED_KEYS:
        mangled = throwaway_key(name).replace(_DASHES, dash * 5)
        assert carries_credential_material(mangled), name
        assert _commit(store, mangled)["status"] == "rejected", name
    assert _memory_count(store) == 0


@pytest.mark.parametrize("prefix", ["> ", "# ", "// "])
def test_r4_a_dashless_armor_with_quoted_body_lines_is_refused(prefix: str) -> None:
    store = _store()
    for name in ARMORED_KEYS:
        quoted = "\n".join(prefix + line for line in throwaway_key(name).replace(_DASHES, "").splitlines())
        assert carries_credential_material(quoted), name
        assert _commit(store, quoted)["status"] == "rejected", name
    assert _memory_count(store) == 0


_PPK_PROSE = (
    "A .ppk file starts with PuTTY-User-Key-File-3: then the algorithm, followed by Encryption, "
    "Comment, Public-Lines: 2 and Private-Lines: 1 sections.",
    "PuTTY-User-Key-File-2: header, Public-Lines: N, Private-Lines: N, and a Private-MAC: line with the checksum.",
    "Converted with puttygen. PuTTY-User-Key-File-3: marks v3; Private-Lines: 14 counts the lines of the "
    "private blob; Private-MAC: follows.",
)


@pytest.mark.parametrize("note", _PPK_PROSE)
def test_r4_a_note_describing_the_putty_format_is_not_a_key(note: str) -> None:
    assert not carries_credential_material(note)
    assert not carries_credential_material("key format", note)
    # The same prose, base64-encoded, is judged by the same rule.
    assert not carries_credential_material(_b64(note))
    store = _store()
    assert _commit(store, note)["status"] == "committed"


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
@pytest.mark.parametrize("name", PPK_KEYS)
def test_r4_a_putty_key_in_every_placement_is_caught(name: str, placement: str) -> None:
    assert carries_credential_material("Deploy note", PLACEMENTS[placement](throwaway_key(name)))


def test_r4_the_putty_rule_needs_a_mac_or_a_real_body() -> None:
    key = throwaway_key("putty_rsa_v2.ppk")
    header = key.splitlines()[0]
    mac = next(line for line in key.splitlines() if line.startswith("Private-MAC:"))
    private = key[key.index("Private-Lines:") : key.index("Private-MAC:")]
    assert carries_credential_material(f"{header}\n{mac}")
    assert carries_credential_material(f"{header}\n{private}")
    assert not carries_credential_material(f"{header}\nPrivate-Lines: 14\nPrivate-MAC: see the file")


@pytest.mark.parametrize("width", [40, 44])
def test_r4_a_narrow_wrapped_key_file_inside_a_json_string_is_refused(width: int) -> None:
    """The wrap is a literal backslash-n here, not a line break or a space, so
    the base64 join only sees the whole armor line once the escapes are
    decoded. Pins the escape decoding that the body rule's own escape
    handling otherwise masks."""

    for name in ALL_PRIVATE_KEYS:
        escaped = json.dumps(_wrapped_at(throwaway_key(name), width))[1:-1]
        assert "\\n" in escaped
        assert carries_credential_material(escaped), name


# ---------------------------------------------------------------------------
# S4.4 round 5 (2026-09-23): the header-and-body rule refused prose. Round 4
# read the first run of eight base64 characters anywhere after a header, so
# a note that mentions "BEGIN PRIVATE KEY" and then says "1Password",
# "HashiCorp" or "2026/12/31" was refused at every door. The body now starts
# a following line and is a run of at least 16 characters; folded onto one
# line (the commit door folds whitespace) it follows the header's armor
# dashes or a quote prefix, or is a whole 40-character line.
# ---------------------------------------------------------------------------

_HEADER_PROSE = (
    "Stored under BEGIN PRIVATE KEY in 1Password, rotated yearly.",
    "The BEGIN RSA PRIVATE KEY header: HashiCorp Vault holds the rest.",
    "BEGIN PRIVATE KEY rotation is due 2026/12/31 for the signing service.",
    "The file starts BEGIN OPENSSH PRIVATE KEY\n1Password keeps it\n",
    "BEGIN PRIVATE KEY\nHashiCorp\n",
    "BEGIN EC PRIVATE KEY\n2026/12/31\n",
    "> BEGIN PRIVATE KEY\n> 1Password",
    "BEGIN PRIVATE KEY note: see HashiCorp docs 2026/12/31 then rotate",
    # A long run on the header's own line is not a body either.
    "Rotate the BEGIN PRIVATE KEY SigningKeyRotation2026Q3 bundle yearly.",
    # Nor is a lower-case header, whatever follows it: only the case-exact
    # header is read.
    "Begin with the private key\nKubernetesSecretsRotation2026 runbook first.",
)


@pytest.mark.parametrize("note", _HEADER_PROSE)
def test_r5_a_header_mention_before_a_short_word_is_not_a_key(note: str, tmp_path: Path, capsys) -> None:
    assert not carries_credential_material(note)
    assert not carries_credential_material("Rotation", note)
    # At import, the door that reads the detector alone.
    crafted, _ids = _crafted_export(tmp_path, [{"canonical_text": note, "summary": None}])
    code, _target = _import(tmp_path, crafted)
    assert code == 0, capsys.readouterr().err


def test_r5_a_real_body_after_the_same_header_is_still_a_key() -> None:
    # Guards the guard: the same header with a key's first body line.
    body = throwaway_key("pkcs8_ec.pem").splitlines()[1]
    assert carries_credential_material(f"BEGIN PRIVATE KEY\n{body}\n")
    assert not carries_credential_material(f"BEGIN PRIVATE KEY\n{body[:12]}\n")


def test_r5_a_dashless_key_folded_onto_one_line_is_refused_at_the_commit_door() -> None:
    """Dashes lost, then the commit door folds the lines into one: no line
    break, no armor dashes and no quote prefix are left, only the header and
    a whole body line. v0.16.0's phrase list reads RSA, OpenSSH and PKCS#8
    headers, so the EC, encrypted PKCS#8 and OpenPGP keys here rest on the
    detector's folded-line branch alone."""

    store = _store()
    for name in ARMORED_KEYS:
        folded = " ".join(throwaway_key(name).replace(_DASHES, "").split())
        assert carries_credential_material(folded), name
        assert _commit(store, folded)["status"] == "rejected", name
    assert _memory_count(store) == 0
