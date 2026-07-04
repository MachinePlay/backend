"""The API surface: thin routers grouped by concern.

Handlers only translate HTTP (params, session, redirects) to and from the
domain modules — auth, engines, games, users, registry, streaming. Logic
lives there, not here.

Each router carries a `tags=[...]` label that Swagger uses to group the
endpoints; the matching descriptions live in `TAGS_METADATA` in `app/main.py`.
The sub-routers are mounted onto `router`, which `app.main` includes.
"""

from collections.abc import AsyncIterable
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.sse import EventSourceResponse

from machineplay import schemas

from app import auth, engines, games, registry, runners, streaming, tournaments, users
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
    RunnerLiveEvent,
    RunnerOut,
    RunnerUpdateRequest,
    StartGameRequest,
    StartGameResponse,
    TokenOut,
    TournamentCreateRequest,
    TournamentDetailOut,
    TournamentOut,
    UserOut,
    UserProfileOut,
)

router = APIRouter()


# --- auth & account ----------------------------------------------------------

auth_router = APIRouter(tags=["Auth & account"])


@auth_router.get("/auth/github/login")
async def github_login(request: Request) -> RedirectResponse:
    return RedirectResponse(auth.begin_github_login(request.session))


@auth_router.get("/auth/github/callback")
async def github_callback(request: Request, code: str, state: str) -> RedirectResponse:
    return RedirectResponse(
        await auth.complete_github_login(request.session, code, state)
    )


@auth_router.get("/auth/pending", response_model=PendingSignupOut)
async def pending_signup(request: Request) -> PendingSignupOut:
    """The GitHub profile waiting on a handle, for the /register page."""
    return auth.pending_signup(request.session)


@auth_router.post("/auth/register", response_model=UserOut)
async def register(request: Request, payload: RegisterRequest) -> User:
    """Complete a pending GitHub signup with the chosen handle."""
    return await auth.register(request.session, payload.login)


@auth_router.post("/auth/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"success": True}


@auth_router.get("/me", response_model=UserOut)
async def me(user: User = Depends(auth.require_user)) -> User:
    return user


@auth_router.post("/me/tokens", response_model=TokenOut)
async def create_token(user: User = Depends(auth.require_user)) -> TokenOut:
    """Mint a CLI API token for the logged-in user (shown once)."""
    return TokenOut(token=await auth.mint_token(user))


@auth_router.get("/me/tokens", response_model=list[ApiTokenOut])
async def list_tokens(user: User = Depends(auth.require_user)) -> list[ApiToken]:
    """The logged-in user's API tokens (prefix + timestamps, never plaintext)."""
    return await auth.list_tokens(user)


@auth_router.delete("/me/tokens/{token_id}")
async def revoke_token(
    token_id: UUID, user: User = Depends(auth.require_user)
) -> dict[str, bool]:
    await auth.revoke_token(user, token_id)
    return {"success": True}


@auth_router.get("/registry/token", response_model=RegistryTokenOut)
async def registry_token(request: Request) -> RegistryTokenOut:
    """Docker Registry v2 token endpoint (the registry's `auth.token.realm`).

    Authenticates the ``mp_`` token sent as the HTTP Basic password (written by
    ``machineplay login``) and mints a signed JWT granting the requested scopes.
    """
    user = await auth.user_from_basic(request)
    return registry.issue_token(user, request.query_params.getlist("scope"))


# --- engines -------------------------------------------------------------------

engines_router = APIRouter(tags=["Engines"])


@engines_router.get("/engine", response_model=list[EngineOut])
async def list_engines() -> list[EngineOut]:
    return await engines.list_engines()


@engines_router.post("/engine/register", response_model=EngineRegisterResponse)
async def register_engine(
    payload: EngineRegisterRequest,
    user: User = Depends(auth.require_token_user),
) -> EngineRegisterResponse:
    """Record an engine version after the CLI pushed its image to the registry."""
    return await engines.register_version(user, payload)


@engines_router.get("/user/{login}/{engine_name}", response_model=EngineDetailOut)
async def get_engine_by_name(login: str, engine_name: str) -> EngineDetailOut:
    """Engine detail addressed GitHub-style: owner handle + engine name."""
    owner = await users.by_login(login)
    return await engines.detail(await engines.by_name(owner, engine_name))


@engines_router.delete("/user/{login}/{engine_name}")
async def delete_engine(
    login: str, engine_name: str, user: User = Depends(auth.require_user)
) -> dict[str, bool]:
    """Delete an engine, its versions, and its registry images (owner/admin)."""
    owner = await users.by_login(login)
    await engines.delete_engine(user, await engines.by_name(owner, engine_name))
    return {"success": True}


# --- profiles ------------------------------------------------------------------

profiles_router = APIRouter(tags=["Profiles"])


@profiles_router.get("/user/{login}", response_model=UserProfileOut)
async def user_profile(login: str) -> UserProfileOut:
    """Public profile: the user, their engines, and those engines' games."""
    return await users.profile(login)


# --- runners --------------------------------------------------------------------

runners_router = APIRouter(tags=["Runners"])


@runners_router.get("/runners", response_model=list[RunnerOut])
async def list_runners() -> list[RunnerOut]:
    """All registered runners, durable metadata joined with live online status."""
    return await runners.list_runners()


@runners_router.get("/runner/{runner_id}", response_model=RunnerOut)
async def get_runner(runner_id: UUID) -> RunnerOut:
    """One runner's durable metadata joined with its live online status."""
    return await runners.get_runner(runner_id)


@runners_router.patch("/runner/{runner_id}", response_model=RunnerOut)
async def update_runner(
    runner_id: UUID,
    payload: RunnerUpdateRequest,
    user: User = Depends(auth.require_user),
) -> RunnerOut:
    """Edit a runner's name/description (owner only)."""
    return await runners.edit_runner(user, runner_id, payload)


# --- games ----------------------------------------------------------------------

games_router = APIRouter(tags=["Games"])


@games_router.post("/game")
async def start_game(
    payload: StartGameRequest, user: User = Depends(auth.require_user)
) -> StartGameResponse:
    return await games.start_game(user, payload)


@games_router.post("/game/{game_id}/cancel")
async def cancel_game(
    game_id: UUID, user: User = Depends(auth.require_user)
) -> dict[str, bool]:
    """Stop a running game: aborts it and kills its engine containers."""
    await games.cancel_game(user, game_id)
    return {"success": True}


@games_router.get("/game", response_model=list[GameOut])
async def list_games(limit: int = Query(default=50, ge=1, le=200)) -> list[Game]:
    return await games.list_games(limit)


@games_router.get("/game/{game_id}", response_model=GameOut)
async def get_game(game_id: UUID) -> Game:
    return await games.get_game(game_id)


# --- tournaments --------------------------------------------------------------

tournaments_router = APIRouter(tags=["Tournaments"])


@tournaments_router.post("/tournament", response_model=TournamentDetailOut)
async def create_tournament(
    payload: TournamentCreateRequest, user: User = Depends(auth.require_user)
) -> TournamentDetailOut:
    """Create a tournament and start dispatching its pairings onto the runner."""
    tour = await tournaments.create_tournament(user, payload)
    return await tournaments.tournament_detail(tour.id)


@tournaments_router.get("/tournament", response_model=list[TournamentOut])
async def list_tournaments(
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TournamentOut]:
    return await tournaments.list_tournaments(limit)


@tournaments_router.get(
    "/tournament/{tournament_id}", response_model=TournamentDetailOut
)
async def get_tournament(tournament_id: UUID) -> TournamentDetailOut:
    """Tournament detail: participants, computed standings, and its games."""
    return await tournaments.tournament_detail(tournament_id)


@tournaments_router.post("/tournament/{tournament_id}/cancel")
async def cancel_tournament(
    tournament_id: UUID, user: User = Depends(auth.require_user)
) -> dict[str, bool]:
    """Stop a running tournament (creator or admin only)."""
    await tournaments.cancel_tournament(user, tournament_id)
    return {"success": True}


# --- live streaming -----------------------------------------------------------

streaming_router = APIRouter(tags=["Live streaming"])


@streaming_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await streaming.runner_session(ws)


@streaming_router.get(
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


@streaming_router.get(
    "/stream/live",
    response_class=EventSourceResponse,
    responses={200: {"model": LiveStreamEvent}},
)
async def sse_live_stream() -> AsyncIterable[LiveStreamEvent]:
    async for event in streaming.live_event_stream():
        yield event


@streaming_router.get(
    "/stream/runners",
    response_class=EventSourceResponse,
    responses={200: {"model": RunnerLiveEvent}},
)
async def sse_runner_stream() -> AsyncIterable[RunnerLiveEvent]:
    async for event in streaming.runner_event_stream():
        yield event


# Mount the grouped sub-routers onto the router that `app.main` includes.
router.include_router(auth_router)
router.include_router(engines_router)
router.include_router(profiles_router)
router.include_router(games_router)
router.include_router(runners_router)
router.include_router(tournaments_router)
router.include_router(streaming_router)
