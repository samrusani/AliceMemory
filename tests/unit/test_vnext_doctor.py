from __future__ import annotations

from alicebot_api.config import Settings
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_secrets import InMemorySecretProvider


class DoctorStore:
    def __init__(self) -> None:
        self.settings: list[dict[str, object]] = []
        self.states: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    def connector_storage_status(self) -> dict[str, object]:
        return {
            "connector_settings_exists": True,
            "connector_state_exists": True,
            "artifact_quality_ratings_exists": True,
            "scheduler_workflows_exists": True,
            "scheduler_runs_exists": True,
            "pgvector_version": "0.8.0",
            "migration_revision": "20260511_0070",
        }

    def list_connector_settings(self) -> list[dict[str, object]]:
        return self.settings

    def list_connector_states(self) -> list[dict[str, object]]:
        return self.states

    def list_events(self, **_kwargs) -> list[dict[str, object]]:
        return self.events

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def list_sources(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def list_memories(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def list_artifacts(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def list_artifact_quality_ratings(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def list_open_loops(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def list_scheduler_runs(self, **_kwargs) -> list[dict[str, object]]:
        return []

    def get_connector_setting(self, connector_name: str) -> dict[str, object] | None:
        return next((row for row in self.settings if row["connector_name"] == connector_name), None)

    def upsert_connector_setting(self, setting: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            "id": f"setting-{setting['connector_name']}",
            "connector_name": setting["connector_name"],
            "enabled": setting.get("enabled", False),
            "configured": setting.get("configured", False),
            "default_domain": setting["default_domain"],
            "default_sensitivity": setting["default_sensitivity"],
            "sync_mode": setting.get("sync_mode", "manual"),
            "poll_interval_seconds": setting.get("poll_interval_seconds"),
            "secret_ref": setting.get("secret_ref"),
            "validation_errors_json": setting.get("validation_errors_json", []),
            "metadata_json": setting.get("metadata_json", {}),
        }
        self.settings = [item for item in self.settings if item["connector_name"] != row["connector_name"]]
        self.settings.append(row)
        return row

    def get_connector_state(self, connector_name: str, cursor_type: str = "sync_cursor") -> dict[str, object] | None:
        return next(
            (
                row
                for row in self.states
                if row["connector_name"] == connector_name and row.get("cursor_type", "sync_cursor") == cursor_type
            ),
            None,
        )

    def upsert_connector_state(self, state: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            "id": f"state-{state['connector_name']}",
            "connector_name": state["connector_name"],
            "cursor_type": state.get("cursor_type", "sync_cursor"),
            "cursor_value": state.get("cursor_value"),
            "items_seen": state.get("items_seen", 0),
            "items_captured": state.get("items_captured", 0),
            "items_deduped": state.get("items_deduped", 0),
            "items_failed": state.get("items_failed", 0),
        }
        self.states = [item for item in self.states if item["connector_name"] != row["connector_name"]]
        self.states.append(row)
        return row


def test_doctor_fix_safe_initializes_missing_connector_defaults() -> None:
    store = DoctorStore()

    payload = VNextDoctorService(store).run(fix_safe=True, ci=True)

    assert payload["blocking_failure_count"] == 0
    assert {row["connector_name"] for row in store.settings} >= {
        "telegram",
        "local_folder",
        "browser_clipper",
        "agent_output",
    }
    assert any(check["name"] == "connector_settings" and check["status"] == "pass" for check in payload["checks"])


def test_doctor_accepts_on_demand_telegram_without_a_secret() -> None:
    store = DoctorStore()
    store.settings.append(
        {
            "id": "setting-telegram",
            "connector_name": "telegram",
            "enabled": True,
            "configured": True,
            "default_domain": "personal",
            "default_sensitivity": "private",
            "sync_mode": "on_demand",
            "metadata_json": {"config_json": {"allowed_chat_ids": ["999001"]}},
        }
    )
    store.states.append({"id": "state-telegram", "connector_name": "telegram", "cursor_type": "sync_cursor"})
    for connector_name in ("local_folder", "browser_clipper", "agent_output"):
        store.settings.append(
            {
                "id": f"setting-{connector_name}",
                "connector_name": connector_name,
                "enabled": False,
                "configured": False,
                "default_domain": "project",
                "default_sensitivity": "private",
                "sync_mode": "on_demand",
                "metadata_json": {"config_json": {}},
            }
        )
        store.states.append({"id": f"state-{connector_name}", "connector_name": connector_name, "cursor_type": "sync_cursor"})

    payload = VNextDoctorService(store, secret_provider=InMemorySecretProvider()).run(ci=True)

    assert payload["blocking_failure_count"] == 0
    assert all(check["name"] != "telegram_secret_ref" for check in payload["checks"])


def test_doctor_connector_storage_failure_omits_exception_type_and_text() -> None:
    sentinel = "UNIQUE_DOCTOR_STORAGE_EXCEPTION_SENTINEL"
    store = DoctorStore()

    def fail_settings() -> list[dict[str, object]]:
        raise RuntimeError(sentinel)

    store.list_connector_settings = fail_settings  # type: ignore[method-assign]

    payload = VNextDoctorService(store).run(ci=True)

    check = next(item for item in payload["checks"] if item["name"] == "connector_storage")
    assert check["message"] == "Connector settings/state storage is unavailable."
    assert "RuntimeError" not in str(check)
    assert sentinel not in str(check)


def test_doctor_blocks_pgvector_older_than_required_version() -> None:
    store = DoctorStore()
    original_status = store.connector_storage_status

    def old_pgvector_status() -> dict[str, object]:
        return {**original_status(), "pgvector_version": "0.7.4"}

    store.connector_storage_status = old_pgvector_status  # type: ignore[method-assign]

    payload = VNextDoctorService(store).run(fix_safe=True, ci=True)

    check = next(item for item in payload["checks"] if item["name"] == "pgvector_version")
    assert check["status"] == "fail"
    assert check["severity"] == "blocking"
    assert check["details"] == {
        "installed_version": "0.7.4",
        "minimum_version": "0.8.0",
    }
    assert payload["blocking_failure_count"] == 1


def test_doctor_warns_when_local_live_cors_is_missing(tmp_path, monkeypatch) -> None:
    web_dir = tmp_path / "apps" / "web"
    web_dir.mkdir(parents=True)
    (tmp_path / ".env").write_text("CORS_ALLOWED_ORIGINS=http://localhost:3000\n", encoding="utf-8")
    (web_dir / ".env.local").write_text(
        "\n".join(
            [
                "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=http://127.0.0.1:8000",
                "NEXT_PUBLIC_ALICEBOT_USER_ID=00000000-0000-0000-0000-000000000001",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "alicebot_api.vnext_doctor.get_settings",
        lambda: Settings(app_env="development", database_url="postgresql://db"),
    )
    store = DoctorStore()

    payload = VNextDoctorService(store, env={}, cwd=tmp_path).run(fix_safe=True, ci=True)

    local_cors = next(check for check in payload["checks"] if check["name"] == "local_vnext_cors")
    assert local_cors["status"] == "fail"
    assert local_cors["severity"] == "warning"
    assert local_cors["recommended_fix"] == "CORS_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000"
    assert local_cors["details"]["missing_origins"] == ["http://127.0.0.1:3000"]


def test_doctor_words_flagged_sources_per_backend_and_says_when_the_scan_stops() -> None:
    token = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"

    class PostgresDoctorStore(DoctorStore):
        def list_sources(self, **_kwargs) -> list[dict[str, object]]:
            return [{"id": "source-1", "title": token, "metadata_json": {"raw_text": "clean note"}}]

        def delete_source(self, **_kwargs) -> dict[str, object]:
            return {"id": "source-1"}

    postgres = next(
        check
        for check in VNextDoctorService(PostgresDoctorStore()).run(ci=True)["checks"]
        if check["name"] == "flagged_sources"
    )
    assert postgres["status"] == "fail"
    assert "DELETE /v0/vnext/sources/{id}" in postgres["message"]
    assert "delete_source" not in postgres["message"]
    assert "SQLite has no delete_source" not in postgres["message"]
    assert token not in postgres["message"]
    assert postgres["details"]["stopped_early"] is False

    class SqliteDoctorStore(DoctorStore):
        def list_sources(self, **_kwargs) -> list[dict[str, object]]:
            return [{"id": "source-9", "title": token, "metadata_json": {}}]

    sqlite = next(
        check
        for check in VNextDoctorService(SqliteDoctorStore()).run(ci=True)["checks"]
        if check["name"] == "flagged_sources"
    )
    assert "SQLite has no delete_source" in sqlite["message"]
    assert "Delete each listed source with delete_source" not in sqlite["message"]

    class WideDoctorStore(DoctorStore):
        def list_sources(self, **kwargs) -> list[dict[str, object]]:
            limit = int(kwargs.get("limit") or 0)
            return [{"id": f"source-{index}", "title": "clean", "metadata_json": {}} for index in range(limit)]

    wide = next(
        check
        for check in VNextDoctorService(WideDoctorStore()).run(ci=True)["checks"]
        if check["name"] == "flagged_sources"
    )
    assert wide["details"]["stopped_early"] is True
    assert "stopped after 10000 sources" in wide["message"]


def test_shared_empty_metadata_scan_preserves_every_flagged_id_and_field(monkeypatch) -> None:
    import alicebot_api.vnext_doctor as doctor
    token = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    rows = [{"id": "clean-1", "title": "clean", "metadata_json": {}},
            {"id": "clean-2", "title": "clean", "metadata_json": {}},
            {"id": "title-1", "title": token, "metadata_json": {}},
            {"id": "title-2", "title": token, "metadata_json": {}}]
    for field in ("author", "uri", "raw_path", "external_id"):
        rows.append({"id": field, "title": "clean", field: token, "metadata_json": {}})
    rows.extend([{"id": "metadata", "title": "clean", "metadata_json": {"raw_text": token}},
                 {"id": "nested", "title": "clean", "metadata_json": {"provenance": {"token": token}}},
                 {"id": "json", "title": "clean", "metadata_json": '{"raw_text":"' + token + '"}'}])

    class Store:
        def list_sources(self, **kwargs):
            return rows

    original = doctor.source_row_is_flagged
    expected = [row["id"] for row in rows if original(row)]
    calls = []
    def counted(row):
        calls.append(row["id"])
        return original(row)
    monkeypatch.setattr(doctor, "source_row_is_flagged", counted)
    assert doctor._flagged_source_scan(Store()) == (expected, False)
    assert "clean-2" not in calls and "title-2" not in calls
    rows[1]["metadata_json"] = {"raw_text": token}
    assert doctor._flagged_source_scan(Store()) == ([row["id"] for row in rows if original(row)], False)


def test_doctor_passes_when_local_live_cors_is_explicit(tmp_path, monkeypatch) -> None:
    web_dir = tmp_path / "apps" / "web"
    web_dir.mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "CORS_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000\n",
        encoding="utf-8",
    )
    (web_dir / ".env.local").write_text(
        "\n".join(
            [
                "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=http://127.0.0.1:8000",
                "NEXT_PUBLIC_ALICEBOT_USER_ID=00000000-0000-0000-0000-000000000001",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "alicebot_api.vnext_doctor.get_settings",
        lambda: Settings(app_env="development", database_url="postgresql://db"),
    )
    store = DoctorStore()

    payload = VNextDoctorService(store, env={}, cwd=tmp_path).run(fix_safe=True, ci=True)

    local_cors = next(check for check in payload["checks"] if check["name"] == "local_vnext_cors")
    assert local_cors["status"] == "pass"
    assert local_cors["details"]["wildcard_present"] is False
