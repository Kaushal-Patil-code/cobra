"""Request-body validation helper bridging Flask and Pydantic."""
from __future__ import annotations

from typing import Type, TypeVar

from flask import request
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def parse_body(model: Type[T]) -> T:
    """Validate the JSON request body against `model` and return the instance.

    Raises pydantic.ValidationError on invalid input, which the registered
    error handler turns into a 422 response.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    return model.model_validate(payload)
