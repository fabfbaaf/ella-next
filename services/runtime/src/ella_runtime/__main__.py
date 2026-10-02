"""Run the local runtime in development."""

import os

import uvicorn


def main() -> None:
    host = os.getenv("ELLA_RUNTIME_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("艾拉运行时只允许绑定本机回环地址")
    port = int(os.getenv("ELLA_RUNTIME_PORT", "8766"))
    from ella_runtime.api import app
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, reload=False,
        ws_max_size=256 * 1024, timeout_graceful_shutdown=3,
    ))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
