"""GitHub OAuth over signed-cookie sessions, API tokens, and current-user deps.

A known GitHub account logs straight in. An unknown one is stashed in the
session as ``pending_signup`` and sent to the frontend's ``/register`` page to
pick a handle, which is never overwritten from GitHub afterwards.
"""

import base64
import binascii
import hashlib
import logging
import re
import secrets
from collections.abc import MutableMapping
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import Depends, Request

from app import users
from app.config import settings
from app.exceptions import AppException, AuthError, ConflictError, NotFoundError
from app.models import ApiToken, User, utcnow
from app.schemas import PendingSignupOut

logger = logging.getLogger(__name__)

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# Handles are the registry namespace and the profile URL at the frontend root,
# so every top-level frontend route must be reserved.
HANDLE_RE = re.compile(r"^[a-z0-9](?:-?[a-z0-9]){0,31}$")
RESERVED_HANDLES = {
    # frontend routes
    "cli",
    "docs",
    "engine",
    "game",
    "register",
    "runners",
    "tournament",
    # api, infra and spares
    "about",
    "admin",
    "api",
    "assets",
    "auth",
    "blog",
    "help",
    "login",
    "logout",
    "machineplay",
    "me",
    "registry",
    "settings",
    "static",
    "stream",
    "u",
    "www",
}

Session = MutableMapping[str, Any]


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def mint_token(user: User) -> str:
    """Create a new API token for ``user`` and return its plaintext (once)."""
    plaintext = "mp_" + secrets.token_urlsafe(32)
    await ApiToken(
        user=user,
        token_hash=_hash_token(plaintext),
        prefix=plaintext[:11],
    ).insert()
    logger.info("minted api token for user=%s", user.login)
    return plaintext


async def list_tokens(user: User) -> list[ApiToken]:
    return await ApiToken.find({"user.$id": user.id}).sort("-created_at").to_list()


async def revoke_token(user: User, token_id: UUID) -> None:
    token = await ApiToken.get(token_id)
    if token is None or token.user.ref.id != user.id:
        raise NotFoundError("token not found")
    await token.delete()
    logger.info("revoked api token %s for user=%s", token.prefix, user.login)


# Bursts of authenticated requests write last_used_at once per interval, not
# once per request; the token UI only needs minute granularity.
LAST_USED_THROTTLE = timedelta(seconds=60)


async def user_from_token(plaintext: str) -> User | None:
    if not plaintext:
        return None
    token = await ApiToken.find_one(ApiToken.token_hash == _hash_token(plaintext))
    if token is None:
        return None
    await _touch_last_used(token)
    return await User.get(token.user.ref.id)


async def _touch_last_used(token: ApiToken) -> None:
    now = utcnow()
    if token.last_used_at is not None and now - token.last_used_at < LAST_USED_THROTTLE:
        return
    await token.set({ApiToken.last_used_at: now})


async def user_from_auth_header(header: str) -> User | None:
    """Takes the raw header so the runner WebSocket handshake can share it."""
    scheme, _, plaintext = header.partition(" ")
    if scheme.lower() != "bearer" or not plaintext:
        return None
    return await user_from_token(plaintext)


async def _user_from_bearer(request: Request) -> User | None:
    return await user_from_auth_header(request.headers.get("Authorization", ""))


async def get_current_user(request: Request) -> User | None:
    """Resolve the user from the browser session, falling back to a CLI token."""
    user_id = request.session.get("user_id")
    if user_id:
        return await User.get(UUID(user_id))
    return await _user_from_bearer(request)


async def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise AuthError()
    return user


async def require_token_user(request: Request) -> User:
    """For CLI-facing endpoints, which have no browser session."""
    user = await _user_from_bearer(request)
    if user is None:
        raise AuthError("missing or invalid bearer token")
    return user


async def user_from_basic(request: Request) -> User | None:
    """The ``mp_`` token ``docker login`` sends as the Basic password.

    None for anonymous pulls; 401s on credentials that don't resolve.
    """
    password = _basic_password(request)
    if password is None:
        return None
    user = await user_from_token(password)
    if user is None:
        raise AuthError("invalid registry credentials")
    return user


def _basic_password(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, _, password = decoded.partition(":")
    return password or None


def begin_github_login(session: Session) -> str:
    """Stash a CSRF ``state`` in the session, return GitHub's consent URL."""
    if not settings.github_client_id:
        raise AppException("GitHub OAuth is not configured (set GITHUB_CLIENT_ID)")

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    query = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "scope": "read:user",
            "state": state,
            "allow_signup": "true",
        }
    )
    return f"{GITHUB_AUTHORIZE_URL}?{query}"


async def _fetch_github_profile(code: str) -> dict[str, Any]:
    """Exchange the OAuth ``code`` for an access token and fetch the profile."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.oauth_redirect_uri,
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise AuthError("failed to obtain access token from GitHub")

        user_resp = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        user_resp.raise_for_status()
        profile: dict[str, Any] = user_resp.json()
        return profile


async def complete_github_login(session: Session, code: str, state: str) -> str:
    """Handle the OAuth callback; returns the frontend URL to redirect to."""
    expected = session.pop("oauth_state", None)
    if not expected or not secrets.compare_digest(state, expected):
        raise AuthError("invalid oauth state")

    gh = await _fetch_github_profile(code)
    user = await User.find_one(User.github_id == gh["id"])
    if user is None:
        session["pending_signup"] = {
            "github_id": gh["id"],
            "login": gh["login"],
            "name": gh.get("name"),
            "avatar_url": gh.get("avatar_url", ""),
        }
        return f"{settings.frontend_url}/register"

    # The handle is never overwritten from GitHub.
    user.name = gh.get("name")
    user.avatar_url = gh.get("avatar_url", "")
    await user.save()

    session["user_id"] = str(user.id)
    return settings.frontend_url


def pending_signup(session: Session) -> PendingSignupOut:
    """The GitHub profile waiting on a handle, for the /register page."""
    pending = session.get("pending_signup")
    if not pending:
        raise AuthError("no signup in progress")
    return PendingSignupOut(
        suggested_login=pending["login"].lower(),
        name=pending.get("name"),
        avatar_url=pending.get("avatar_url", ""),
    )


async def register(session: Session, requested_login: str) -> User:
    """Complete a pending GitHub signup with the chosen handle and log it in."""
    pending = session.get("pending_signup")
    if not pending:
        raise AuthError("no signup in progress")

    # Another tab may have finished this signup already.
    user = await User.find_one(User.github_id == pending["github_id"])
    if user is None:
        login = requested_login.strip().lower()
        if not HANDLE_RE.fullmatch(login):
            raise ConflictError(
                "handle must be 1-32 characters: a-z, 0-9 and single hyphens, "
                "starting and ending with a letter or digit"
            )
        if login in RESERVED_HANDLES:
            raise ConflictError(f"handle {login!r} is reserved")
        if await users.find_by_login(login) is not None:
            raise ConflictError(f"handle {login!r} is already taken")
        user = User(
            github_id=pending["github_id"],
            login=login,
            name=pending.get("name"),
            avatar_url=pending.get("avatar_url", ""),
        )
        await user.insert()
        logger.info("registered new user login=%s id=%s", user.login, user.id)

    session.pop("pending_signup", None)
    session["user_id"] = str(user.id)
    return user
