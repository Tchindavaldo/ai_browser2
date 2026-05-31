"""MobileWallet backend — thin launcher. The app lives in core/server.py."""

import os

import uvicorn

from core.server import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7332"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")
