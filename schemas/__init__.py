"""Pydantic schemas — base models / scaffolding for request & response shaping.

Add concrete resource schemas here (subclass ApiModel) as real endpoints land.
"""
from .base import ApiModel, ErrorResponse

__all__ = ["ApiModel", "ErrorResponse"]
