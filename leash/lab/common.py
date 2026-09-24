"""Shared bits of the lab pages: each part is its own small app (`leash lab <part>`), on its own port."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"
PORTS = {"permanent": 8101, "mandate": 8102, "apply": 8103, "shop-text": 8104, "respond": 8105}
MODULES = {"permanent": "permanent", "mandate": "mandate", "apply": "apply", "shop-text": "shoptext", "respond": "respond"}


def make_app(title: str, page: str) -> FastAPI:
    app = FastAPI(title=title)

    @app.get("/")
    def index():
        return FileResponse(STATIC / page)

    @app.get("/lab.css")
    def css():
        return FileResponse(STATIC / "lab.css", media_type="text/css")

    @app.get("/lab.js")
    def js():
        return FileResponse(STATIC / "lab.js", media_type="text/javascript")

    return app
