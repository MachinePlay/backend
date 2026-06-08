"""Docker Registry v2 token issuer.

The registry (`registry.machineplay.org`) is configured with `auth.token`
pointing its `realm` at ``GET /registry/token`` on this backend. The push/pull
handshake is:

  1. ``docker push registry.machineplay.org/<login>/<engine>:<tag>``
  2. registry replies ``401`` with
     ``WWW-Authenticate: Bearer realm="…/registry/token",service="…",scope="repository:<login>/<engine>:push,pull"``
  3. docker GETs the realm with that ``service``/``scope`` and HTTP Basic auth
     (``<login>:<mp-token>``, written by ``machineplay login``)
  4. we authenticate the ``mp_`` token, decide which actions to grant, and mint
     a short-lived RS256 JWT whose ``access`` claim lists the granted scopes
  5. docker retries with ``Authorization: Bearer <jwt>``; the registry verifies
     the signature against its ``rootcertbundle`` (the cert matching our key)

The JWT header carries a libtrust key id (``kid``) derived from the signing
key; the registry uses it to pick the matching cert from its bundle. The
derivation is base32 of the first 240 bits of the SHA-256 of the DER-encoded
``SubjectPublicKeyInfo``, grouped into twelve colon-separated quads — exactly
what docker/distribution's libtrust computes for the same cert.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.config import settings


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
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()[:30]
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    return ":".join(b32[i : i + 4] for i in range(0, len(b32), 4))


@lru_cache(maxsize=1)
def _signer() -> tuple[RSAPrivateKey, str]:
    key = _load_private_key()
    return key, _libtrust_kid(key)


def parse_scope(scope: str) -> Access | None:
    """Parse a ``repository:<name>:<actions>`` scope string into an Access.

    Repository names may contain ``/`` but never ``:`` (that separates the
    fields), so split on the first and last colons. Returns None for malformed
    or unsupported (non-repository) scopes.
    """
    parts = scope.split(":")
    if len(parts) < 3 or parts[0] != "repository":
        return None
    name = ":".join(parts[1:-1])
    actions = [a for a in parts[-1].split(",") if a]
    if not name or not actions:
        return None
    return Access(type="repository", name=name, actions=actions)


def make_token(subject: str, granted: list[Access]) -> tuple[str, int]:
    """Sign a registry JWT for `subject` granting `granted`. Returns (jwt, ttl)."""
    key, kid = _signer()
    now = int(time.time())
    ttl = settings.registry_token_ttl
    claims = {
        "iss": settings.registry_issuer,
        "sub": subject,
        "aud": settings.registry_host,
        "iat": now,
        "nbf": now - 10,  # small skew allowance
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(16),
        "access": [
            {"type": a.type, "name": a.name, "actions": a.actions} for a in granted
        ],
    }
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
    return token, ttl
