from __future__ import annotations

import sys
from pathlib import Path

_WORKSPACE_CACHE = Path(__file__).resolve().parent.parent / "cache"
if _WORKSPACE_CACHE.exists():
    _PYCACHE = _WORKSPACE_CACHE / "pycache"
    _PYCACHE.mkdir(parents=True, exist_ok=True)
    sys.pycache_prefix = str(_PYCACHE)

import uvicorn

from app.config import Config
from app.main import create_app


def main() -> None:
    config = Config.load()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
