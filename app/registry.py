"""Docker Registry v2 token issuer.

The registry's `auth.token.realm` is ``GET /registry/token``. On a 401, docker
calls it with the requested scope and HTTP Basic ``<login>:<mp-token>``; we
mint a short-lived RS256 JWT granting what the user may do, and docker retries
with it.
"""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.config import settings
from app.models import User, utcnow
from app.schemas import RegistryTokenOut

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Access:
    """One granted registry scope, serialized into the token's `access` claim."""

    type: str
    name: str
    actions: list[str]


def _load_private_key() -> RSAPrivateKey:
    if settings.registry_auth_key_file is not None:
        pem = settings.registry_auth_key_file.read_bytes()
    elif settings.registry_auth_key:
        pem = settings.registry_auth_key.encode()
    else:
        raise RuntimeError(
            "registry token signing key not configured "
            "(set REGISTRY_AUTH_KEY_FILE or REGISTRY_AUTH_KEY)"
        )
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise RuntimeError("registry auth key must be an RSA private key")
    return key


def _libtrust_kid(key: RSAPrivateKey) -> str:
    # docker/distribution's libtrust key id: base32 of the first 240 bits of
    # the SPKI's SHA-256, in colon-separated quads.
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()[:30]
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    return ":".join(b32[i : i + 4] for i in range(0, len(b32), 4))


def _load_cert_x5c() -> str | None:
    """The signing cert as base64 DER for the `x5c` header.

    Optional, but distribution v3 rejects tokens with only a `kid`.
    """
    if settings.registry_auth_cert_file is not None:
        pem = settings.registry_auth_cert_file.read_bytes()
    elif settings.registry_auth_cert:
        pem = settings.registry_auth_cert.encode()
    else:
        return None
    cert = x509.load_pem_x509_certificate(pem)
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode(
        "ascii"
    )


@lru_cache(maxsize=1)
def _signer() -> tuple[RSAPrivateKey, str, str | None]:
    key = _load_private_key()
    return key, _libtrust_kid(key), _load_cert_x5c()


def parse_scope(scope: str) -> Access | None:
    """Parse ``repository:<name>:<actions>``; None if malformed or unsupported."""
    parts = scope.split(":")
    if len(parts) < 3 or parts[0] != "repository":
        return None
    name = ":".join(parts[1:-1])
    actions = [a for a in parts[-1].split(",") if a]
    if not name or not actions:
        return None
    return Access(type="repository", name=name, actions=actions)


def grant_scopes(scopes: list[str], namespace: str | None) -> list[Access]:
    """``pull`` is always granted; ``push`` only under the user's ``namespace``."""
    granted: list[Access] = []
    for scope in scopes:
        access = parse_scope(scope)
        if access is None:
            continue
        repo_ns = access.name.split("/", 1)[0]
        actions: list[str] = []
        if "pull" in access.actions or "*" in access.actions:
            actions.append("pull")
        if (
            ("push" in access.actions or "*" in access.actions)
            and namespace is not None
            and repo_ns == namespace
        ):
            actions.append("push")
        if actions:
            granted.append(Access(type=access.type, name=access.name, actions=actions))
    return granted


def issue_token(user: User | None, scopes: list[str]) -> RegistryTokenOut:
    """`user` None is an anonymous pull."""
    namespace = user.login.lower() if user else None
    token, ttl = make_token(
        subject=user.login if user else "",
        granted=grant_scopes(scopes, namespace),
    )
    return RegistryTokenOut(
        token=token,
        access_token=token,
        expires_in=ttl,
        issued_at=utcnow().isoformat(),
    )


async def delete_manifest(repository: str, digest: str) -> None:
    """Delete one manifest; raises on anything but deleted or already gone.

    Safe for other users with the identical image: blobs are shared, but each
    repository has its own manifest keeping them alive until GC.
    """
    if not settings.registry_api_url:
        logger.info(
            "REGISTRY_API_URL not set; skipping manifest delete %s@%s",
            repository,
            digest,
        )
        return
    # The "delete" action is never granted to users.
    token, _ = make_token(
        subject="machineplay-backend",
        granted=[Access(type="repository", name=repository, actions=["delete"])],
    )
    url = f"{settings.registry_api_url}/v2/{repository}/manifests/{digest}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code in (202, 404):  # deleted / already gone
        logger.info("deleted manifest %s@%s (%s)", repository, digest, resp.status_code)
        return
    raise RuntimeError(
        f"registry manifest delete failed for {repository}@{digest}: "
        f"{resp.status_code} {resp.text[:200]}"
    )


def make_token(subject: str, granted: list[Access]) -> tuple[str, int]:
    """Returns (jwt, ttl)."""
    key, kid, x5c = _signer()
    now = int(time.time())
    ttl = settings.registry_token_ttl
    claims = {
        "iss": settings.registry_issuer,
        "sub": subject,
        "aud": settings.registry_host,
        "iat": now,
        "nbf": now - 10,  # clock skew
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(16),
        "access": [
            {"type": a.type, "name": a.name, "actions": a.actions} for a in granted
        ],
    }
    # distribution v3 trusts x5c; kid only matters to older servers.
    headers: dict[str, object] = {"kid": kid}
    if x5c is not None:
        headers["x5c"] = [x5c]
    token = jwt.encode(claims, key, algorithm="RS256", headers=headers)
    return token, ttl
