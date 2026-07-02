import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from starlette.middleware.sessions import SessionMiddleware

from app import db, streaming
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
    await streaming.abort_orphan_games()
    try:
        yield
    finally:
        logger.info("lifespan shutdown")
        await client.close()


# Use the route's function name as the OpenAPI operationId so generated
# clients get readable method names (e.g. `getGame` instead of
# `get_game_game__game_id__get`).
def _operation_id(route: APIRoute) -> str:
    return route.name


API_DESCRIPTION = """
Backend for **[MachinePlay](https://machineplay.org)**.

Users upload UCI chess engines as Docker images; runners pull them from a
private registry and play games via
[fastchess](https://github.com/Disservin/fastchess), streamed live to the
browser over Server-Sent Events.
"""

# Groups the routes into named sections in the Swagger UI; each `name` matches
# the `tags=[...]` on the corresponding router in `app/routes.py`.
TAGS_METADATA = [
    {
        "name": "Auth & account",
        "description": "GitHub OAuth login, the logged-in account, session and "
        "CLI tokens, and the Docker registry token endpoint.",
    },
    {
        "name": "Engines & profiles",
        "description": "Public user profiles and the engines they've uploaded.",
    },
    {
        "name": "Games & runners",
        "description": "Start games, browse results, and see the connected "
        "runners that play them.",
    },
    {
        "name": "Tournaments",
        "description": "Round-robin and gauntlet tournaments: create one on a "
        "runner and watch its pairings play out with live standings.",
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

# Signs the session cookie that holds the logged-in user id. `same_site="lax"`
# is enough because the frontend (machineplay.org) and API (api.machineplay.org)
# share a registrable domain; flip `cookie_secure` on in production for HTTPS.
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
