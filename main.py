"""Railway-compatible ASGI entry point.

This wrapper lets Railway start the app from the repository root with:
    uvicorn main:app
"""
from app.main import app

__all__ = ["app"]
