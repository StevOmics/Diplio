from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://mediabridge:mediabridge@db:5432/mediabridge"
    rabbitmq_default_user: str = "mediabridge"
    rabbitmq_default_pass: str = "mediabridge"
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672

    class Config:
        env_file = ".env"

    @property
    def broker_url(self) -> str:
        return f"amqp://{self.rabbitmq_default_user}:{self.rabbitmq_default_pass}@{self.rabbitmq_host}:{self.rabbitmq_port}//"


settings = Settings()
