from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "AI Mercado Manager"
    environment: str = "development"
    database_url: str = "sqlite:///./mercado_manager.db"
    app_encryption_key: str = ""
    ai_enabled: bool = False
    auto_sync_worker_enabled: bool = True
    backup_status_path: str = ""
    app_access_token: str = ""
    auth_required: bool = True
    allowed_hosts: str = "localhost,127.0.0.1,::1"
    api_docs_enabled: bool = False
    public_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    ml_client_id: str = ""
    ml_client_secret: str = ""
    ml_redirect_uri: str = "https://YOUR-HTTPS-DOMAIN/api/v1/integrations/mercadolivre/callback"
    ml_site_id: str = "MLB"
    marketplace_write_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
