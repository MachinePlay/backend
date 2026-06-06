"""GitHub OAuth login via signed-cookie sessions.

Flow:
  1. Browser hits ``GET /auth/github/login`` → 302 to GitHub's consent screen
     (carrying a random ``state`` we stash in the session).
  2. GitHub redirects back to ``GET /auth/github/callback`` with ``code`` +
     ``state``; we verify ``state``, exchange ``code`` for an access token,
     fetch the GitHub profile, upsert a :class:`~app.models.User`, store its id
     in the session, then 302 back to the frontend.

The session itself lives in a signed cookie managed by Starlette's
``SessionMiddleware`` (wired up in ``app.main``).
"""

import hashlib
import logging
import secrets
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.config import settings
from app.exceptions import AppException, AuthError
from app.models import ApiToken, User, utcnow
from app.schemas import TokenOut, UserOut

logger = logging.getLogger(__name__)
router = APIRouter()

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def _user_from_bearer(request: Request) -> User | None:
    """Resolve a user from an ``Authorization: Bearer <token>`` header, or None."""
    header = request.headers.get("Authorization", "")
    scheme, _, plaintext = header.partition(" ")
    if scheme.lower() != "bearer" or not plaintext:
        return None

    token = await ApiToken.find_one(ApiToken.token_hash == _hash_token(plaintext))
    if token is None:
        return None

    token.last_used_at = utcnow()
    await token.save()
    return await User.get(token.user_id)


async def get_current_user(request: Request) -> User | None:
    """Resolve the user from the browser session, falling back to a CLI token."""
    user_id = request.session.get("user_id")
    if user_id:
        return await User.get(UUID(user_id))
    return await _user_from_bearer(request)


async def require_user(user: User | None = Depends(get_current_user)) -> User:
    """Dependency that 401s when there is no logged-in user."""
    if user is None:
        raise AuthError()
    return user


async def mint_token(user: User) -> str:
    """Create a new API token for ``user`` and return its plaintext (once)."""
    plaintext = "mp_" + secrets.token_urlsafe(32)
    await ApiToken(
        user_id=user.id,
        token_hash=_hash_token(plaintext),
        prefix=plaintext[:11],
    ).insert()
    return plaintext


async def require_token_user(request: Request) -> User:
    """Resolve the user from an ``Authorization: Bearer <token>`` header.

    Used by CLI-facing endpoints (engine upload) where there is no browser
    session. 401s on a missing or unknown token.
    """
    user = await _user_from_bearer(request)
    if user is None:
        raise AuthError("missing or invalid bearer token")
    return user


@router.get("/auth/github/login")
async def github_login(request: Request) -> RedirectResponse:
    if not settings.github_client_id:
        raise AppException("GitHub OAuth is not configured (set GITHUB_CLIENT_ID)")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    query = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "scope": "read:user",
            "state": state,
            "allow_signup": "true",
        }
    )
    return RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{query}")


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str, state: str) -> RedirectResponse:
    expected = request.session.pop("oauth_state", None)
    if not expected or not secrets.compare_digest(state, expected):
        raise AuthError("invalid oauth state")

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
        gh = user_resp.json()

    user = await User.find_one(User.github_id == gh["id"])
    if user is None:
        user = User(
            github_id=gh["id"],
            login=gh["login"],
            name=gh.get("name"),
            avatar_url=gh.get("avatar_url", ""),
        )
        await user.insert()
        logger.info("registered new user login=%s id=%s", user.login, user.id)
    else:
        # Refresh profile fields that may have changed on GitHub.
        user.login = gh["login"]
        user.name = gh.get("name")
        user.avatar_url = gh.get("avatar_url", "")
        await user.save()

    request.session["user_id"] = str(user.id)
    return RedirectResponse(settings.frontend_url)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(require_user)) -> User:
    return user


@router.post("/me/tokens", response_model=TokenOut)
async def create_token(user: User = Depends(require_user)) -> TokenOut:
    """Mint a CLI API token for the logged-in user (shown once)."""
    plaintext = await mint_token(user)
    logger.info("minted api token for user=%s", user.login)
    return TokenOut(token=plaintext)


@router.post("/auth/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"success": True}
