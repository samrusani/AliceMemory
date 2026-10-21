"""The package reads FastAPI and Starlette through their public modules, and no route declares a parameter the filter cannot read.

The layer that withholds the ids of rows a key may not read learns what a request supplied from the route that matched it.
It reads the public fields of the route's ``Dependant`` (``query_params``, ``body_params``, ``dependencies``). It once imported
``get_flat_dependant`` from ``fastapi.dependencies.utils``, which a later release of FastAPI no longer has, and every request
to a route that returned an id answered HTTP 500 under that release. The framework is not pinned, so the package may import
from a public module only.

Mutations, each one alone: put ``from fastapi.dependencies.utils import get_flat_dependant`` back in
``vnext_hidden_ids.py``, or import anything from a module of FastAPI or Starlette with a leading underscore:
``test_the_package_imports_nothing_private_from_the_frameworks``. Declare a pydantic model as a query parameter of a route:
``test_no_route_declares_a_model_as_a_query_header_or_cookie_parameter``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import typing
from pathlib import Path

import pytest
from fastapi import Depends, Header, Query
from pydantic import BaseModel

SOURCE = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "alicebot_api"
FRAMEWORKS = ("fastapi", "starlette")
# Modules of the framework that are its machinery and not its interface, apart from the ones whose name starts with an underscore.
MACHINERY_MODULES = ("fastapi.dependencies",)
# Names of that machinery, wherever they are imported from.
MACHINERY_NAMES = frozenset(
    {
        "get_dependant",
        "get_flat_dependant",
        "get_flat_params",
        "get_body_field",
        "get_typed_signature",
        "get_param_field",
        "analyze_param",
        "solve_dependencies",
        "request_params_to_args",
        "request_body_to_args",
    }
)


def _private_use(tree: ast.AST) -> list[tuple[int, str]]:
    """Every import in ``tree``, at any depth, of a private module or a private name of FastAPI or Starlette."""

    found: list[tuple[int, str]] = []

    def check_module(line: int, module: str) -> None:
        parts = module.split(".")
        if parts[0] not in FRAMEWORKS:
            return
        if any(part.startswith("_") for part in parts[1:]) or module.startswith(MACHINERY_MODULES):
            found.append((line, module))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            check_module(node.lineno, node.module)
            if node.module.split(".")[0] in FRAMEWORKS:
                for alias in node.names:
                    if alias.name.startswith("_") or alias.name in MACHINERY_NAMES:
                        found.append((node.lineno, f"{node.module}.{alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                check_module(node.lineno, alias.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # ``importlib.import_module("fastapi.dependencies.utils")`` is the same import.
            if node.value.split(".")[0] in FRAMEWORKS and "." in node.value:
                check_module(node.lineno, node.value)
    return found


def test_the_package_imports_nothing_private_from_the_frameworks() -> None:
    violations = []
    files = sorted(SOURCE.rglob("*.py"))
    assert len(files) > 100, "the package was not found"
    for path in files:
        for line, name in _private_use(ast.parse(path.read_text(encoding="utf-8"))):
            violations.append(f"{path.relative_to(SOURCE)}:{line} {name}")
    assert not violations, violations


@pytest.mark.parametrize(
    "source",
    [
        "from fastapi.dependencies.utils import get_flat_dependant",
        "from fastapi.dependencies import utils",
        "def f():\n    from fastapi.dependencies.utils import get_flat_dependant",
        "from fastapi import _compat",
        "from fastapi._compat import ModelField",
        "from fastapi._compat.v2 import ModelField",
        "from starlette._utils import is_async_callable",
        "import fastapi.dependencies.utils",
        "from fastapi.routing import get_dependant",
        "import importlib\nimportlib.import_module('fastapi.dependencies.utils')",
    ],
)
def test_the_scan_finds_a_private_import_in_any_of_its_spellings(source: str) -> None:
    assert _private_use(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "from fastapi import FastAPI, Query",
        "from fastapi.responses import JSONResponse",
        "from fastapi.encoders import jsonable_encoder",
        "from fastapi.exceptions import RequestValidationError",
        "from fastapi.openapi.utils import get_openapi",
        "from starlette.requests import Request",
        "x = 'fastapi'",
    ],
)
def test_the_scan_leaves_the_public_modules_alone(source: str) -> None:
    assert not _private_use(ast.parse(source))


def _model_parameters(call: typing.Callable[..., object], seen: set[int] | None = None) -> list[str]:
    """Names of the parameters of ``call`` (and of the dependencies it declares) that are models read from the query,
    the headers or the cookies."""

    seen = seen if seen is not None else set()
    if id(call) in seen:
        return []
    seen.add(id(call))
    found: list[str] = []
    for name, parameter in inspect.signature(call, eval_str=True).parameters.items():
        annotation = parameter.annotation
        markers = [parameter.default]
        if typing.get_origin(annotation) is typing.Annotated:
            annotation, *extra = typing.get_args(annotation)
            markers += extra
        for marker in markers:
            kind = type(marker).__name__
            if not type(marker).__module__.startswith("fastapi"):
                continue
            if kind == "Depends" and callable(getattr(marker, "dependency", None)):
                found += [f"{name}.{inner}" for inner in _model_parameters(marker.dependency, seen)]
            elif (
                kind in {"Query", "Header", "Cookie"}
                and isinstance(annotation, type)
                and issubclass(annotation, BaseModel)
            ):
                found.append(name)
    return found


class Filters(BaseModel):
    artifact_id: str | None = None


def _with_query_model(filters: typing.Annotated[Filters, Query()]) -> None: ...


def _with_header_model(filters: Filters = Header()) -> None: ...  # noqa: B008


def _with_scalars(artifact_id: str | None = None, limit: int = 10, body: Filters | None = None) -> None: ...


def _through_a_dependency(shared: object = Depends(_with_query_model)) -> None: ...  # noqa: B008


def test_the_check_for_a_model_parameter_finds_one_where_it_is_declared() -> None:
    assert _model_parameters(_with_query_model) == ["filters"]
    assert _model_parameters(_with_header_model) == ["filters"]
    assert _model_parameters(_with_scalars) == []
    assert _model_parameters(_through_a_dependency) == ["shared.filters"]


def _every_route() -> list[typing.Any]:
    """The routes of the application: those the routers hold and those the application declares itself."""

    import alicebot_api.main as main_module
    import alicebot_api.routers as routers_package
    from fastapi import APIRouter

    routes: dict[int, typing.Any] = {}
    containers: list[object] = [main_module]
    for info in pkgutil.iter_modules(routers_package.__path__, f"{routers_package.__name__}."):
        containers.append(importlib.import_module(info.name))
    for module in containers:
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                for route in value.routes:
                    if callable(getattr(route, "endpoint", None)):
                        routes[id(route)] = route
    for route in main_module.app.router.routes:
        if callable(getattr(route, "endpoint", None)):
            routes[id(route)] = route
    return list(routes.values())


def test_no_route_declares_a_model_as_a_query_header_or_cookie_parameter() -> None:
    """The filter lets an id stay where the answer repeats the query parameter that held it. A route that read its query
    into a model would name the model and not the parameters, so the filter would find no input for it. No route does, and
    one that does has to teach ``echo_of_request`` first.
    """

    routes = _every_route()
    assert len(routes) > 150, len(routes)
    declared = []
    for route in routes:
        names = _model_parameters(route.endpoint)
        if names:
            declared.append(f"{route.path} {route.endpoint.__name__}: {names}")
    assert not declared, sorted(declared)
