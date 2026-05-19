from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str
    REDIS_URL: str
    API_KEY: str

    POOL_MIN_SIZE: int = 5
    POOL_MAX_SIZE: int = 20
    POOL_COMMAND_TIMEOUT: int = 10

    CACHE_TTL_SECONDS: int = 86400

    WORKERS: int = 4
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "production"


settings = Settings()
