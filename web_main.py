import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import uvicorn

import config
from web.server_live import app


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.WEB_PORT, log_level="info")
