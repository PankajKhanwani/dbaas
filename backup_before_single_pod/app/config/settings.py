"""
Application configuration using Pydantic Settings.
Loads configuration from environment variables with validation.
"""
from typing import List, Optional
from pydantic import Field, field_validator, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Main application settings with environment variable loading."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = Field(default="KubeDB DBaaS Platform", description="Application name")
    app_version: str = Field(default="1.0.0", description="Application version")
    environment: str = Field(default="development", description="Environment (development/staging/production)")
    debug: bool = Field(default=False, description="Debug mode")
    log_level: str = Field(default="INFO", description="Logging level")

    # Server
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, ge=1, le=65535, description="Server port")
    workers: int = Field(default=4, ge=1, le=32, description="Number of worker processes")
    reload: bool = Field(default=False, description="Auto-reload on code changes")

    # Security
    secret_key: str = Field(..., min_length=32, description="Secret key for JWT encoding")
    jwt_algorithm: str = Field(default="HS256", description="JWT algorithm")
    jwt_access_token_expire_minutes: int = Field(
        default=30, ge=1, le=1440, description="Access token expiration in minutes"
    )
    jwt_refresh_token_expire_days: int = Field(
        default=7, ge=1, le=30, description="Refresh token expiration in days"
    )
    api_key_header: str = Field(default="X-API-Key", description="API key header name")

    # Database (MongoDB for metadata storage)
    mongodb_url: str = Field(
        ..., description="MongoDB connection URL for metadata (e.g., mongodb://user:pass@host:27017/dbname)"
    )
    mongodb_database: str = Field(default="kubedb_dbaas", description="MongoDB database name")
    mongodb_max_pool_size: int = Field(default=20, ge=5, le=100, description="MongoDB max pool size (production: 20)")
    mongodb_min_pool_size: int = Field(default=2, ge=1, le=10, description="MongoDB min pool size (production: 2)")
    mongodb_max_idle_time_ms: int = Field(default=30000, description="Close idle connections after 30s")

    # Redis
    redis_url: RedisDsn = Field(..., description="Redis connection URL")
    redis_max_connections: int = Field(default=20, ge=5, le=100, description="Redis max connections (production: 20)")

    # Kubernetes
    kubeconfig_path: Optional[str] = Field(
        default=None, description="Path to kubeconfig file (None for in-cluster)"
    )
    k8s_namespace: str = Field(default="default", description="Default Kubernetes namespace")
    k8s_in_cluster: bool = Field(default=False, description="Running inside Kubernetes cluster")

    # KubeDB
    kubedb_enabled: bool = Field(default=True, description="Enable KubeDB integration")
    kubedb_operator_namespace: str = Field(
        default="kubedb", description="KubeDB operator namespace"
    )

    # HashiCorp Vault
    vault_enabled: bool = Field(default=False, description="Enable Vault integration")
    vault_addr: Optional[str] = Field(default=None, description="Vault server address")
    vault_token: Optional[str] = Field(default=None, description="Vault authentication token")
    vault_namespace: Optional[str] = Field(default=None, description="Vault namespace")

    # Monitoring
    prometheus_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    prometheus_port: int = Field(default=9090, ge=1, le=65535, description="Prometheus port")
    jaeger_enabled: bool = Field(default=False, description="Enable Jaeger tracing")
    jaeger_agent_host: str = Field(default="localhost", description="Jaeger agent host")
    jaeger_agent_port: int = Field(default=6831, ge=1, le=65535, description="Jaeger agent port")

    # Sentry
    sentry_dsn: Optional[str] = Field(default=None, description="Sentry DSN for error tracking")
    sentry_traces_sample_rate: float = Field(
        default=0.1, ge=0.0, le=1.0, description="Sentry traces sample rate"
    )

    # Rate Limiting
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")
    rate_limit_per_minute: int = Field(default=60, ge=1, description="Rate limit per minute")
    rate_limit_per_hour: int = Field(default=1000, ge=1, description="Rate limit per hour")

    # Multi-tenancy
    multi_tenant_enabled: bool = Field(default=True, description="Enable multi-tenancy")
    default_tenant_quota_cpu: int = Field(
        default=10, ge=1, description="Default tenant CPU quota (cores)"
    )
    default_tenant_quota_memory_gb: int = Field(
        default=20, ge=1, description="Default tenant memory quota (GB)"
    )
    default_tenant_quota_storage_gb: int = Field(
        default=100, ge=1, description="Default tenant storage quota (GB)"
    )
    default_tenant_max_databases: int = Field(
        default=10, ge=1, description="Default max databases per tenant"
    )

    # Backup & Restore
    backup_enabled: bool = Field(default=True, description="Enable backup functionality")
    backup_storage_type: str = Field(default="s3", description="Backup storage type (s3/gcs/azure)")
    backup_s3_bucket: Optional[str] = Field(default=None, description="S3 bucket for backups")
    backup_s3_region: Optional[str] = Field(default="us-east-1", description="S3 region")
    backup_retention_days: int = Field(default=30, ge=1, le=365, description="Backup retention in days")

    # Database Reconciler
    reconcile_interval: int = Field(default=30, ge=10, le=300, description="Reconciliation interval in seconds")
    reconcile_batch_size: int = Field(default=50, ge=10, le=200, description="Number of databases to process per batch")

    # CORS
    cors_origins: List[str] = Field(
        default=["http://localhost:3000"], description="CORS allowed origins"
    )
    cors_allow_credentials: bool = Field(default=True, description="CORS allow credentials")
    cors_allow_methods: List[str] = Field(default=["*"], description="CORS allowed methods")
    cors_allow_headers: List[str] = Field(default=["*"], description="CORS allowed headers")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Log level must be one of {valid_levels}")
        return v.upper()

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        """Validate environment."""
        valid_envs = ["development", "staging", "production"]
        if v.lower() not in valid_envs:
            raise ValueError(f"Environment must be one of {valid_envs}")
        return v.lower()

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.environment == "development"


# Global settings instance
settings = Settings()
