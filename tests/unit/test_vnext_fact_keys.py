from __future__ import annotations

import json

import pytest

from alicebot_api import vnext_fact_keys
from alicebot_api.vnext_fact_keys import (
    MAX_FACT_KEYS,
    MAX_FACT_KEY_LENGTH,
    OpenAICompatibleFactKeyProvider,
    VNextFactKeyConfigurationError,
    VNextFactKeyProviderError,
    apply_fact_keys,
    attach_memory_fact_keys,
    backfill_memory_fact_keys,
    derive_deterministic_fact_keys,
    derive_fact_keys,
    fact_keys_text,
    get_fact_key_provider,
    split_fact_keys,
)


@pytest.fixture(autouse=True)
def _clear_fact_key_env(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_FACT_KEYS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_FACT_KEYS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_FACT_KEYS_API_KEY", raising=False)


def _memory(**overrides: object) -> dict[str, object]:
    memory: dict[str, object] = {
        "id": "00000000-0000-0000-0000-00000000000a",
        "memory_key": "vnext.capture.episode.abcdef0123456789",
        "title": "Bike-a-Thon result",
        "canonical_text": "The Bike-a-Thon raised $5,000 for the hospital.",
        "summary": "Bike-a-Thon raised $5,000.",
        "value": {"text": "The Bike-a-Thon raised $5,000 for the hospital."},
    }
    memory.update(overrides)
    return memory


# -- deterministic tier --------------------------------------------------------


def test_derivation_is_deterministic_across_calls() -> None:
    memory = _memory()
    first = derive_deterministic_fact_keys(memory)
    assert first == derive_deterministic_fact_keys(memory)
    assert first == derive_fact_keys(memory)  # unconfigured == tier (a) only


def test_category_and_amount_keys_bridge_the_bike_a_thon_gap() -> None:
    keys = derive_deterministic_fact_keys(_memory())
    joined = " ".join(keys).lower()
    # The category-phrased question's vocabulary must all be present:
    # "charity event fundraising total" shares zero tokens with the memory.
    for term in ("charity", "event", "fundraising", "total"):
        assert term in joined
    assert "5000 dollars total amount" in keys


def test_keys_are_capped_in_count_and_length() -> None:
    text = (
        "The fundraiser gala had yoga and a marathon, sushi from a restaurant, "
        "a novel and a documentary, a flight and hotel, my laptop, a guitar, "
        "an orchid, chess, a birthday, allergy medication, and a workshop "
        "for engineers costing $25 or 10% off plus 5 km at 3 hrs and 2 kg."
    )
    keys = derive_deterministic_fact_keys(_memory(canonical_text=text, summary=text, title=text))
    assert 0 < len(keys) <= MAX_FACT_KEYS
    assert all(len(key) <= MAX_FACT_KEY_LENGTH for key in keys)
    assert all("\n" not in key for key in keys)


def test_keys_must_contribute_novel_tokens() -> None:
    # Every derivable phrase already appears in the memory's own text, so
    # no key survives the novelty filter (nothing new to index).
    memory = _memory(
        title="charity event",
        canonical_text="A charity event fundraiser: fundraising total amount $5,000 (5000 dollars).",
        summary="charity event fundraiser fundraising total amount 5000 dollars",
        value={"text": "charity event fundraiser fundraising total amount 5000 dollars"},
        memory_key="vnext.capture.episode.abcdef0123456789",
    )
    assert derive_deterministic_fact_keys(memory) == []


def test_unit_percent_and_value_attribute_phrasings() -> None:
    memory = _memory(
        title="Race",
        canonical_text="Finished the 10 km race in 90 mins, beating 20% of the field.",
        summary=None,
        value={"text": "Finished the 10 km race.", "placement": "top half"},
        memory_key="vnext.capture.episode.ff00",
    )
    keys = derive_deterministic_fact_keys(memory)
    assert "10 kilometers distance" in keys
    assert "90 minutes duration" in keys
    assert "20 percent percentage" in keys
    assert "placement top half" in keys


def test_value_provenance_attributes_never_become_fact_keys() -> None:
    """Captured memories carry ``value.source_id``/``value.source_chunk_id``
    plumbing; indexing "source chunk id <uuid>" into FTS puts identical
    uuid tokens on every captured row (897/897 on the benchmark), so
    provenance attributes and identifier-shaped values must be skipped."""
    memory = _memory(
        value={
            "text": "The Bike-a-Thon raised $5,000 for the hospital.",
            "source_id": "0a34ad58-1a2b-4c3d-8e4f-556677889900",
            "source_chunk_id": "75832dbd-9f8e-4d7c-8b6a-112233445566",
        },
    )
    keys = derive_deterministic_fact_keys(memory)
    joined = " ".join(keys).lower()
    assert "source id" not in joined
    assert "source chunk id" not in joined
    assert "0a34ad58" not in joined
    assert "75832dbd" not in joined
    # Real derivations from the text fields still come through.
    assert any("charity" in key for key in keys)


def test_id_named_value_attributes_are_excluded_by_name() -> None:
    memory = _memory(
        value={
            "text": "Finished the race.",
            "ticket_id": "ABC-123",
            "order_ids": "17, 18",
            "thread_id": 42,
        },
    )
    keys = derive_deterministic_fact_keys(memory)
    joined = " ".join(keys).lower()
    for fragment in ("ticket", "order", "thread"):
        assert fragment not in joined


def test_identifier_shaped_string_values_are_excluded_by_pattern() -> None:
    uuid_value = "5c02b1de-93f8-4a5a-a111-222222222222"
    hex_value = "abcdef0123456789"
    memory = _memory(
        value={
            "text": "Finished the race.",
            "badge": uuid_value,  # innocuous name, identifier-shaped value
            "checksum": hex_value,
            "placement": "top half",  # real attribute survives
        },
    )
    keys = derive_deterministic_fact_keys(memory)
    joined = " ".join(keys).lower()
    assert uuid_value not in joined
    assert hex_value not in joined
    assert "placement top half" in keys


def test_short_or_wordy_values_are_not_mistaken_for_identifiers() -> None:
    memory = _memory(
        value={
            "text": "Finished the race.",
            "cafe": "deadbeef",  # 8 hex chars: below the identifier length bar
            "grade": "ab",
            "seat": "14a",
        },
    )
    keys = derive_deterministic_fact_keys(memory)
    assert "cafe deadbeef" in keys
    assert "grade ab" in keys
    assert "seat 14a" in keys


def test_memory_key_words_only_from_human_authored_keys() -> None:
    human = derive_deterministic_fact_keys(
        {"memory_key": "profile.vehicle.primary", "canonical_text": "Sam drives a blue Outback."}
    )
    assert "profile vehicle primary" in human

    for generated in (
        "vnext.capture.episode.abcdef0123456789",
        "agentic_memory.preference.5c02b1de-93f8-4a5a-a111-222222222222",
        "notes.2026.q3",
    ):
        keys = derive_deterministic_fact_keys(
            {"memory_key": generated, "canonical_text": "Sam drives a blue Outback."}
        )
        assert not any("capture" in key or "agentic" in key or "q3" in key for key in keys)


def test_entity_names_aliases_and_type_words_recombine() -> None:
    keys = derive_deterministic_fact_keys(
        _memory(),
        entities=(
            {"name": "Acme Corp", "entity_type": "organization", "aliases": ["Acme Corporation"]},
            {"name": "Hermes", "entity_type": "agent"},
        ),
    )
    assert "Acme Corp organization company" in keys
    assert "Acme Corporation" in keys
    assert "Hermes agent" in keys


def test_empty_memory_derives_no_keys() -> None:
    assert derive_deterministic_fact_keys({"memory_key": "vnext.capture.a.b"}) == []
    assert derive_deterministic_fact_keys({}) == []


def test_fact_keys_text_round_trips_and_empty_means_processed() -> None:
    keys = ["charity event fundraiser fundraising", "5000 dollars total amount"]
    assert split_fact_keys(fact_keys_text(keys)) == keys
    assert fact_keys_text([]) == ""
    assert split_fact_keys("") == []
    assert split_fact_keys(None) == []


# -- provider seam -------------------------------------------------------------


def test_get_fact_key_provider_requires_base_url_and_model(monkeypatch) -> None:
    assert get_fact_key_provider() is None

    monkeypatch.setenv("ALICE_FACT_KEYS_BASE_URL", "http://localhost:11434/v1")
    assert get_fact_key_provider() is None

    monkeypatch.setenv("ALICE_FACT_KEYS_MODEL", "qwen2.5:3b")
    provider = get_fact_key_provider()
    assert provider is not None
    assert provider.base_url == "http://localhost:11434/v1"
    assert provider.model == "qwen2.5:3b"
    assert provider.api_key is None

    monkeypatch.setenv("ALICE_FACT_KEYS_API_KEY", "sk-local")
    provider_with_key = get_fact_key_provider()
    assert provider_with_key is not None
    assert provider_with_key.api_key == "sk-local"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_provider_posts_chat_completions_shape_and_parses_json_array(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout, enforce_public_peer):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": '["charity event", "fundraising total"]'}}
                ]
            }
        ).encode("utf-8")
        return _FakeResponse(body)

    monkeypatch.setattr(vnext_fact_keys, "open_provider_url", fake_urlopen)
    provider = OpenAICompatibleFactKeyProvider(
        base_url="http://localhost:11434/v1/", model="qwen2.5:3b", api_key="sk-local"
    )

    keys = provider.suggest_keys("The Bike-a-Thon raised $5,000.")

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    payload = captured["payload"]
    assert payload["model"] == "qwen2.5:3b"
    assert payload["temperature"] == 0
    assert payload["messages"][1]["content"] == "The Bike-a-Thon raised $5,000."
    assert dict(captured["headers"]).get("Authorization") == "Bearer sk-local"
    assert keys == ["charity event", "fundraising total"]


def test_provider_tolerates_line_output_and_sanitizes(monkeypatch) -> None:
    content = "\n".join(
        [
            "- charity event",
            "* fundraising total ",
            "",
            "this line is far far too many words to be a retrieval key phrase honestly",
            '"amount raised"',
        ]
    )
    monkeypatch.setattr(
        vnext_fact_keys,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(
            json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        ),
    )
    provider = OpenAICompatibleFactKeyProvider(base_url="http://localhost:1234/v1", model="m")

    assert provider.suggest_keys("text") == ["charity event", "fundraising total", "amount raised"]


def test_provider_error_shapes(monkeypatch) -> None:
    monkeypatch.setattr(
        vnext_fact_keys,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(json.dumps({"unexpected": True}).encode("utf-8")),
    )
    provider = OpenAICompatibleFactKeyProvider(base_url="http://localhost:1234/v1", model="m")
    with pytest.raises(VNextFactKeyProviderError, match="chat completion"):
        provider.suggest_keys("text")
    with pytest.raises(VNextFactKeyConfigurationError, match="non-empty"):
        provider.suggest_keys("   ")
    with pytest.raises(VNextFactKeyConfigurationError, match="base_url"):
        OpenAICompatibleFactKeyProvider(base_url="  ", model="m")
    with pytest.raises(VNextFactKeyConfigurationError, match="model"):
        OpenAICompatibleFactKeyProvider(base_url="http://localhost", model=" ")


class _StubProvider:
    provider = "stub"
    model = "stub-keys"

    def __init__(self, keys: list[str] | None = None, error: Exception | None = None) -> None:
        self._keys = keys or []
        self._error = error
        self.calls: list[str] = []

    def suggest_keys(self, text: str) -> list[str]:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return list(self._keys)


def test_llm_tier_appends_after_deterministic_and_respects_caps() -> None:
    provider = _StubProvider(
        keys=[
            "charity event fundraiser fundraising",  # duplicate of tier (a)
            "Bike-a-Thon",  # no novel token vs the memory text
            "sponsored ride donation",  # genuinely new
        ]
    )
    keys = derive_fact_keys(_memory(), provider=provider)
    deterministic = derive_deterministic_fact_keys(_memory())
    assert keys[: len(deterministic)] == deterministic
    assert "sponsored ride donation" in keys
    assert keys.count("charity event fundraiser fundraising") == 1
    assert "Bike-a-Thon" not in keys
    assert len(keys) <= MAX_FACT_KEYS


def test_llm_tier_never_runs_when_unconfigured() -> None:
    provider = _StubProvider(keys=["unused"])
    derive_fact_keys(_memory(), provider=None)
    assert provider.calls == []


def test_attach_with_use_env_provider_false_never_dials_out(monkeypatch) -> None:
    # The commit-path contract: even with the model tier configured, the
    # attach call pinned to the deterministic tier makes no request.
    monkeypatch.setenv("ALICE_FACT_KEYS_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("ALICE_FACT_KEYS_MODEL", "m")

    def _fail_urlopen(request, timeout, enforce_public_peer):  # pragma: no cover - must not run
        raise AssertionError("commit-path attach must not call the model endpoint")

    monkeypatch.setattr(vnext_fact_keys, "open_provider_url", _fail_urlopen)
    memory = _memory()
    store = _AttachStore({str(memory["id"]): memory})

    assert attach_memory_fact_keys(store, memory, use_env_provider=False) is True
    assert store.fact_keys[str(memory["id"])] == fact_keys_text(derive_deterministic_fact_keys(memory))
    assert store.events == []


# -- attach / apply / backfill --------------------------------------------------


class _AttachStore:
    def __init__(self, memories: dict[str, dict[str, object]] | None = None) -> None:
        self.memories = memories or {}
        self.fact_keys: dict[str, str | None] = {}
        self.events: list[dict[str, object]] = []

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        return self.memories.get(memory_id)

    def update_memory_fact_keys(self, *, memory_id: str, fact_keys: str | None) -> dict[str, object] | None:
        if memory_id not in self.memories:
            return None
        self.fact_keys[memory_id] = fact_keys
        return {"id": memory_id}

    def list_memories_missing_fact_keys(self, *, limit: int = 100, after_id: str | None = None) -> list[dict[str, object]]:
        rows = sorted(
            (row for key, row in self.memories.items() if key not in self.fact_keys),
            key=lambda row: str(row["id"]),
        )
        if after_id is not None:
            rows = [row for row in rows if str(row["id"]) > after_id]
        return rows[:limit]

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event


def test_attach_writes_joined_keys_and_skips_stores_without_surface() -> None:
    memory = _memory()
    store = _AttachStore({str(memory["id"]): memory})

    assert attach_memory_fact_keys(store, memory) is True
    stored = store.fact_keys[str(memory["id"])]
    assert "charity event fundraiser fundraising" in stored

    class _NoSurface:
        pass

    assert attach_memory_fact_keys(_NoSurface(), memory) is False


def test_attach_falls_back_to_deterministic_and_logs_on_provider_failure() -> None:
    memory = _memory()
    store = _AttachStore({str(memory["id"]): memory})
    provider = _StubProvider(error=VNextFactKeyProviderError("endpoint down"))

    assert attach_memory_fact_keys(store, memory, provider=provider) is True
    assert store.fact_keys[str(memory["id"])] == fact_keys_text(derive_deterministic_fact_keys(memory))
    assert len(store.events) == 1
    event = store.events[0]
    assert event["event_type"] == "memory.fact_keys_failed"
    assert event["payload_json"]["provider"] == "stub"
    assert event["payload_json"]["error_code"] == "fact_key_derivation_failed"
    assert event["payload_json"]["error_message"] == "Memory fact-key derivation failed"
    assert "endpoint down" not in str(event["payload_json"])


def test_attach_writes_empty_marker_when_nothing_derivable() -> None:
    memory = {"id": "m-1", "memory_key": "vnext.capture.a.b", "canonical_text": ""}
    store = _AttachStore({"m-1": memory})

    assert attach_memory_fact_keys(store, memory) is True
    assert store.fact_keys["m-1"] == ""


def test_apply_fact_keys_fetches_then_attaches() -> None:
    memory = _memory()
    store = _AttachStore({str(memory["id"]): memory})

    assert apply_fact_keys(store, str(memory["id"])) is True
    assert str(memory["id"]) in store.fact_keys
    assert apply_fact_keys(store, "missing") is False


def test_backfill_pages_and_marks_empty_rows_processed() -> None:
    rows = {}
    for index in range(5):
        memory = _memory(id=f"0000000{index}", title=f"Bike-a-Thon {index}")
        rows[str(memory["id"])] = memory
    rows["00000005"] = {"id": "00000005", "memory_key": "vnext.capture.a.b", "canonical_text": ""}
    store = _AttachStore(rows)

    summary = backfill_memory_fact_keys(store, batch_size=2, use_env_provider=False)

    assert summary["updated"] == 6
    assert summary["empty"] == 1
    assert summary["batches"] >= 3
    assert summary["provider"] is None
    assert set(store.fact_keys) == set(rows)

    # Re-running converges: everything already processed.
    again = backfill_memory_fact_keys(store, batch_size=2, use_env_provider=False)
    assert again["updated"] == 0


def test_backfill_counts_provider_failures_and_still_writes_deterministic() -> None:
    memory = _memory(id="m-1")
    store = _AttachStore({"m-1": memory})
    provider = _StubProvider(error=VNextFactKeyProviderError("boom"))

    summary = backfill_memory_fact_keys(store, provider=provider)

    assert summary["provider_failures"] == 1
    assert summary["updated"] == 1
    assert store.fact_keys["m-1"] == fact_keys_text(derive_deterministic_fact_keys(memory))


def test_backfill_requires_store_surface_and_valid_batch_size() -> None:
    with pytest.raises(VNextFactKeyConfigurationError, match="backfill"):
        backfill_memory_fact_keys(object())
    with pytest.raises(VNextFactKeyConfigurationError, match="batch_size"):
        backfill_memory_fact_keys(_AttachStore(), batch_size=0)
