from __future__ import annotations

import uvicorn

from app.config import settings
from app.main import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
