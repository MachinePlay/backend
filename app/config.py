from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tc: str = Field(default="30+0.3", validation_alias="MACHINEPLAY_TC")

    # Where uploaded engine image tarballs are stored (relative to the backend
    # working dir unless absolute). Add `storage/` to .gitignore.
    storage_dir: Path = Field(default=Path("storage"), validation_alias="STORAGE_DIR")
    # Reject engine tarballs larger than this (bytes). Default 200 MB.
    max_upload_bytes: int = Field(
        default=200 * 1024 * 1024, validation_alias="MAX_UPLOAD_BYTES"
    )
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


settings = Settings()
