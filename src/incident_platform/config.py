from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://incident:incident@localhost:5432/incident"
    kafka_bootstrap_servers: str = "localhost:19092"
    jira_webhook_token: SecretStr
    jira_approval_token: SecretStr
    status_token: SecretStr
    slack_signing_secret: SecretStr
    siem_signing_secret: SecretStr
    slack_approvers: list[str] = ["U_DEMO_APPROVER"]
    allowed_targets: list[str] = ["checkout"]
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_attempts: int = Field(default=5, ge=1, le=20)
    max_investigation_rounds: int = Field(default=6, ge=1, le=20)
    job_timeout_seconds: int = Field(default=240, ge=1)
    approval_ttl_seconds: int = Field(default=3600, ge=1)
    mock_mode: bool = True
    llm_model: str = "anthropic:claude-sonnet-4-5"
    langfuse_enabled: bool = False

    @model_validator(mode="after")
    def validate_credentials(self):
        secrets = [
            self.jira_webhook_token,
            self.jira_approval_token,
            self.status_token,
            self.slack_signing_secret,
            self.siem_signing_secret,
        ]
        values = [value.get_secret_value() for value in secrets]
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("Configure nonempty, distinct credentials for each trust boundary")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
