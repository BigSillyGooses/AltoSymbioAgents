"""
backend/server.py — FastAPI sidecar entrypoint.

Lifecycle (matches the contract Electron's SidecarManager expects):
1. Parse `--token <uuid>` (required) and `--user-data <path>` (optional).
2. Bind a uvicorn server on 127.0.0.1 with port=0 (OS-assigned).
3. Print `PORT=<n>` to stdout and flush, so Electron can read it line-by-line.
4. Mount routers, then print `READY` and flush.
5. Serve until POST /shutdown is hit (or the parent kills us).

All routes (except GET /health with no token) require a Bearer token.
SSE endpoints accept the token via ?token= query string since EventSource
doesn't support custom headers.

Network: binds 127.0.0.1 only — never 0.0.0.0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Make `import core`, `import services`, `import db`, etc. resolve from this dir
# whether we run as `python backend/server.py` or as a frozen exe.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import sse_events  # noqa: E402
from core import paths  # noqa: E402
from core.errors import install_error_handlers  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.settings import Settings  # noqa: E402
from core.api import API  # noqa: E402
import db as _db_module  # noqa: E402

log = logging.getLogger("sidecar")


# ── Auth middleware ──────────────────────────────────────────────────────────

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request that doesn't carry the right Bearer token.

    /health is intentionally token-optional so Electron's pre-ready poller can
    detect liveness before it has been told the token. Every other route must
    present ``Authorization: Bearer <token>``. Electron's main process injects
    that header for the renderer via a webRequest hook, so EventSource works
    without a query-string token (which would otherwise leak into history,
    referers, and access logs).
    """

    def __init__(self, app, *, expected_token: str) -> None:
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/health":
            return await call_next(request)

        supplied = ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()

        # Fallback for EventSource (which can't set custom headers): allow
        # ``?token=…`` on /api/events so opening the stream from a context
        # without the Electron webRequest hook (e.g. devtools, a curl probe)
        # still authenticates. The token is the same per-process secret —
        # only loopback callers can read it from the parent process — so
        # this neither widens the trust surface nor leaks across origins.
        if not supplied and path == "/api/events":
            supplied = request.query_params.get("token", "").strip()

        if not supplied or not secrets.compare_digest(supplied, self._expected):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        return await call_next(request)


# ── App container ────────────────────────────────────────────────────────────

class _AppContainer:
    """Holds the shared services that route handlers reach into."""

    def __init__(self, user_data: Path | None) -> None:
        # Honor --user-data so Electron's userData dir wins over platformdirs
        # when bundled inside a packaged installer. paths.user_dir() reads
        # MYAI_USER_DATA on every call and falls back to platformdirs when it
        # is unset (standalone runs, pytest).
        if user_data is not None:
            user_data.mkdir(parents=True, exist_ok=True)
            os.environ[paths.USER_DATA_ENV] = str(user_data)

        # Migrations run before logging is configured so the log FileHandler
        # opens against the post-migration path. Order matters:
        #   1. migrate_v5_user_dir   — historic app-name rename (no-op today).
        #   2. migrate_to_unified_userdata — moves data from the platformdirs
        #      path into the Electron userData dir when they differ.
        #   3. migrate_legacy_install — sweeps any files left next to the
        #      installer executable into user_dir.
        paths.migrate_v5_user_dir()
        paths.migrate_to_unified_userdata()
        user_dir = paths.user_dir()
        paths.migrate_legacy_install(_HERE, user_dir)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler(paths.log_path(), encoding="utf-8"),
            ],
        )

        self.settings = Settings(paths.settings_path())
        self.bus = EventBus()
        self.api = API(self.settings, self.bus, _HERE, log)

        # Match the legacy main.py post-load behavior: kick off heavy services
        # in a background thread so /health responds quickly.
        self.api.start_deferred_init()

        # Phase 9: rebound the bundled llama-server if the user previously
        # picked bundled mode. Runs on a daemon thread so a missing/corrupt
        # model file can't block sidecar boot — the wizard surfaces errors.
        try:
            threading.Thread(
                target=self.api.maybe_autostart_bundled_server,
                daemon=True,
                name="bundled-autostart",
            ).start()
        except Exception as exc:
            log.warning("bundled-autostart spawn failed: %s", exc, exc_info=True)

    def shutdown(self) -> None:
        try:
            self.api.shutdown()
        except Exception as exc:
            log.warning("api.shutdown raised: %s", exc, exc_info=True)


# ── FastAPI factory ──────────────────────────────────────────────────────────

# Single source of truth for app metadata. Surfaced as module constants so the
# OpenAPI → TS codegen (build-scripts/generate_api_types.py) emits a schema
# whose info block matches the running sidecar without re-stating the literals.
OPENAPI_TITLE = "altosybioagents Sidecar"
OPENAPI_VERSION = "1.0.0"

# (dotted_module_under_backend, include_router_prefix). Order matches the
# original explicit include_router() sequence so route enumeration in
# /openapi.json stays byte-stable. The codegen script imports this list and
# REGISTER_ROUTERS so the schema it dumps is the same one the live sidecar
# serves.
ROUTER_SPECS: tuple[tuple[str, str], ...] = (
    ("routes.health", ""),
    ("routes.echo", "/api"),
    ("routes.events", "/api"),
    ("routes.chat", "/api/chat"),
    ("routes.conversations", "/api/conversations"),
    ("routes.attachments", "/api"),
    ("routes.agents", "/api/agents"),
    ("routes.memory", "/api/memory"),
    ("routes.rag", "/api/rag"),
    ("routes.models", "/api/models"),
    ("routes.settings", "/api/settings"),
    ("routes.mcp", "/api/mcp"),
    ("routes.lifecycle", "/api/lifecycle"),
    ("routes.escalation", "/api/escalation"),
    ("routes.prompts", "/api/prompts"),
    ("routes.prompt_templates", "/api/prompt-templates"),
    ("routes.safety", "/api/safety"),
    ("routes.system", "/api/system"),
    ("routes.usage", "/api/usage"),
    ("routes.voice", "/api/voice"),
)


def register_routers(app: FastAPI) -> None:
    """Wire every backend router onto ``app``. Single source of truth shared by
    build_app() (the runtime path) and build-scripts/generate_api_types.py
    (the OpenAPI → TS codegen). Add a router by appending to ROUTER_SPECS."""
    import importlib
    for dotted, prefix in ROUTER_SPECS:
        module = importlib.import_module(dotted)
        if prefix:
            app.include_router(module.router, prefix=prefix)
        else:
            app.include_router(module.router)


def build_app(token: str, user_data: Path | None) -> tuple[FastAPI, _AppContainer]:
    container = _AppContainer(user_data)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        sse_events.attach_loop(asyncio.get_running_loop())
        # Sweep workflow_checkpoints rows that were 'provisional' when the
        # previous sidecar exited. Without this, every restart leaves the
        # debate/saga UI showing in-flight work that nobody is processing.
        try:
            from services.pipeline import mark_abandoned_provisional_checkpoints
            n = mark_abandoned_provisional_checkpoints()
            if n:
                log.info("workflow_checkpoints: marked %d orphaned provisional rows as abandoned", n)
        except Exception as exc:
            log.warning("workflow_checkpoints abandon-sweep skipped: %s", exc)
        yield

    app = FastAPI(
        title=OPENAPI_TITLE, version=OPENAPI_VERSION, lifespan=_lifespan,
    )

    # CORS: Electron's renderer runs on file:// or http://localhost in dev.
    # Allow only localhost origins; the Bearer middleware is the real gate.
    app.add_middleware(
        CORSMiddleware,
        # Dev-server origins + the packaged-build ``null`` origin.
        # Packaged Electron loads the renderer from ``file://``
        # (see desktop-shell/main.ts), whose Origin header is the string
        # ``"null"`` (opaque origin). Chromium enforces CORS even for
        # loopback when webSecurity:true, so the preflight OPTIONS and the
        # subsequent request both require ``Access-Control-Allow-Origin: null``
        # in the response. BearerAuthMiddleware remains the real auth gate.
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
            "null",  # file:// renderer origin in packaged Electron builds
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_middleware(BearerAuthMiddleware, expected_token=token)

    # Stash the container on the app so route handlers can reach it via
    # request.app.state.container.
    app.state.container = container
    app.state.shutdown_event = asyncio.Event()

    # Typed error envelopes (Stage-2 #11). Routes raise DomainError instead
    # of HTTPException(...) so the renderer can pattern-match on
    # ``error_type`` instead of parsing strings. The HTTPException branch
    # of the handler wraps unmigrated raises into the same envelope shape.
    install_error_handlers(app)

    # Register routers (single source of truth: ROUTER_SPECS above).
    register_routers(app)

    @app.post("/shutdown")
    async def _shutdown(request: Request) -> dict:
        container.shutdown()
        request.app.state.shutdown_event.set()
        return {"ok": True}

    return app, container


# ── Port discovery ───────────────────────────────────────────────────────────

def _bind_free_port() -> tuple[socket.socket, int]:
    """Bind a TCP socket on 127.0.0.1:0 and return (socket, assigned_port).

    Uvicorn 0.30+ accepts an `fd=` kwarg so we can hand it the bound socket
    directly, sidestepping the race where another process grabs the port
    between us calling getsockname() and uvicorn calling bind().
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    return sock, port


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server")
    parser.add_argument("--token", required=True, help="Bearer auth token")
    parser.add_argument("--user-data", default="", help="Override userData dir")
    args = parser.parse_args(argv)

    user_data = Path(args.user_data) if args.user_data else None

    app, container = build_app(args.token, user_data)

    sock, port = _bind_free_port()

    # Print the line Electron's stdout reader is grepping for. Flush
    # immediately — the parent uses line-by-line iteration and a buffered
    # write would deadlock the handshake.
    print(f"PORT={port}", flush=True)

    # Hand uvicorn the already-bound socket directly (via the sockets= kwarg
    # on serve(), NOT via Config(fd=…)) so we never release the port between
    # getsockname() and serve(). Closing + rebinding (the old behavior) had
    # a brief TOCTOU window where another local process could claim the
    # port; advertising a port we no longer hold then made Electron poll a
    # stranger's /health.
    #
    # Cross-platform note: uvicorn's `Config(fd=…)` path is POSIX-only
    # (marked `# pragma: py-win32` in uvicorn/server.py — it calls
    # `socket.fromfd(fd, socket.AF_UNIX, …)` which AttributeErrors on
    # Windows because socket.AF_UNIX isn't exposed). Passing the bound
    # socket via `serve(sockets=[sock])` takes the other branch in
    # uvicorn.Server._serve(), which works on both POSIX and Windows and
    # still preserves the TOCTOU guard (sock stays bound the whole time).
    config = uvicorn.Config(
        app,
        log_level="info",
        access_log=False,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        # No reload — Electron's electron-vite handles HMR for the renderer;
        # the sidecar is restarted explicitly when the user clicks "Restart
        # Backend" or by sending POST /shutdown then respawning.
        reload=False,
    )
    server = uvicorn.Server(config)

    async def _run() -> None:
        # Print READY *after* startup events have fired (CORS + auth middleware
        # registered, services warmed up to first-paint readiness).
        async def _emit_ready() -> None:
            # `serve()` blocks until shutdown; this watcher races with the
            # built-in startup event so we only print when the app is truly
            # serving.
            while not server.started:
                await asyncio.sleep(0.05)
            print("READY", flush=True)

        ready_task = asyncio.create_task(_emit_ready())
        shutdown_task = asyncio.create_task(app.state.shutdown_event.wait())

        serve_task = asyncio.create_task(server.serve(sockets=[sock]))

        # If POST /shutdown fires, ask uvicorn to exit cleanly. If uvicorn
        # exits on its own (parent killed us), we just fall through.
        done, _ = await asyncio.wait(
            {serve_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            server.should_exit = True
            await serve_task
        ready_task.cancel()

    # Graceful SIGTERM: uvicorn already handles SIGINT on POSIX, but on
    # Windows the parent uses taskkill /T which sends a CTRL_BREAK — install
    # a no-op handler so the default abort path is replaced with our cleanup.
    def _sig(_signum, _frame):
        try:
            container.shutdown()
        finally:
            os._exit(0)

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _sig)
        except (OSError, ValueError):
            pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        container.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
