from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tc: str = Field(default="30+0.3", validation_alias="MACHINEPLAY_TC")

    # Docker registry that engines are pushed to. `registry_host` is the public
    # hostname the CLI tags/pushes to and that the runner pulls from; it doubles
    # as the JWT `aud`/`service` the registry validates against. `registry_issuer`
    # must match the registry's `auth.token.issuer`. `registry_auth_key` is the
    # RSA private key (PEM) used to sign registry tokens; its self-signed cert is
    # the registry's `auth.token.rootcertbundle`. Provide the key inline
    # (REGISTRY_AUTH_KEY) or by path (REGISTRY_AUTH_KEY_FILE); the file wins.
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
    # The signing cert (PEM), embedded in each token's `x5c` header so the
    # registry can verify it against its rootcertbundle. distribution v3 does
    # NOT build its `kid` trust-set from the rootcertbundle, so x5c (not kid) is
    # what actually gets the token trusted. Provide inline or by path.
    registry_auth_cert: str = Field(default="", validation_alias="REGISTRY_AUTH_CERT")
    registry_auth_cert_file: Path | None = Field(
        default=None, validation_alias="REGISTRY_AUTH_CERT_FILE"
    )
    # How long an issued registry token is valid (seconds).
    registry_token_ttl: int = Field(default=300, validation_alias="REGISTRY_TOKEN_TTL")

    mongo_url: str = Field(
        default="mongodb://localhost:27017", validation_alias="MONGO_URL"
    )
    mongo_db: str = Field(default="machineplay", validation_alias="MONGO_DB")

    # Secret used to sign session cookies. MUST be overridden in production.
    secret_key: str = Field(
        default="dev-insecure-change-me", validation_alias="SECRET_KEY"
    )
    # Send the session cookie only over HTTPS. Set true in production.
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")

    # GitHub OAuth app credentials (https://github.com/settings/developers).
    github_client_id: str = Field(default="", validation_alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field(
        default="", validation_alias="GITHUB_CLIENT_SECRET"
    )
    # Must match the "Authorization callback URL" of the GitHub OAuth app.
    oauth_redirect_uri: str = Field(
        default="http://localhost:8000/auth/github/callback",
        validation_alias="OAUTH_REDIRECT_URI",
    )
    # Where to send the browser back to after a successful login.
    frontend_url: str = Field(
        default="http://localhost:5173", validation_alias="FRONTEND_URL"
    )
    # Browser origins allowed by CORS. Credentialed requests (the session
    # cookie) require explicit origins, so never use "*" here. Override in the
    # environment as a JSON array: CORS_ORIGINS='["https://machineplay.org"]'.
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "https://machineplay.org"],
        validation_alias="CORS_ORIGINS",
    )


settings = Settings()
