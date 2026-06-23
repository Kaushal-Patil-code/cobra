import os

# Must be set before importing the app (settings are read at import time).
# Keep the test suite from triggering a real Fyers login in create_app().
os.environ.setdefault("FYERS_AUTOLOGIN_ON_STARTUP", "false")

import pytest

from app import create_app


@pytest.fixture()
def app():
    application = create_app()
    application.config.update(TESTING=True)
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()
