from __future__ import annotations

from contextlib import contextmanager
import importlib
import sys
from typing import Any


MODULE_NAME = "apps.api.alembic.env"


class FakeAlembicConfig:
    def __init__(
        self,
        sqlalchemy_url: str,
        section: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.config_file_name = "alembic.ini"
        self.config_ini_section = "alembic"
        self.sqlalchemy_url = sqlalchemy_url
        self.section = section or {}
        self.attributes = attributes or {}

    def get_main_option(self, option: str) -> str:
        assert option == "sqlalchemy.url"
        return self.sqlalchemy_url

    def get_section(self, section_name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        assert section_name == self.config_ini_section
        base = dict(default or {})
        base.update(self.section)
        return base


class RecordingConnectable:
    def __init__(self) -> None:
        self.connection = object()
        self.connected = False

    @contextmanager
    def connect(self):
        self.connected = True
        yield self.connection


def load_env_module(
    monkeypatch,
    *,
    offline_mode: bool,
    admin_url: str | None = None,
    app_url: str | None = None,
    config_url: str = "postgresql://config-user:secret@localhost:5432/configdb",
    config_section: dict[str, Any] | None = None,
    config_attributes: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    records: dict[str, Any] = {
        "file_config_calls": [],
        "configure_calls": [],
        "run_migrations_calls": 0,
        "begin_calls": 0,
        "engine_calls": [],
    }
    fake_config = FakeAlembicConfig(config_url, config_section, config_attributes)
    connectable = RecordingConnectable()

    if admin_url is None:
        monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_ADMIN_URL", admin_url)
    if app_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", app_url)

    monkeypatch.setattr("logging.config.fileConfig", lambda name, **kwargs: records["file_config_calls"].append((name, kwargs)))
    monkeypatch.setattr("alembic.context.config", fake_config, raising=False)
    monkeypatch.setattr("alembic.context.is_offline_mode", lambda: offline_mode, raising=False)
    monkeypatch.setattr(
        "alembic.context.configure",
        lambda **kwargs: records["configure_calls"].append(kwargs),
        raising=False,
    )

    @contextmanager
    def begin_transaction():
        records["begin_calls"] += 1
        yield

    monkeypatch.setattr("alembic.context.begin_transaction", begin_transaction, raising=False)
    monkeypatch.setattr(
        "alembic.context.run_migrations",
        lambda: records.__setitem__("run_migrations_calls", records["run_migrations_calls"] + 1),
        raising=False,
    )

    def fake_engine_from_config(configuration: dict[str, Any], **kwargs: Any) -> RecordingConnectable:
        records["engine_calls"].append((dict(configuration), kwargs))
        return connectable

    monkeypatch.setattr("sqlalchemy.engine_from_config", fake_engine_from_config)

    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    records["connectable"] = connectable
    return module, records


def test_normalize_sqlalchemy_url_rewrites_postgresql_scheme(monkeypatch) -> None:
    module, _records = load_env_module(monkeypatch, offline_mode=True)

    assert module.normalize_sqlalchemy_url("postgresql://user:pw@localhost/db") == (
        "postgresql+psycopg://user:pw@localhost/db"
    )
    assert module.normalize_sqlalchemy_url("sqlite:///tmp/test.db") == "sqlite:///tmp/test.db"


def test_get_url_prefers_explicit_config_attribute_over_env(monkeypatch) -> None:
    module, _records = load_env_module(
        monkeypatch,
        offline_mode=True,
        admin_url="postgresql://admin-user:secret@localhost:5432/admin_db",
        app_url="postgresql://app-user:secret@localhost:5432/app_db",
        config_attributes={
            "explicit_database_url": "postgresql://explicit:secret@localhost:5432/per_test_db"
        },
    )

    assert module.get_url() == "postgresql+psycopg://explicit:secret@localhost:5432/per_test_db"


def test_get_url_prefers_admin_env_then_database_env_then_config(monkeypatch) -> None:
    module, _records = load_env_module(
        monkeypatch,
        offline_mode=True,
        admin_url="postgresql://admin-user:secret@localhost:5432/admin_db",
        app_url="postgresql://app-user:secret@localhost:5432/app_db",
    )

    assert module.get_url() == "postgresql+psycopg://admin-user:secret@localhost:5432/admin_db"

    module, _records = load_env_module(
        monkeypatch,
        offline_mode=True,
        admin_url=None,
        app_url="postgresql://app-user:secret@localhost:5432/app_db",
    )

    assert module.get_url() == "postgresql+psycopg://app-user:secret@localhost:5432/app_db"

    module, _records = load_env_module(monkeypatch, offline_mode=True, admin_url=None, app_url=None)

    assert module.get_url() == "postgresql+psycopg://config-user:secret@localhost:5432/configdb"


def test_run_migrations_offline_configures_context_with_normalized_url(monkeypatch) -> None:
    """Mutation: let migration logging disable existing named loggers."""
    _module, records = load_env_module(
        monkeypatch,
        offline_mode=True,
        admin_url="postgresql://admin-user:secret@localhost:5432/admin_db",
    )

    assert records["file_config_calls"] == [("alembic.ini", {"disable_existing_loggers": False})]
    assert records["begin_calls"] == 1
    assert records["run_migrations_calls"] == 1
    assert records["configure_calls"] == [
        {
            "url": "postgresql+psycopg://admin-user:secret@localhost:5432/admin_db",
            "target_metadata": None,
            "literal_binds": True,
            "dialect_opts": {"paramstyle": "named"},
        }
    ]
    assert records["engine_calls"] == []


def test_run_migrations_online_builds_engine_configuration(monkeypatch) -> None:
    """Mutation: let migration logging disable existing named loggers."""
    _module, records = load_env_module(
        monkeypatch,
        offline_mode=False,
        app_url="postgresql://app-user:secret@localhost:5432/app_db",
        config_section={"sqlalchemy.echo": "false"},
    )

    configuration, engine_kwargs = records["engine_calls"][0]

    assert records["file_config_calls"] == [("alembic.ini", {"disable_existing_loggers": False})]
    assert configuration == {
        "sqlalchemy.echo": "false",
        "sqlalchemy.url": "postgresql+psycopg://app-user:secret@localhost:5432/app_db",
    }
    assert engine_kwargs["prefix"] == "sqlalchemy."
    assert engine_kwargs["poolclass"].__name__ == "NullPool"
    assert records["connectable"].connected is True
    assert records["configure_calls"] == [
        {"connection": records["connectable"].connection, "target_metadata": None}
    ]
    assert records["begin_calls"] == 1
    assert records["run_migrations_calls"] == 1
