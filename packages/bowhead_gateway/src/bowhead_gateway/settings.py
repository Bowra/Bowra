"""Gateway 配置（从 env 加载）。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。

    所有字段从环境变量读取（prefix=BOWHEAD_）。.env 文件 dev 期自动加载。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BOWHEAD_",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = Field(default="dev", description="dev | staging | prod")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json", description="json | pretty")

    # Storage.
    db_url: str = Field(default=f"sqlite+aiosqlite:///{Path('.data/sqlite/bowhead.db').resolve()}")
    redis_url: str = Field(default="redis://localhost:6379/0")
    chroma_url: str = Field(default="http://localhost:8000")

    # Datarc Embedding Service (vector database), integrated in M2.6a.
    # Default to empty so an unconfigured deployment skips global health probes. Per-turn embeddings
    # use the team Datarc connection proxy (embed-via-openapi), independent of this global direct connection.
    vector_db_url: str = Field(
        default="",
        description="Datarc Embedding Service 根 URL（URL 本身即 tenant 秘钥）；空 = 禁用全局直连向量库",
    )
    vector_db_token: str = Field(
        default="",
        description="向量库 access_token；dev 环境留空（URL 即秘钥，实测带 bearer 也不做校验）",
    )
    vector_db_timeout_sec: int = Field(default=30)
    vector_db_enabled: bool = Field(
        default=True,
        description="关掉后 gateway 跳过所有向量操作（含 lifespan health 探测）",
    )
    # Datarc collection names: v1_<tenant>_p<workspace_id>, optionally suffixed _keywords.
    # Tenant is the Datarc-side client and varies between deployments. An empty default
    # leaves discovery to EmbedProxyClientFactory through the configured connection.
    # Administrators pin it only when inference is ambiguous, for example when several
    # clients share a vector service and have colliding project IDs.
    vector_db_tenant: str = Field(
        default="",
        description="collection 命名前缀 tenant 段的显式 pin；留空 = 按连接自动判定（推荐）",
    )

    # Cost cap
    session_max_cost_usd: float = Field(default=5.0)
    session_max_tokens: int = Field(default=500_000)

    # Auth
    auth_mode: str = Field(default="dev", description="dev | oidc | jwt | bi_passthrough")
    dev_default_user: str = Field(default="u_dev_alice")

    # Sprint file-upload-presign Phase 1.2: dev presigned-POST receiver.
    # Prod uses S3Backend (no local-receiver route registered). Dev opts in
    # by setting BOWHEAD_ALLOW_LOCAL_RECEIVER=1 (+ secret) so the LocalBackend
    # produces a JWT-signed envelope addressed to a gateway-mounted receiver
    # that streams chunks to disk without touching the FastAPI body-pump.
    allow_local_receiver: bool = Field(
        default=False,
        description="Dev only. Prod startup asserts this stays False.",
    )
    local_presign_secret: str = Field(
        default="",
        description="HS256 secret for JWT envelopes; auto-generated per process if blank.",
    )
    gateway_base_url: str = Field(
        default="http://localhost:8090",
        description="Legacy dev local-receiver opt-in value; browser uploads use same-origin paths.",
    )

    # Feishu OpenAPI IM adapter.
    feishu_enabled: bool = Field(default=False)
    feishu_app_id: str = Field(default="")
    feishu_app_secret: str = Field(default="")
    feishu_verification_token: str = Field(default="")
    feishu_encrypt_key: str = Field(default="")
    feishu_bot_open_id: str = Field(default="")
    feishu_group_public_answers_enabled: bool = Field(default=False)
    feishu_openapi_base_url: str = Field(default="https://open.feishu.cn")
    feishu_event_ingress: str = Field(default="webhook")
    feishu_sweep_interval_sec: int = Field(default=3600)
    feishu_dedup_retention_days: float = Field(default=14.0)
    feishu_turn_link_retention_days: float = Field(default=30.0)
    feishu_session_retention_days: float = Field(default=30.0)


def get_settings() -> Settings:
    """Singleton accessor（FastAPI dependency 友好）。"""
    return Settings()
