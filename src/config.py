from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    hsdl_db_path: str = "data/hsdl_catalog.db"
    hsdl_data_dir: str = "data"
    host: str = "0.0.0.0"
    port: int = 7780

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
