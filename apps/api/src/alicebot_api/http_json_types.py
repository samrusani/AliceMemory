"""Request-model types for a field that holds caller JSON the product stores.

A field typed ``dict[str, object]`` or ``list[object]`` takes any JSON the body limit lets through, which is up to 256
levels of nesting and any number the decoder reads, ``NaN`` and ``Infinity`` included. These types add the write limits
of ``alicebot_api.json_bounds`` to the field: a value nested more than ``MAX_STORED_JSON_DEPTH`` levels, or one that
holds a number that is not finite, is refused with the HTTP 422 of any other validation error, naming the field and the
limit and never repeating the value. The OpenAPI schema of the field does not change.

A model that has a field of one of these shapes and does not use these types must have a service that holds the value to
the same limits and answers its own 400; ``tests/unit/test_http_json_fields_are_bounded.py`` lists both kinds and fails
for a field of neither.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

from alicebot_api.json_bounds import MAX_STORED_JSON_DEPTH, NOT_FINITE, TOO_DEEP, first_json_problem

TOO_DEEP_ERROR_TYPE = "json_too_deep"
NOT_FINITE_ERROR_TYPE = "finite_number"


def bounded_json(value: object) -> object:
    """Return ``value`` when it can be stored, else raise the validation error that names the limit."""

    problem = first_json_problem(value)
    if problem == TOO_DEEP:
        raise PydanticCustomError(
            TOO_DEEP_ERROR_TYPE,
            "Value must nest at most {limit} levels of arrays and objects",
            {"limit": MAX_STORED_JSON_DEPTH},
        )
    if problem == NOT_FINITE:
        raise PydanticCustomError(NOT_FINITE_ERROR_TYPE, "Input should be a finite number")
    return value


BoundedJsonObject = Annotated[dict[str, object], AfterValidator(bounded_json)]
BoundedJsonList = Annotated[list[object], AfterValidator(bounded_json)]
BoundedJsonObjectList = Annotated[list[dict[str, object]], AfterValidator(bounded_json)]
BoundedJsonValue = Annotated[object, AfterValidator(bounded_json)]

BOUNDED_TYPE_NAMES = ("BoundedJsonObject", "BoundedJsonList", "BoundedJsonObjectList", "BoundedJsonValue")

__all__ = [
    "BOUNDED_TYPE_NAMES",
    "BoundedJsonList",
    "BoundedJsonObject",
    "BoundedJsonObjectList",
    "BoundedJsonValue",
    "bounded_json",
]
