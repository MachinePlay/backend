import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from starlette.middleware.sessions import SessionMiddleware

from app import books, db, streaming
from app.config import settings
from app.exceptions import AppException
from app.routes import router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    client = await db.connect()
    await books.seed()
    await streaming.abort_orphan_games()
    try:
        yield
    finally:
        logger.info("lifespan shutdown")
        await client.close()


# Readable generated client names: `getGame`, not `get_game_game__game_id__get`.
def _operation_id(route: APIRoute) -> str:
    return route.name


API_DESCRIPTION = """
Backend for **[MachinePlay](https://machineplay.org)**.

Users upload UCI chess engines as Docker images; runners pull them from the
registry and play games via
[fastchess](https://github.com/Disservin/fastchess), streamed live to the
browser over Server-Sent Events.
"""

# Each `name` matches a router's `tags=[...]` in `app/routes.py`.
TAGS_METADATA = [
    {
        "name": "Auth & account",
        "description": "GitHub OAuth login, the logged-in account, session and "
        "CLI tokens, and the Docker registry token endpoint.",
    },
    {
        "name": "Engines",
        "description": "The engine catalog: uploaded engines, their image "
        "versions, registration from the CLI, and deletion.",
    },
    {
        "name": "Profiles",
        "description": "Public user profiles.",
    },
    {
        "name": "Runners",
        "description": "The machines that play games: registered runners with "
        "their hardware and live online status.",
    },
    {
        "name": "Games",
        "description": "Start games and browse live or finished ones.",
    },
    {
        "name": "Tournaments",
        "description": "Round-robin and gauntlet tournaments: create one on a "
        "runner and watch its pairings play out with live standings.",
    },
    {
        "name": "Opening books",
        "description": "The opening lines games are dealt to start from, so "
        "two deterministic engines don't replay one game all tournament.",
    },
    {
        "name": "Live streaming",
        "description": "Server-Sent Events for live game updates and the runner "
        "WebSocket.",
    },
]

app = FastAPI(
    title="MachinePlay API",
    description=API_DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    generate_unique_id_function=_operation_id,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# `same_site="lax"` suffices: machineplay.org and api.machineplay.org share a
# registrable domain.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.cookie_secure,
)


@app.exception_handler(AppException)
async def app_error_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


app.include_router(router)
