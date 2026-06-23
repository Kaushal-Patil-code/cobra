"""Base Pydantic models shared by all API schemas (pydantic v2)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    """Base for every request/response schema.

    - from_attributes: build straight from DB rows / ORM objects.
    - str_strip_whitespace: trim incoming strings.
    - extra="ignore": drop unknown keys instead of erroring (forward-compatible
      with extra DB columns and clients sending stray fields).
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="ignore",
    )


class ErrorResponse(ApiModel):
    """Canonical JSON error envelope returned by the error handlers."""

    error: str
    detail: Optional[Any] = None
