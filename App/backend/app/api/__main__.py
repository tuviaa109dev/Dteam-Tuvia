"""Entry point: `python -m app.api`. Runs uvicorn with our JSON logging instead of its own."""

import os

import uvicorn

from app.config import settings
from app.logging_config import configure_logging

if __name__ == "__main__":
    configure_logging(settings.log_level)
    uvicorn.run(
        "app.api.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=int(os.getenv("API_WORKERS", "1")),
        log_config=None,
    )
