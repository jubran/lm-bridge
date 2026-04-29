from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    LM_BASE_URL:         str   = "http://localhost:1234"   # LM Studio default
    LM_MODEL:            str   = "qwen3.5-9b"
    DEFAULT_TEMPERATURE: float = 0.6
    DEFAULT_MAX_TOKENS:  int   = 8192
    HOST:                str   = "0.0.0.0"
    PORT:                int   = 8082

    class Config:
        env_file = ".env"


settings = Settings()
