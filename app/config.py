"""Application configuration. All secrets/config via environment variables."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="IAS_", extra="ignore")

    # Core
    api_key: str = "dev-key-change-me"          # simple bearer auth for the API
    database_url: str = "sqlite+aiosqlite:///./incident_suite.db"
    public_base_url: str = "http://localhost:8000"  # used to build deep links in Slack/JIRA
    max_upload_mb: int = 100
    chunk_lines: int = 200
    chunk_overlap: int = 20
    token_budget_per_run: int = 400_000          # hard cap
    node_timeout_s: int = 120
    node_retries: int = 3

    # LLM — any OpenAI-compatible Chat Completions endpoint (OpenRouter, Gemini, OpenAI, etc.)
    llm_api_key: str = ""                        # empty => MockLLM (demo/CI mode)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    model: str = "openai/gpt-4o-mini"
    llm_mode: str = "auto"                       # auto | live | mock

    # Slack
    slack_bot_token: str = ""
    slack_channel: str = ""
    slack_auto_post: bool = False                # approval-gated by default

    # JIRA
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = "OPS"
    jira_issue_type: str = "Bug"                 # must exist on the target project's template
    jira_auto_create: bool = False

    @property
    def llm_is_live(self) -> bool:
        if self.llm_mode == "mock":
            return False
        if self.llm_mode == "live":
            return True
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
