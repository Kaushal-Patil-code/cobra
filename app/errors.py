"""JSON error handlers — every error leaves as the ErrorResponse envelope."""
from __future__ import annotations

import json
import logging

from flask import Flask, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ValidationError)
    def handle_validation_error(exc: ValidationError):
        # 422: well-formed request, semantically invalid body. A custom validator
        # raising ValueError lands the exception object in each error's `ctx`,
        # which isn't JSON-serializable — round-trip through default=str to make
        # the whole detail safe for jsonify.
        detail = json.loads(json.dumps(exc.errors(include_url=False), default=str))
        return jsonify(error="validation_error", detail=detail), 422

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        name = (exc.name or "error").lower().replace(" ", "_")
        return jsonify(error=name, detail=exc.description), exc.code or 500

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        logger.exception("Unhandled error")
        return jsonify(error="internal_server_error"), 500
