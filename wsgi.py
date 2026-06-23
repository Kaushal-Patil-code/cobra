"""Production WSGI entrypoint.

    gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app

Do NOT pass --preload: each worker must open its own DB pool after fork.
"""
from app import create_app

app = create_app()
