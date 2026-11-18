"""Bind locally by default. Supply token through the environment, never source."""
import os
import uvicorn
from .app import create_app


def main():
    token = os.environ.get("AGENTLEAK_SERVER_TOKEN")
    app = create_app(os.environ.get("AGENTLEAK_DB", "results/server/episodes.sqlite3"), token)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
