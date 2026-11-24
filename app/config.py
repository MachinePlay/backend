from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tc: str = Field(default="30+0.3", validation_alias="MACHINEPLAY_TC")

    # `registry_host` is the public hostname engines are tagged with, and the
    # token `aud`. `registry_issuer` must match the registry's
    # `auth.token.issuer`. The key/cert pair signs tokens; each is given inline
    # or by path, and the file wins.
    registry_host: str = Field(
        default="registry.machineplay.org", validation_alias="REGISTRY_HOST"
    )
    registry_issuer: str = Field(
        default="machineplay-auth", validation_alias="REGISTRY_ISSUER"
    )
    registry_auth_key: str = Field(default="", validation_alias="REGISTRY_AUTH_KEY")
    registry_auth_key_file: Path | None = Field(
        default=None, validation_alias="REGISTRY_AUTH_KEY_FILE"
    )
    # Embedded in each token's `x5c` header: distribution v3 doesn't trust
    # `kid`-only tokens against its rootcertbundle.
    registry_auth_cert: str = Field(default="", validation_alias="REGISTRY_AUTH_CERT")
    registry_auth_cert_file: Path | None = Field(
        default=None, validation_alias="REGISTRY_AUTH_CERT_FILE"
    )
    registry_token_ttl: int = Field(default=300, validation_alias="REGISTRY_TOKEN_TTL")
    # Where the backend deletes manifests of removed engines; empty skips that.
    registry_api_url: str = Field(default="", validation_alias="REGISTRY_API_URL")

    mongo_url: str = Field(
        default="mongodb://localhost:27017", validation_alias="MONGO_URL"
    )
    mongo_db: str = Field(default="machineplay", validation_alias="MONGO_DB")

    # Signs session cookies. MUST be overridden in production.
    secret_key: str = Field(
        default="dev-insecure-change-me", validation_alias="SECRET_KEY"
    )
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")

    github_client_id: str = Field(default="", validation_alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field(
        default="", validation_alias="GITHUB_CLIENT_SECRET"
    )
    # Must match the GitHub OAuth app's "Authorization callback URL".
    oauth_redirect_uri: str = Field(
        default="http://localhost:8000/auth/github/callback",
        validation_alias="OAUTH_REDIRECT_URI",
    )
    frontend_url: str = Field(
        default="http://localhost:5173", validation_alias="FRONTEND_URL"
    )
    # Credentialed CORS needs explicit origins, never "*". The env var is a
    # JSON array.
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "https://machineplay.org"],
        validation_alias="CORS_ORIGINS",
    )


settings = Settings()
