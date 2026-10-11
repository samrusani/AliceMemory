"""Every HTTP request-model field that holds free JSON is held to the write limits, by its type or by its service.

A field typed ``dict[str, object]``, ``list[object]``, ``list[dict[str, object]]`` or ``object`` takes any JSON the body
limit lets through: up to 256 levels, and any number the decoder reads. The test walks every ``*Request`` model of
``alicebot_api.routers`` and lists the fields of that kind. Each one must either use one of the bounded types of
``alicebot_api.http_json_types`` (a 422 that names the field and the limit) or be named below with the service that holds
it to the same limits and answers its own 400, and the test that proves that service. A field added later that does
neither fails here, so a new door cannot store unbounded JSON by being new.

Mutations, each one alone: change one bounded field back to ``dict[str, object]`` (this test lists it as unbounded);
remove a name from ``HELD_BY_A_SERVICE`` (this test lists the field as unbounded); change a name there to a test that does
not exist (``test_every_service_held_field_names_a_test_that_exists`` fails).
"""

from __future__ import annotations

import collections.abc as abc
import importlib
import inspect
import pkgutil
import types
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

import alicebot_api.routers as routers_package
from alicebot_api.http_json_types import bounded_json

# field -> (the service that holds it, the test in test_write_doors_json_bounds.py or the integration twin that proves it)
HELD_BY_A_SERVICE = {
    "vnext_memories.VNextMemoryCommitRequest.source_refs": ("memory commit service", "test_the_commit_door_refuses_what_cannot_be_stored"),
    "vnext_memories.VNextMemoryProposalRequest.source_refs": ("proposal builder", "test_the_proposal_builder_refuses_what_cannot_be_stored"),
    "vnext_memories.VNextAgentOutputIngestRequest.source_refs": ("connector normalizer", "test_the_connector_normalizer_refuses_what_cannot_be_stored"),
    "vnext_memories.VNextConnectorSyncRequest.items": ("connector normalizer", "test_the_connector_normalizer_refuses_what_cannot_be_stored"),
    "vnext_memories.VNextTelegramSyncRequest.updates": ("connector normalizer", "test_the_connector_normalizer_refuses_what_cannot_be_stored"),
    "vnext_memories.VNextConnectorConfigRequest.config_json": ("connector service configuration", "test_the_connector_config_is_held_to_the_limits"),
    "continuity.ContinuityCaptureCommitRequest.candidates": ("capture commit service", "test_the_capture_and_correction_services_refuse_what_cannot_be_stored"),
    "continuity.ContinuityCorrectionRequest.body": ("correction service", "test_the_capture_and_correction_services_refuse_what_cannot_be_stored"),
    "continuity.ContinuityCorrectionRequest.provenance": ("correction service", "test_the_capture_and_correction_services_refuse_what_cannot_be_stored"),
    "continuity.ContinuityCorrectionRequest.replacement_body": ("correction service", "test_the_capture_and_correction_services_refuse_what_cannot_be_stored"),
    "continuity.ContinuityCorrectionRequest.replacement_provenance": ("correction service", "test_the_capture_and_correction_services_refuse_what_cannot_be_stored"),
}
SERVICE_TESTS_MODULES = ("tests.unit.test_write_doors_json_bounds", "tests.unit.test_capture_and_correction_json_bounds")


def _free(annotation: Any) -> bool:
    if annotation is object or annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _free(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return any(_free(arg) for arg in get_args(annotation))
    if origin in (list, tuple, set, frozenset, abc.Sequence):
        return any(_free(arg) for arg in get_args(annotation))
    if origin in (dict, abc.Mapping):
        args = get_args(annotation)
        return not args or (len(args) == 2 and _free(args[1]))
    return annotation in (dict, list)


def _annotation_is_bounded(annotation: Any) -> bool:
    """True when the annotation is, or is an optional of, a type that carries ``bounded_json``."""

    origin = get_origin(annotation)
    if origin is Annotated:
        return any(getattr(item, "func", None) is bounded_json for item in get_args(annotation)[1:])
    if origin in (Union, types.UnionType):
        arms = [arm for arm in get_args(annotation) if arm is not type(None)]
        return bool(arms) and all(_annotation_is_bounded(arm) for arm in arms)
    return False


def _uses_a_bounded_type(field) -> bool:
    # Pydantic lifts the metadata of a top-level ``Annotated`` onto the field and keeps a union's inside the annotation.
    return any(getattr(item, "func", None) is bounded_json for item in field.metadata) or _annotation_is_bounded(field.annotation)


def _request_models() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(routers_package.__path__):
        module = importlib.import_module(f"alicebot_api.routers.{info.name}")
        for name, value in vars(module).items():
            if inspect.isclass(value) and issubclass(value, BaseModel) and value.__module__ == module.__name__ and name.endswith("Request"):
                found[f"{info.name}.{name}"] = value
    return found


def _free_fields() -> dict[str, Any]:
    fields = {}
    for model_name, model in _request_models().items():
        for field_name, field in model.model_fields.items():
            if _free(field.annotation):
                fields[f"{model_name}.{field_name}"] = field
    return fields


def test_the_walk_finds_the_fields_it_should() -> None:
    free = _free_fields()
    assert len(_request_models()) > 100
    assert len(free) >= 50
    assert "vnext_memories.VNextMemoryCommitRequest.source_refs" in free
    assert "providers.RegisterProviderRequest.metadata" in free


def test_every_free_json_field_is_bounded_by_its_type_or_its_service() -> None:
    unbounded = sorted(
        name for name, field in _free_fields().items() if not _uses_a_bounded_type(field) and name not in HELD_BY_A_SERVICE
    )
    assert unbounded == [], (
        "these request fields hold free JSON and are held to no limit: give them a type from "
        "alicebot_api.http_json_types, or hold them in a service and list them in HELD_BY_A_SERVICE"
    )


def test_a_field_listed_as_held_by_a_service_does_not_use_a_bounded_type_twice() -> None:
    free = _free_fields()
    for name in HELD_BY_A_SERVICE:
        assert name in free, f"{name} is not a free JSON field of a request model"
        assert not _uses_a_bounded_type(free[name]), f"{name} uses a bounded type: take it off the list"


def test_every_service_held_field_names_a_test_that_exists() -> None:
    for name, (service, test_name) in HELD_BY_A_SERVICE.items():
        assert service
        holders = [importlib.import_module(path) for path in SERVICE_TESTS_MODULES if importlib.util.find_spec(path) is not None]
        assert any(hasattr(module, test_name) for module in holders), (name, test_name)


def test_the_bounded_types_do_not_change_the_published_schema() -> None:
    """The OpenAPI schema of a bounded field is the schema of the plain type: the validator adds no keyword."""

    import alicebot_api.main as main_module

    schemas = main_module.app.openapi()["components"]["schemas"]
    assert schemas["VNextContextPackRequest"]["properties"]["options"] == {
        "additionalProperties": True, "title": "Options", "type": "object"}
    assert schemas["RegisterProviderRequest"]["properties"]["metadata"] == {
        "additionalProperties": True, "title": "Metadata", "type": "object"}
    assert schemas["VNextBrainCharterUpsertRequest"]["properties"]["active_projects_json"] == {
        "items": {}, "title": "Active Projects Json", "type": "array"}
    assert schemas["AdmitMemoryRequest"]["properties"]["value"] == {"anyOf": [{}, {"type": "null"}], "title": "Value"}
