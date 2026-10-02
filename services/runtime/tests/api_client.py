"""Authenticated loopback client for runtime API tests."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella_runtime.api import get_runtime_session


def authorized_client(app: FastAPI) -> TestClient:
    return TestClient(
        app,
        base_url="http://127.0.0.1:8766",
        headers={"Authorization": f"Bearer {get_runtime_session().token}"},
    )
