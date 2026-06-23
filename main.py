"""Development entrypoint.

    uv run main.py          # or: python main.py

For production use a WSGI server (multiple workers):

    gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app
"""
from app import create_app
from config.settings import settings

app = create_app()


def main() -> None:
    app.run(host=settings.host, port=settings.port, debug=settings.debug)


if __name__ == "__main__":
    main()
