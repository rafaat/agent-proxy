"""`python -m gateway` entry point."""

import uvicorn

from gateway.config import get_settings
from gateway.server import create_app


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
