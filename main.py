"""MobileWallet backend — thin launcher. The app lives in core/server.py."""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7332"))
    host = os.environ.get("HOST", "0.0.0.0")
    # RELOAD=1 enables dev hot-reload (restarts on .py changes). Reload mode
    # requires the app as an import string, not the object.
    reload = os.environ.get("RELOAD", "0") == "1"
    if reload:
        uvicorn.run("core.server:app", host=host, port=port, log_level="info", reload=True)
    else:
        from core.server import app

        uvicorn.run(app, host=host, port=port, log_level="info")
