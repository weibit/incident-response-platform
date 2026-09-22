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
    knowledge_enabled: bool = False
    knowledge_mcp_url: str = "http://runbooks:8080/mcp"
    knowledge_mcp_token: SecretStr = SecretStr("")
    # The MCP SDK enables DNS-rebinding protection by default and rejects any Host
    # header not listed here. The worker reaches the server by its Compose name.
    knowledge_allowed_hosts: list[str] = ["runbooks:8080", "localhost:8080", "127.0.0.1:8080"]
    knowledge_top_k: int = Field(default=5, ge=1, le=5)
    embedding_model: str | None = None
    embedding_dim: int | None = Field(default=None, ge=1, le=4096)

    @model_validator(mode="after")
    def validate_credentials(self):
        secrets = [
            self.jira_webhook_token,
            self.jira_approval_token,
            self.status_token,
            self.slack_signing_secret,
            self.siem_signing_secret,
        ]
        if self.knowledge_enabled:
            # Only a trust boundary once it is actually in use, so the default
            # configuration still starts with no new secret.
            if not self.knowledge_mcp_token.get_secret_value():
                raise ValueError("knowledge_mcp_token is required when knowledge_enabled is true")
            secrets.append(self.knowledge_mcp_token)
        values = [value.get_secret_value() for value in secrets]
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("Configure nonempty, distinct credentials for each trust boundary")
        if self.embedding_model and not self.embedding_dim:
            raise ValueError("embedding_dim is required when embedding_model is set")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
