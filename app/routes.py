"""The API surface: one thin router.

Handlers only translate HTTP (params, session, redirects) to and from the
domain modules — auth, engines, games, users, registry, streaming. Logic
lives there, not here.
"""

from collections.abc import AsyncIterable
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.sse import EventSourceResponse

from machineplay import schemas

from app import auth, engines, games, registry, streaming, users
from app.models import ApiToken, Game, User
from app.schemas import (
    ApiTokenOut,
    EngineDetailOut,
    EngineOut,
    EngineRegisterRequest,
    EngineRegisterResponse,
    GameOut,
    LiveStreamEvent,
    PendingSignupOut,
    RegisterRequest,
    RegistryTokenOut,
    RunnerOut,
    StartGameRequest,
    StartGameResponse,
    TokenOut,
    UserOut,
    UserProfileOut,
)

router = APIRouter()


# --- auth & account ----------------------------------------------------------


@router.get("/auth/github/login")
async def github_login(request: Request) -> RedirectResponse:
    return RedirectResponse(auth.begin_github_login(request.session))


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str, state: str) -> RedirectResponse:
    return RedirectResponse(
        await auth.complete_github_login(request.session, code, state)
    )


@router.get("/auth/pending", response_model=PendingSignupOut)
async def pending_signup(request: Request) -> PendingSignupOut:
    """The GitHub profile waiting on a handle, for the /register page."""
    return auth.pending_signup(request.session)


@router.post("/auth/register", response_model=UserOut)
async def register(request: Request, payload: RegisterRequest) -> User:
    """Complete a pending GitHub signup with the chosen handle."""
    return await auth.register(request.session, payload.login)


@router.post("/auth/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"success": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(auth.require_user)) -> User:
    return user


@router.post("/me/tokens", response_model=TokenOut)
async def create_token(user: User = Depends(auth.require_user)) -> TokenOut:
    """Mint a CLI API token for the logged-in user (shown once)."""
    return TokenOut(token=await auth.mint_token(user))


@router.get("/me/tokens", response_model=list[ApiTokenOut])
async def list_tokens(user: User = Depends(auth.require_user)) -> list[ApiToken]:
    """The logged-in user's API tokens (prefix + timestamps, never plaintext)."""
    return await auth.list_tokens(user)


@router.delete("/me/tokens/{token_id}")
async def revoke_token(
    token_id: UUID, user: User = Depends(auth.require_user)
) -> dict[str, bool]:
    await auth.revoke_token(user, token_id)
    return {"success": True}


@router.get("/registry/token", response_model=RegistryTokenOut)
async def registry_token(request: Request) -> RegistryTokenOut:
    """Docker Registry v2 token endpoint (the registry's `auth.token.realm`).

    Authenticates the ``mp_`` token sent as the HTTP Basic password (written by
    ``machineplay login``) and mints a signed JWT granting the requested scopes.
    """
    user = await auth.user_from_basic(request)
    return registry.issue_token(user, request.query_params.getlist("scope"))


# --- engines & profiles -------------------------------------------------------


@router.get("/engine", response_model=list[EngineOut])
async def list_engines() -> list[EngineOut]:
    return await engines.list_engines()


@router.post("/engine/register", response_model=EngineRegisterResponse)
async def register_engine(
    payload: EngineRegisterRequest,
    user: User = Depends(auth.require_token_user),
) -> EngineRegisterResponse:
    """Record an engine version after the CLI pushed its image to the registry."""
    return await engines.register_version(user, payload)


@router.get("/u/{login}", response_model=UserProfileOut)
async def user_profile(login: str) -> UserProfileOut:
    """Public profile: the user, their engines, and those engines' games."""
    return await users.profile(login)


@router.get("/u/{login}/{engine_name}", response_model=EngineDetailOut)
async def get_engine_by_name(login: str, engine_name: str) -> EngineDetailOut:
    """Engine detail addressed GitHub-style: owner handle + engine name."""
    owner = await users.by_login(login)
    return await engines.detail(await engines.by_name(owner, engine_name))


# --- games & runners ----------------------------------------------------------


@router.get("/runners", response_model=list[RunnerOut])
async def list_runners() -> list[streaming.Runner]:
    return streaming.runners.list_runners()


@router.post("/game")
async def start_game(
    payload: StartGameRequest, user: User = Depends(auth.require_user)
) -> StartGameResponse:
    return await games.start_game(user, payload)


@router.get("/game", response_model=list[GameOut])
async def list_games(limit: int = Query(default=50, ge=1, le=200)) -> list[Game]:
    return await games.list_games(limit)


@router.get("/game/{game_id}", response_model=GameOut)
async def get_game(game_id: UUID) -> Game:
    return await games.get_game(game_id)


# --- live streaming -----------------------------------------------------------


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await streaming.runner_session(ws)


@router.get(
    "/stream/game/{game_id}",
    response_class=EventSourceResponse,
    # SSE responses are opaque to FastAPI's auto-schema; declaring the
    # per-message payload here makes the event type appear in OpenAPI so
    # the generated TS client can reference it.
    responses={200: {"model": schemas.GameStreamEvent}},
)
async def sse_stream(game_id: UUID) -> AsyncIterable[schemas.GameStreamEvent]:
    async for event in streaming.game_event_stream(game_id):
        yield event


@router.get(
    "/stream/live",
    response_class=EventSourceResponse,
    responses={200: {"model": LiveStreamEvent}},
)
async def sse_live_stream() -> AsyncIterable[LiveStreamEvent]:
    async for event in streaming.live_event_stream():
        yield event
