"""Validated runtime settings loaded from environment variables and .env.

Group database/Kafka connections, credentials, workflow limits, model gateway aliases,
and knowledge retrieval options. Validators reject invalid configuration; settings()
caches it per process. Worker replica counts belong to deployment configuration.
"""

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
    # Gateway aliases, not provider model names. LiteLLM maps these to real models, so
    # changing which model serves a role is a gateway config change, not a redeploy.
    llm_model_triage: str | None = None
    llm_model_rca: str | None = None
    llm_model_plan: str | None = None
    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: SecretStr = SecretStr("")
    langfuse_enabled: bool = False
    knowledge_enabled: bool = False
    # Trailing slash is required: LiteLLM answers /mcp with a 307 to /mcp/, and the MCP
    # SDK does not follow it on POST, so the initialize handshake fails without it.
    knowledge_mcp_url: str = "http://litellm:4000/mcp/"
    knowledge_mcp_token: SecretStr = SecretStr("")
    # The MCP SDK enables DNS-rebinding protection by default and rejects any Host
    # header not listed here. The worker reaches the server by its Compose name.
    knowledge_allowed_hosts: list[str] = ["runbooks:8080", "localhost:8080", "127.0.0.1:8080"]
    knowledge_top_k: int = Field(default=5, ge=1, le=5)
    embedding_model: str | None = None
    embedding_dim: int | None = Field(default=None, ge=1, le=4096)

    @property
    def triage_model(self) -> str:
        return self.llm_model_triage or self.llm_model

    @property
    def rca_model(self) -> str:
        return self.llm_model_rca or self.llm_model

    @property
    def plan_model(self) -> str:
        return self.llm_model_plan or self.llm_model

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
        if self.knowledge_enabled or not self.mock_mode:
            # Both paths reach the gateway: retrieval is gateway-only, and a live analyst
            # calls models through it. Fail at startup rather than at the first call.
            if not self.litellm_api_key.get_secret_value():
                raise ValueError("litellm_api_key is required when the gateway is in use")
            secrets.append(self.litellm_api_key)
        values = [value.get_secret_value() for value in secrets]
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("Configure nonempty, distinct credentials for each trust boundary")
        if self.embedding_model and not self.embedding_dim:
            raise ValueError("embedding_dim is required when embedding_model is set")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
