"""Process-scoped bearer credential shared with the trusted Tauri shell."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from ella_runtime.storage_paths import default_data_dir


class RuntimeSession:
    def __init__(self, path: Path | None = None, *, token: str | None = None) -> None:
        self.path = path or default_data_dir() / "runtime.session"
        self.token = token or secrets.token_urlsafe(32)

    def publish(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(self.token)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if self.path.read_text(encoding="ascii") == self.token:
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass

    def accepts_bearer(self, authorization: str | None) -> bool:
        if authorization is None or not authorization.startswith("Bearer "):
            return False
        return secrets.compare_digest(authorization[7:], self.token)

    def accepts_websocket_protocols(self, protocols: list[str]) -> bool:
        return "ella-auth" in protocols and any(
            secrets.compare_digest(value, self.token) for value in protocols
        )


def loopback_host_allowed(host_header: str | None, *, port: int) -> bool:
    if not host_header or "@" in host_header:
        return False
    try:
        parsed = urlsplit(f"//{host_header}")
        return (
            parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port in {None, port}
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False
