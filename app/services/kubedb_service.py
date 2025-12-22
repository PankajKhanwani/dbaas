"""
KubeDB Service - Core integration with KubeDB for database provisioning.
Handles lifecycle management of databases through Kubernetes Custom Resources.

Supports multi-cluster mode where each provider can connect to a different Kubernetes cluster.
"""
import asyncio
import base64
import re
import tempfile
import yaml
from typing import Dict, Any, Optional
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException
import aiohttp

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.config.logging import get_logger
from app.config.settings import settings
from app.exceptions import KubeDBError, KubernetesError
from app.models.database import DatabaseEngine, DatabaseSize
from app.utils.retry import retry_on_k8s_error, retry_on_connection_error

logger = get_logger(__name__)


class KubernetesClientSet:
    """Container for Kubernetes API clients."""

    def __init__(
        self,
        api_client: client.ApiClient,
        custom_api: client.CustomObjectsApi,
        core_api: client.CoreV1Api,
        apps_api: client.AppsV1Api,
    ):
        self.api_client = api_client
        self.custom_api = custom_api
        self.core_api = core_api
        self.apps_api = apps_api
        self.storage_api = client.StorageV1Api(api_client)

    async def close(self):
        """Close all API clients."""
        if self.api_client:
            await self.api_client.close()


class KubeDBService:
    """
    Service for managing KubeDB database resources across multiple providers.

    Pure multi-provider architecture:
    - Each provider is onboarded with its own kubeconfig via API
    - Every database operation uses a specific provider selected based on requirements
    - No default or global cluster configuration
    """

    def __init__(self):
        """Initialize KubeDB service with multi-provider support."""
        # Per-provider clients (keyed by provider_id)
        self.provider_clients: Dict[str, KubernetesClientSet] = {}
        # Track last access time for each client (for TTL-based cleanup)
        self._client_last_access: Dict[str, float] = {}
        # Client TTL in seconds (default: 1 hour)
        self._client_ttl_seconds: int = 3600

    def invalidate_provider_cache(self, provider_id: str) -> None:
        """
        Invalidate cached client for a provider.

        Call this when a provider is updated to force recreation of the client
        with the new configuration.

        Args:
            provider_id: Provider ID to invalidate
        """
        if provider_id in self.provider_clients:
            logger.info("invalidating_provider_cache", provider_id=provider_id)
            # Close the existing client
            import asyncio
            try:
                # Try to close if we're in an async context
                asyncio.create_task(self.provider_clients[provider_id].close())
            except RuntimeError:
                # Not in an async context, just remove from cache
                pass
            del self.provider_clients[provider_id]
            # Also remove from last access tracking
            self._client_last_access.pop(provider_id, None)
            logger.info("provider_cache_invalidated", provider_id=provider_id)

    async def _cleanup_expired_clients(self) -> None:
        """
        Remove expired clients from cache based on TTL.

        Clients that haven't been accessed for longer than _client_ttl_seconds
        will be closed and removed from the cache.
        """
        import time
        current_time = time.time()
        expired_provider_ids = []

        for provider_id, last_access in self._client_last_access.items():
            age_seconds = current_time - last_access
            if age_seconds > self._client_ttl_seconds:
                expired_provider_ids.append(provider_id)

        if expired_provider_ids:
            logger.info(
                "cleaning_up_expired_clients",
                count=len(expired_provider_ids),
                ttl_seconds=self._client_ttl_seconds,
            )

            for provider_id in expired_provider_ids:
                if provider_id in self.provider_clients:
                    try:
                        await self.provider_clients[provider_id].close()
                        logger.debug("closed_expired_client", provider_id=provider_id)
                    except Exception as e:
                        logger.warning(
                            "failed_to_close_expired_client",
                            provider_id=provider_id,
                            error=str(e),
                        )
                    del self.provider_clients[provider_id]
                    self._client_last_access.pop(provider_id, None)

            logger.info(
                "expired_clients_cleaned_up",
                count=len(expired_provider_ids),
                remaining_clients=len(self.provider_clients),
            )

    async def _get_default_storage_class(self, client_set: KubernetesClientSet) -> str:
        """
        Get the default storage class from the cluster.

        Returns:
            Name of the default storage class, or None if not found
        """
        try:
            storage_classes = await client_set.storage_api.list_storage_class()

            # Look for storage class with annotation storageclass.kubernetes.io/is-default-class=true
            for sc in storage_classes.items:
                annotations = sc.metadata.annotations or {}
                if annotations.get('storageclass.kubernetes.io/is-default-class') == 'true':
                    logger.info("found_default_storage_class", name=sc.metadata.name)
                    return sc.metadata.name

            # If no default, return the first one
            if storage_classes.items:
                fallback = storage_classes.items[0].metadata.name
                logger.warning("no_default_storage_class_using_first", name=fallback)
                return fallback

            # No storage classes found
            logger.warning("no_storage_classes_found")
            return None

        except Exception as e:
            logger.error("failed_to_get_storage_class", error=str(e))
            return None

    async def _load_kubeconfig_from_provider(
        self, provider_id: str, kubeconfig_content: str, verify_ssl: bool = True
    ) -> KubernetesClientSet:
        """
        Load Kubernetes client from provider's kubeconfig content.

        Args:
            provider_id: Provider ID for logging and caching
            kubeconfig_content: Base64-encoded kubeconfig content (required format)
            verify_ssl: Whether to verify SSL certificates

        Returns:
            KubernetesClientSet for this provider

        Raises:
            KubernetesError: If kubeconfig is invalid or connection fails
        """
        try:
            # Decode base64-encoded kubeconfig (enforced format)
            try:
                decoded_content = base64.b64decode(kubeconfig_content).decode('utf-8')
                logger.info("kubeconfig_decoded_from_base64", provider_id=provider_id)
            except Exception as e:
                # Try as raw YAML as fallback for backward compatibility
                decoded_content = kubeconfig_content
                logger.warning(
                    "kubeconfig_not_base64_encoded",
                    provider_id=provider_id,
                    message="Kubeconfig should be base64 encoded",
                    error=str(e)
                )

            # Validate YAML structure
            try:
                kubeconfig_dict = yaml.safe_load(decoded_content)
                if not isinstance(kubeconfig_dict, dict):
                    raise ValueError("Kubeconfig must be a YAML dictionary")
            except yaml.YAMLError as e:
                raise KubernetesError(f"Invalid kubeconfig YAML: {str(e)}")

            # Write to temporary file (kubernetes_asyncio needs a file path)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(decoded_content)
                temp_kubeconfig_path = f.name

            # Create a new Configuration object for this provider (isolated, not global)
            configuration = client.Configuration()

            # Load configuration from the temp file into this isolated config
            await config.load_kube_config(
                config_file=temp_kubeconfig_path,
                client_configuration=configuration
            )

            # Override SSL verification if disabled for this provider
            if not verify_ssl:
                logger.warning(
                    "ssl_verification_disabled",
                    provider_id=provider_id,
                    message="SSL verification disabled for this provider - use only for clusters with certificate issues"
                )
                configuration.verify_ssl = False

            logger.info(
                "kubernetes_configuration_loaded",
                provider_id=provider_id,
                host=configuration.host,
                verify_ssl=configuration.verify_ssl,
                ssl_ca_cert=configuration.ssl_ca_cert if verify_ssl else "disabled",
                cert_file=configuration.cert_file,
                key_file=configuration.key_file
            )

            # Clean up the kubeconfig temp file (certs are already extracted to other temp files)
            import os
            try:
                os.unlink(temp_kubeconfig_path)
                logger.debug("cleaned_up_temp_kubeconfig", path=temp_kubeconfig_path)
            except Exception as cleanup_error:
                logger.warning(
                    "failed_to_cleanup_temp_kubeconfig",
                    path=temp_kubeconfig_path,
                    error=str(cleanup_error)
                )

            # Create API client with this isolated configuration
            api_client = client.ApiClient(configuration=configuration)
            client_set = KubernetesClientSet(
                api_client=api_client,
                custom_api=client.CustomObjectsApi(api_client),
                core_api=client.CoreV1Api(api_client),
                apps_api=client.AppsV1Api(api_client),
            )
            # Storage API is added in KubernetesClientSet.__init__

            logger.info(
                "provider_kubernetes_client_initialized",
                provider_id=provider_id,
            )

            return client_set

        except Exception as e:
            logger.error(
                "failed_to_load_provider_kubeconfig",
                provider_id=provider_id,
                error=str(e),
                exc_info=True,
            )
            raise KubernetesError(
                f"Failed to load kubeconfig for provider {provider_id}: {str(e)}"
            )


    async def get_client_for_provider(
        self, provider_id: Optional[str], kubeconfig_content: Optional[str] = None
    ) -> KubernetesClientSet:
        """
        Get Kubernetes client for a specific provider.

        Args:
            provider_id: Provider ID (required in multi-provider mode)
            kubeconfig_content: Provider's kubeconfig content (optional - will be fetched from DB if not provided)

        Returns:
            KubernetesClientSet for the provider

        Raises:
            KubernetesError: If client initialization fails or params are invalid
        """
        # Validate provider_id is provided
        if not provider_id:
            raise KubernetesError(
                "provider_id is required - no default cluster in pure multi-provider mode. "
                "Every database must be associated with a provider."
            )

        # Periodically cleanup expired clients (10% of requests to reduce overhead)
        import random
        if random.random() < 0.1:
            await self._cleanup_expired_clients()

        # Check if we already have a client for this provider
        if provider_id in self.provider_clients:
            logger.debug("using_cached_provider_client", provider_id=provider_id)
            # Update last access time
            import time
            self._client_last_access[provider_id] = time.time()
            return self.provider_clients[provider_id]

        # Fetch provider document to get kubeconfig_content and verify_ssl
        from app.repositories.models import Provider
        provider = await Provider.find_one({"_id": provider_id})

        if not provider:
            raise KubernetesError(
                f"Provider {provider_id} not found in database. "
                "Provider must be created before use."
            )

        # Use kubeconfig from provider document if not provided
        if not kubeconfig_content:
            kubeconfig_content = provider.kubeconfig_content

        if not kubeconfig_content:
            raise KubernetesError(
                f"kubeconfig_content is required for provider {provider_id}. "
                "Provider may be missing kubeconfig configuration."
            )

        verify_ssl = provider.verify_ssl if hasattr(provider, 'verify_ssl') else True

        # Load and cache client for this provider
        logger.info("creating_new_provider_client", provider_id=provider_id, verify_ssl=verify_ssl)
        client_set = await self._load_kubeconfig_from_provider(provider_id, kubeconfig_content, verify_ssl)
        self.provider_clients[provider_id] = client_set
        # Track access time for new client
        import time
        self._client_last_access[provider_id] = time.time()

        return client_set

    async def initialize(self, provider_id: Optional[str] = None) -> None:
        """
        Initialize Kubernetes client (backward compatibility method).

        Args:
            provider_id: Optional provider ID for multi-cluster mode
        """
        # In pure multi-provider mode, initialization happens on-demand
        # when get_client_for_provider is called with provider_id and kubeconfig_content
        logger.debug("initialize_called", provider_id=provider_id,
                     message="Pure multi-provider mode - clients initialized on-demand")

    async def close(self) -> None:
        """Close all Kubernetes clients."""
        for provider_id, client_set in self.provider_clients.items():
            await client_set.close()
            logger.info("provider_kubernetes_client_closed", provider_id=provider_id)

        self.provider_clients.clear()
        self._client_last_access.clear()

    async def _patch_auth_secret_with_custom_credentials(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> None:
        """
        Wait for KubeDB to create the auth secret, then patch it with custom credentials.

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            username: Custom username (optional, ignored for Elasticsearch/MariaDB - must be 'elastic'/'root')
            password: Custom password (optional)
            provider_id: Provider ID for multi-cluster mode
            kubeconfig_content: Provider's kubeconfig content
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        import asyncio

        secret_name = f"{name}-auth"

        # Elasticsearch has a hardcoded requirement that username must be "elastic"
        if engine == DatabaseEngine.ELASTICSEARCH:
            if username and username != "elastic":
                logger.warning(
                    "elasticsearch_username_override",
                    provided_username=username,
                    message="Elasticsearch requires username to be 'elastic', ignoring custom username"
                )
            # Don't patch username for Elasticsearch, only password
            username = None

        # MariaDB has a hardcoded requirement that username must be "root"
        if engine == DatabaseEngine.MARIADB:
            if username and username != "root":
                logger.warning(
                    "mariadb_username_override",
                    provided_username=username,
                    message="MariaDB requires username to be 'root', ignoring custom username"
                )
            # Don't patch username for MariaDB, only password
            username = None

        # Wait for KubeDB to create the secret (retry up to 30 seconds)
        max_attempts = 15
        for attempt in range(max_attempts):
            try:
                secret = await client_set.core_api.read_namespaced_secret(
                    name=secret_name,
                    namespace=namespace,
                )

                # Secret exists, now patch it with custom credentials
                patch_data = {}

                if username:
                    patch_data["username"] = base64.b64encode(username.encode()).decode()

                if password:
                    patch_data["password"] = base64.b64encode(password.encode()).decode()

                if patch_data:
                    await client_set.core_api.patch_namespaced_secret(
                        name=secret_name,
                        namespace=namespace,
                        body={"data": patch_data},
                    )

                    logger.info(
                        "auth_secret_patched_with_custom_credentials",
                        secret_name=secret_name,
                        namespace=namespace,
                        username=username or "(not changed)",
                    )

                return

            except ApiException as e:
                if e.status == 404:
                    # Secret not created yet, wait and retry
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(2)
                        continue
                    else:
                        logger.warning(
                            "auth_secret_not_found_after_waiting",
                            secret_name=secret_name,
                            namespace=namespace,
                        )
                        return
                else:
                    logger.error(
                        "auth_secret_patch_failed",
                        secret_name=secret_name,
                        error=e.reason,
                    )
                    raise

    def _get_kubedb_group(self, engine: DatabaseEngine) -> str:
        """Get KubeDB API group for database engine."""
        engine_groups = {
            DatabaseEngine.POSTGRES: "kubedb.com",
            DatabaseEngine.MYSQL: "kubedb.com",
            DatabaseEngine.MARIADB: "kubedb.com",
            DatabaseEngine.MONGODB: "kubedb.com",
            DatabaseEngine.REDIS: "kubedb.com",
            DatabaseEngine.ELASTICSEARCH: "kubedb.com",
        }
        return engine_groups.get(engine, "kubedb.com")

    def _get_kubedb_version(self, engine: DatabaseEngine) -> str:
        """
        Get KubeDB API version for database engine.
        
        KubeDB uses v1alpha2 for most installations.
        Some newer installations may use v1.
        """
        # KubeDB commonly uses v1alpha2 API version
        # v1 is available in newer KubeDB versions
        # Default to v1alpha2 for compatibility
        return "v1alpha2"

    def _get_kubedb_plural(self, engine: DatabaseEngine) -> str:
        """Get KubeDB resource plural name."""
        plurals = {
            DatabaseEngine.POSTGRES: "postgreses",
            DatabaseEngine.MYSQL: "mysqls",
            DatabaseEngine.MARIADB: "mariadbs",
            DatabaseEngine.MONGODB: "mongodbs",
            DatabaseEngine.REDIS: "redises",
            DatabaseEngine.ELASTICSEARCH: "elasticsearches",
        }
        return plurals[engine]

    def _get_kubedb_kind(self, engine: DatabaseEngine) -> str:
        """Get KubeDB resource Kind name."""
        kinds = {
            DatabaseEngine.POSTGRES: "Postgres",
            DatabaseEngine.MYSQL: "MySQL",
            DatabaseEngine.MARIADB: "MariaDB",
            DatabaseEngine.MONGODB: "MongoDB",
            DatabaseEngine.REDIS: "Redis",
            DatabaseEngine.ELASTICSEARCH: "Elasticsearch",
        }
        return kinds[engine]

    def get_default_port(self, engine: DatabaseEngine) -> int:
        """Get default port for database engine."""
        ports = {
            DatabaseEngine.POSTGRES: 5432,
            DatabaseEngine.MYSQL: 3306,
            DatabaseEngine.MARIADB: 3306,
            DatabaseEngine.MONGODB: 27017,
            DatabaseEngine.REDIS: 6379,
            DatabaseEngine.ELASTICSEARCH: 9200,
        }
        return ports.get(engine, 0)

    def _get_resource_limits(self, size: DatabaseSize) -> Dict[str, str]:
        """Get resource requests and limits for database size."""
        size_configs = {
            DatabaseSize.MICRO: {"cpu": "500m", "memory": "1Gi"},
            DatabaseSize.SMALL: {"cpu": "1", "memory": "2Gi"},
            DatabaseSize.MEDIUM: {"cpu": "2", "memory": "4Gi"},
            DatabaseSize.LARGE: {"cpu": "2", "memory": "8Gi"},
            DatabaseSize.XLARGE: {"cpu": "4", "memory": "16Gi"},
            DatabaseSize.XXLARGE: {"cpu": "8", "memory": "32Gi"},
        }
        return size_configs.get(size, size_configs[DatabaseSize.SMALL])

    def _build_postgres_spec(
        self,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        labels: Dict[str, str],
        annotations: Dict[str, str],
        storage_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build PostgreSQL custom resource spec."""
        resources = self._get_resource_limits(size)

        storage_config = {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
        }
        if storage_class:
            storage_config["storageClassName"] = storage_class

        spec = {
            "version": version,
            "replicas": replicas if high_availability else 1,
            "storage": storage_config,
            "podTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": "postgres",
                            "resources": {
                                "requests": resources,
                                "limits": resources,
                            }
                        }
                    ]
                }
            },
            "terminationPolicy": "WipeOut",
        }

        if high_availability:
            spec["standbyMode"] = "Hot"
            spec["streamingMode"] = "Asynchronous"

        return spec

    def _build_mysql_spec(
        self,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        labels: Dict[str, str],
        annotations: Dict[str, str],
        storage_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build MySQL custom resource spec."""
        resources = self._get_resource_limits(size)

        spec = {
            "version": version,
            "replicas": replicas if high_availability else 1,
            "storage": {
                "storageClassName": storage_class or "standard",
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
            },
            "podTemplate": {
                "spec": {
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                }
            },
            "terminationPolicy": "WipeOut",
        }

        if high_availability:
            spec["topology"] = {
                "mode": "GroupReplication",
                "group": {"name": f"{name}-group"},
            }

        return spec

    def _build_mariadb_spec(
        self,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        labels: Dict[str, str],
        annotations: Dict[str, str],
        storage_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build MariaDB custom resource spec."""
        resources = self._get_resource_limits(size)

        storage_config = {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
        }
        if storage_class:
            storage_config["storageClassName"] = storage_class

        spec = {
            "version": version,
            "replicas": replicas if high_availability else 1,
            "storage": storage_config,
            "podTemplate": {
                "spec": {
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                }
            },
            "terminationPolicy": "WipeOut",
        }

        # MariaDB Galera cluster for high availability
        if high_availability and replicas >= 3:
            spec["topology"] = {
                "mode": "GroupReplication",
                "group": {"name": f"{name}-group"},
            }

        return spec

    def _build_mongodb_spec(
        self,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        labels: Dict[str, str],
        annotations: Dict[str, str],
        auth_secret: Optional[str] = None,
        storage_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build MongoDB custom resource spec.

        For 1 replica: Standalone mode (no replicaSet) - single instance, not scalable
        For 2 replicas: Force to 3 (ReplicaSet needs odd number for quorum)
        For 3+ replicas: ReplicaSet mode - scalable and highly available

        Note: Scaling from Standalone (1) to ReplicaSet (3+) requires database recreation.
        """
        resources = self._get_resource_limits(size)

        # Determine topology based on replicas
        # ReplicaSet requires minimum 3 replicas for proper quorum
        if replicas == 1:
            # Standalone mode - single instance, no replicaSet
            effective_replicas = 1
            use_replicaset = False
        elif replicas == 2:
            # ReplicaSet needs odd number for quorum, force to 3
            logger.warning(
                "mongodb_replicas_adjusted",
                requested=2,
                effective=3,
                reason="ReplicaSet requires odd number (minimum 3) for quorum"
            )
            effective_replicas = 3
            use_replicaset = True
        else:
            # 3+ replicas - use replicaSet
            effective_replicas = replicas
            use_replicaset = True

        spec = {
            "version": version,
            "replicas": effective_replicas,
            "storage": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
            },
            "podTemplate": {
                "spec": {
                    # Configure containers with resources and health check delays
                    "containers": [
                        {
                            "name": "mongodb",
                            "resources": {
                                "requests": resources,
                                "limits": resources,
                            },
                            "livenessProbe": {
                                "exec": {
                                    "command": [
                                        "bash",
                                        "-c",
                                        "mongosh admin --host=localhost --quiet --eval \"db.adminCommand('ping').ok\" | grep -q 1"
                                    ]
                                },
                                "initialDelaySeconds": 60,  # Give MongoDB time to initialize
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                                "failureThreshold": 3,
                                "successThreshold": 1,
                            },
                            "readinessProbe": {
                                "exec": {
                                    "command": [
                                        "bash",
                                        "-c",
                                        "mongosh admin --host=localhost --quiet --eval \"db.adminCommand('ping').ok\" | grep -q 1"
                                    ]
                                },
                                "initialDelaySeconds": 30,  # Start checking readiness after 30s
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                                "failureThreshold": 3,
                                "successThreshold": 1,
                            },
                        }
                    ]
                }
            },
            "terminationPolicy": "WipeOut",
        }

        # Only add replicaSet configuration if using replicaSet mode
        if use_replicaset:
            spec["replicaSet"] = {"name": f"{name}-replicaset"}

        # Add storage class if provided
        if storage_class:
            spec["storage"]["storageClassName"] = storage_class

        # Add custom auth secret if provided
        if auth_secret:
            spec["authSecret"] = {"name": auth_secret}

        return spec

    def _build_redis_spec(
        self,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        labels: Dict[str, str],
        annotations: Dict[str, str],
        storage_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build Redis custom resource spec."""
        resources = self._get_resource_limits(size)

        spec = {
            "version": version,
            "replicas": replicas if high_availability else 1,
            "storage": {
                "storageClassName": storage_class or "standard",
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
            },
            "podTemplate": {
                "spec": {
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                }
            },
            "terminationPolicy": "WipeOut",
        }

        # Redis cluster mode for HA
        if high_availability and replicas >= 3:
            spec["mode"] = "Cluster"
            spec["cluster"] = {
                "master": 3,
                "replicas": max(1, replicas // 3),
            }
        elif replicas > 1:
            # Sentinel mode for replication
            spec["mode"] = "Sentinel"
            spec["sentinel"] = {
                "replicas": 3,
            }
        else:
            # Standalone mode
            spec["mode"] = "Standalone"

        return spec

    def _build_elasticsearch_spec(
        self,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        labels: Dict[str, str],
        annotations: Dict[str, str],
        storage_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build Elasticsearch custom resource spec."""
        resources = self._get_resource_limits(size)

        spec = {
            "version": version,
            "replicas": replicas if high_availability else 1,
            "storage": {
                "storageClassName": storage_class or "standard",
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
            },
            "podTemplate": {
                "spec": {
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                }
            },
            "terminationPolicy": "WipeOut",
        }

        # Elasticsearch topology for HA
        if high_availability and replicas >= 3:
            # Use dedicated master, data, and ingest nodes
            spec["topology"] = {
                "master": {
                    "replicas": 3,
                    "storage": {
                        "storageClassName": storage_class or "standard",
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
                    },
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                },
                "data": {
                    "replicas": max(2, replicas - 3),
                    "storage": {
                        "storageClassName": storage_class or "standard",
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
                    },
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                },
                "ingest": {
                    "replicas": 2,
                    "storage": {
                        "storageClassName": storage_class or "standard",
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": f"{storage_gb}Gi"}},
                    },
                    "resources": {
                        "requests": resources,
                        "limits": resources,
                    }
                }
            }
            # Remove top-level replicas when using topology
            del spec["replicas"]
            del spec["storage"]
            del spec["podTemplate"]

        return spec

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(ApiException),
    )
    @retry_on_k8s_error(max_retries=5, initial_delay=2.0, max_delay=30.0)
    async def create_database(
        self,
        engine: DatabaseEngine,
        name: str,
        version: str,
        size: DatabaseSize,
        storage_gb: int,
        replicas: int,
        high_availability: bool,
        namespace: str,
        labels: Optional[Dict[str, str]] = None,
        annotations: Optional[Dict[str, str]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        monitoring_enabled: bool = False,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a database using KubeDB custom resources.

        Args:
            engine: Database engine type
            name: Database name
            version: Database version
            size: Instance size
            storage_gb: Storage size in GB
            replicas: Number of replicas
            high_availability: Enable HA
            namespace: Kubernetes namespace
            labels: Custom labels
            annotations: Custom annotations
            username: Database username (optional, auto-generated if not provided)
            password: Database password (optional, auto-generated if not provided)

        Returns:
            Created resource details

        Raises:
            KubeDBError: If database creation fails
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        # Get default storage class from the cluster
        storage_class = await self._get_default_storage_class(client_set)
        if not storage_class:
            # Try to get any available storage class as fallback
            try:
                storage_classes = await client_set.storage_api.list_storage_class()
                if storage_classes.items:
                    storage_class = storage_classes.items[0].metadata.name
                    logger.warning(
                        "no_default_storage_class_using_first_available",
                        storage_class=storage_class,
                        message="Using first available storage class as fallback"
                    )
                else:
                    logger.warning(
                        "no_storage_class_available",
                        message="MongoDB creation may fail if cluster requires storage class"
                    )
            except Exception as e:
                logger.error(
                    "failed_to_get_fallback_storage_class",
                    error=str(e),
                    message="MongoDB creation may fail if cluster requires storage class"
                )

        labels = labels or {}
        annotations = annotations or {}

        # Build spec based on engine
        # Note: We don't pre-create auth secrets anymore - KubeDB will create them
        # If custom credentials are provided, we'll patch the secret after the database is created
        if engine == DatabaseEngine.POSTGRES:
            spec = self._build_postgres_spec(
                name, version, size, storage_gb, replicas, high_availability, labels, annotations, storage_class
            )
        elif engine == DatabaseEngine.MYSQL:
            spec = self._build_mysql_spec(
                name, version, size, storage_gb, replicas, high_availability, labels, annotations, storage_class
            )
        elif engine == DatabaseEngine.MARIADB:
            spec = self._build_mariadb_spec(
                name, version, size, storage_gb, replicas, high_availability, labels, annotations, storage_class
            )
        elif engine == DatabaseEngine.MONGODB:
            spec = self._build_mongodb_spec(
                name, version, size, storage_gb, replicas, high_availability, labels, annotations,
                auth_secret=None,  # Let KubeDB create the secret
                storage_class=storage_class
            )
        elif engine == DatabaseEngine.REDIS:
            spec = self._build_redis_spec(
                name, version, size, storage_gb, replicas, high_availability, labels, annotations, storage_class
            )
        elif engine == DatabaseEngine.ELASTICSEARCH:
            spec = self._build_elasticsearch_spec(
                name, version, size, storage_gb, replicas, high_availability, labels, annotations, storage_class
            )
        else:
            raise KubeDBError(f"Unsupported database engine: {engine}")

        # Add monitoring configuration if enabled
        if monitoring_enabled:
            spec["monitor"] = {
                "agent": "prometheus.io/builtin",
                "prometheus": {
                    "exporter": {
                        "port": 56790
                    }
                }
            }

        # Build custom resource
        body = {
            "apiVersion": f"{self._get_kubedb_group(engine)}/{self._get_kubedb_version(engine)}",
            "kind": self._get_kubedb_kind(engine),
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": spec,
        }

        try:
            logger.info(
                "creating_kubedb_resource",
                engine=engine,
                name=name,
                namespace=namespace,
                body=body,
            )

            result = await client_set.custom_api.create_namespaced_custom_object(
                group=self._get_kubedb_group(engine),
                version=self._get_kubedb_version(engine),
                namespace=namespace,
                plural=self._get_kubedb_plural(engine),
                body=body,
            )

            logger.info(
                "kubedb_resource_created",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            # If custom credentials were provided, wait for KubeDB to create the secret
            # then patch it with custom credentials (works for MongoDB, PostgreSQL, MySQL, Redis, Elasticsearch)
            # Note: Elasticsearch requires username to be "elastic" - custom username will be ignored
            if username or password:
                await self._patch_auth_secret_with_custom_credentials(
                    engine=engine,
                    name=name,
                    namespace=namespace,
                    username=username,
                    password=password,
                    provider_id=provider_id
                )

            return result

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            logger.error(
                "kubedb_resource_creation_failed",
                engine=engine,
                name=name,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to create database: {e.reason}. Details: {error_body}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(ApiException),
    )
    async def get_database(
        self, engine: DatabaseEngine, name: str, namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get database resource from KubeDB."""

        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            result = await client_set.custom_api.get_namespaced_custom_object(
                group=self._get_kubedb_group(engine),
                version=self._get_kubedb_version(engine),
                namespace=namespace,
                plural=self._get_kubedb_plural(engine),
                name=name,
            )
            return result

        except ApiException as e:
            if e.status == 404:
                return None
            logger.error(
                "kubedb_resource_get_failed",
                engine=engine,
                name=name,
                error=e.reason,
            )
            raise KubeDBError(f"Failed to get database: {e.reason}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(ApiException),
    )
    @retry_on_k8s_error(max_retries=5, initial_delay=2.0, max_delay=30.0)
    async def delete_database(
        self, engine: DatabaseEngine, name: str, namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> bool:
        """
        Delete database resource from KubeDB and clean up all related resources.

        This method:
        1. Deletes the KubeDB CRD
        2. Waits for the operator to clean up managed resources
        3. Explicitly cleans up any remaining resources (secrets, PVCs, services)
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            logger.info(
                "deleting_kubedb_resource",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            # Step 1: Delete the KubeDB CRD
            try:
                await client_set.custom_api.delete_namespaced_custom_object(
                    group=self._get_kubedb_group(engine),
                    version=self._get_kubedb_version(engine),
                    namespace=namespace,
                    plural=self._get_kubedb_plural(engine),
                    name=name,
                )
                logger.info("kubedb_crd_deleted", name=name)
            except ApiException as e:
                if e.status == 404:
                    logger.info("kubedb_crd_already_deleted", name=name)
                else:
                    raise

            # Step 2: Wait a moment for the operator to start cleanup
            await asyncio.sleep(2)

            # Step 3: Explicitly clean up remaining resources
            await self._cleanup_database_resources(name, namespace, engine, provider_id, kubeconfig_content)

            logger.info(
                "kubedb_resource_fully_deleted",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            return True

        except ApiException as e:
            if e.status == 404:
                logger.info(
                    "kubedb_resource_already_deleted",
                    engine=engine,
                    name=name,
                    message="Resource not found, likely already deleted"
                )
                # Still try to clean up any orphaned resources
                try:
                    await self._cleanup_database_resources(name, namespace, engine, provider_id, kubeconfig_content)
                except Exception as cleanup_error:
                    logger.warning("cleanup_after_404_failed", error=str(cleanup_error))
                return True

            logger.error(
                "kubedb_resource_deletion_failed",
                engine=engine,
                name=name,
                error=e.reason,
            )
            raise KubeDBError(f"Failed to delete database: {e.reason}")

    async def _cleanup_database_resources(
        self, name: str, namespace: str, engine: DatabaseEngine,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> None:
        """
        Clean up all resources related to a database.

        Resources cleaned:
        - Auth secrets ({name}-auth)
        - Additional secrets (for Elasticsearch: multiple system credentials)
        - Services ({name}, {name}-pods, {name}-stats)
        - PersistentVolumeClaims (datadir-{name}-*)
        - AppBindings ({name})
        """
        logger.info("cleanup_database_resources_started", name=name, namespace=namespace)

        # Get client for the provider
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        cleanup_errors = []

        # 1. Delete auth secrets
        secret_names = [f"{name}-auth", f"{name}-key"]

        # For Elasticsearch, there are additional secrets
        if engine == DatabaseEngine.ELASTICSEARCH:
            secret_names.extend([
                f"{name}-apm-system-cred",
                f"{name}-beats-system-cred",
                f"{name}-kibana-system-cred",
                f"{name}-logstash-system-cred",
                f"{name}-remote-monitoring-user-cred",
            ])

        for secret_name in secret_names:
            try:
                await client_set.core_api.delete_namespaced_secret(
                    name=secret_name,
                    namespace=namespace,
                )
                logger.info("secret_deleted", secret=secret_name)
            except ApiException as e:
                if e.status != 404:
                    logger.warning("secret_deletion_failed", secret=secret_name, error=e.reason)
                    cleanup_errors.append(f"Secret {secret_name}: {e.reason}")

        # 2. Delete services
        service_names = [
            name,
            f"{name}-pods",
            f"{name}-stats",
            f"{name}-primary",  # For PostgreSQL
            f"{name}-standby",  # For PostgreSQL HA
        ]

        for service_name in service_names:
            try:
                await client_set.core_api.delete_namespaced_service(
                    name=service_name,
                    namespace=namespace,
                )
                logger.info("service_deleted", service=service_name)
            except ApiException as e:
                if e.status != 404:
                    logger.warning("service_deletion_failed", service=service_name, error=e.reason)
                    cleanup_errors.append(f"Service {service_name}: {e.reason}")

        # 3. Delete PVCs with label selector
        try:
            label_selector = f"app.kubernetes.io/instance={name}"
            pvcs = await client_set.core_api.list_namespaced_persistent_volume_claim(
                namespace=namespace,
                label_selector=label_selector,
            )

            for pvc in pvcs.items:
                try:
                    await client_set.core_api.delete_namespaced_persistent_volume_claim(
                        name=pvc.metadata.name,
                        namespace=namespace,
                    )
                    logger.info("pvc_deleted", pvc=pvc.metadata.name)
                except ApiException as e:
                    if e.status != 404:
                        logger.warning("pvc_deletion_failed", pvc=pvc.metadata.name, error=e.reason)
                        cleanup_errors.append(f"PVC {pvc.metadata.name}: {e.reason}")
        except ApiException as e:
            logger.warning("pvc_list_failed", error=e.reason)
            cleanup_errors.append(f"List PVCs: {e.reason}")

        # 4. Delete AppBinding (KubeDB creates these for connection info)
        try:
            await client_set.custom_api.delete_namespaced_custom_object(
                group="appcatalog.appscode.com",
                version="v1alpha1",
                namespace=namespace,
                plural="appbindings",
                name=name,
            )
            logger.info("appbinding_deleted", name=name)
        except ApiException as e:
            if e.status != 404:
                logger.warning("appbinding_deletion_failed", name=name, error=e.reason)
                cleanup_errors.append(f"AppBinding {name}: {e.reason}")

        # 5. Delete StatefulSet (if operator didn't clean it up)
        try:
            await client_set.apps_api.delete_namespaced_stateful_set(
                name=name,
                namespace=namespace,
            )
            logger.info("statefulset_deleted", name=name)
        except ApiException as e:
            if e.status != 404:
                logger.warning("statefulset_deletion_failed", name=name, error=e.reason)
                cleanup_errors.append(f"StatefulSet {name}: {e.reason}")

        if cleanup_errors:
            logger.warning(
                "cleanup_completed_with_errors",
                name=name,
                errors_count=len(cleanup_errors),
                errors=cleanup_errors,
            )
        else:
            logger.info("cleanup_completed_successfully", name=name)

    @retry_on_k8s_error(max_retries=3, initial_delay=1.0, max_delay=10.0)
    async def get_database_status(
        self, engine: DatabaseEngine, name: str, namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get current status of database resource.

        Returns the actual phase from KubeDB resource status.
        """
        resource = await self.get_database(engine, name, namespace)
        if not resource:
            return None

        return resource.get("status", {}).get("phase", "Unknown")

    async def get_detailed_status(
        self, engine: DatabaseEngine, name: str, namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get detailed status information from KubeDB resource.

        Returns:
            Dictionary with phase, conditions, ready status, and replica counts
        """
        resource = await self.get_database(engine, name, namespace, provider_id, kubeconfig_content)
        if not resource:
            return None
        status = resource.get("status", {})
        spec = resource.get("spec", {})

        # Get replica counts
        desired_replicas = spec.get("replicas", 1)

        # Try multiple field names for ready replicas (different engines use different names)
        ready_replicas = status.get("readyReplicas") or status.get("replicas")

        # If not in KubeDB status, check the PetSet (KubeDB's StatefulSet replacement)
        if ready_replicas is None:
            try:
                client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)
                # Query PetSet (KubeDB's custom resource for managing pods)
                petset = await client_set.custom_api.get_namespaced_custom_object(
                    group="apps.k8s.appscode.com",
                    version="v1",
                    namespace=namespace,
                    plural="petsets",
                    name=name
                )
                ready_replicas = petset.get("status", {}).get("readyReplicas", 0)
                logger.info(
                    "replica_count_from_petset",
                    name=name,
                    desired=petset.get("spec", {}).get("replicas", 1),
                    ready=ready_replicas,
                )
            except Exception as e:
                # 404 is expected for new/deleted databases - PetSet may not exist yet
                error_str = str(e)
                if "404" in error_str or "Not Found" in error_str:
                    logger.debug(
                        "petset_not_found",
                        name=name,
                        message="PetSet not created yet or already deleted",
                    )
                else:
                    logger.warning(
                        "failed_to_get_petset_replicas",
                        name=name,
                        error=error_str,
                    )
                ready_replicas = 0
        else:
            ready_replicas = int(ready_replicas)

        # Get recent events for better error visibility
        events = await self._get_resource_events(name, namespace, provider_id=provider_id)

        # Extract version from spec (version field in KubeDB resources)
        # Version is typically at spec.version for most KubeDB engines
        version = spec.get("version")
        
        # Log version extraction for debugging
        if version:
            logger.debug(
                "version_extracted_from_kubedb_resource",
                name=name,
                namespace=namespace,
                engine=engine.value if hasattr(engine, 'value') else engine,
                version=version,
            )
        else:
            logger.warning(
                "version_not_found_in_kubedb_resource",
                name=name,
                namespace=namespace,
                engine=engine.value if hasattr(engine, 'value') else engine,
                spec_keys=list(spec.keys()) if spec else [],
            )
        
        return {
            "phase": status.get("phase", "Unknown"),
            "conditions": status.get("conditions", []),
            "events": events,
            "observedGeneration": status.get("observedGeneration"),
            "ready": self._is_database_ready(status),
            "replicas": desired_replicas,
            "ready_replicas": ready_replicas,
            "version": version,  # Current version from KubeDB resource
        }

    async def _get_resource_events(
        self, resource_name: str, namespace: str, limit: int = 5,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> list:
        """
        Get recent Kubernetes events for a resource.

        Args:
            resource_name: Name of the resource
            namespace: Namespace
            limit: Maximum number of events to return

        Returns:
            List of recent events (warnings and errors)
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            # List events in the namespace
            events = await client_set.core_api.list_namespaced_event(namespace=namespace)

            # Filter events related to this resource
            relevant_events = []
            for event in events.items:
                if (hasattr(event.involved_object, 'name') and
                    resource_name in event.involved_object.name):
                    relevant_events.append({
                        "type": event.type,
                        "reason": event.reason,
                        "message": event.message,
                        "count": event.count,
                        "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                        "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
                    })

            # Sort by last timestamp and return most recent
            relevant_events.sort(
                key=lambda x: x.get("last_timestamp") or "",
                reverse=True
            )

            return relevant_events[:limit]

        except Exception as e:
            logger.warning(
                "failed_to_fetch_events",
                resource=resource_name,
                error=str(e)
            )
            return []

    def _is_database_ready(self, status: Dict[str, Any]) -> bool:
        """
        Check if database is ready based on status conditions.

        Args:
            status: KubeDB resource status dict

        Returns:
            True if database is ready to accept connections
        """
        phase = status.get("phase", "").lower()

        # Check if phase indicates readiness
        if phase in ["ready", "running"]:
            return True

        # Check conditions for Ready type
        conditions = status.get("conditions", [])
        for condition in conditions:
            if condition.get("type") == "Ready" and condition.get("status") == "True":
                return True

        return False

    async def get_database_endpoint(
        self, engine: DatabaseEngine, name: str, namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get database connection endpoint with actual IP address.

        Priority:
        1. LoadBalancer External IP (for production/public access)
        2. ClusterIP (for internal access)

        Returns IP address instead of DNS name for cleaner UI.
        """
        # Get client for the provider
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        resource = await self.get_database(engine, name, namespace, provider_id, kubeconfig_content)
        if not resource:
            return None

        # KubeDB creates a service with the same name as the database
        try:
            service = await client_set.core_api.read_namespaced_service(name, namespace)

            if not service.spec.ports:
                return None

            port = service.spec.ports[0].port
            host = None

            # Priority 1: Check for LoadBalancer external IP (production)
            if service.spec.type == "LoadBalancer" and service.status.load_balancer:
                ingress_list = service.status.load_balancer.ingress
                if ingress_list and len(ingress_list) > 0:
                    ingress = ingress_list[0]
                    # External IP or hostname
                    host = ingress.ip or ingress.hostname
                    if host:
                        logger.info(
                            "database_endpoint_loadbalancer",
                            name=name,
                            namespace=namespace,
                            host=host,
                            port=port,
                            type="LoadBalancer"
                        )

            # Priority 2: Use ClusterIP (internal access)
            if not host and service.spec.cluster_ip:
                host = service.spec.cluster_ip
                logger.info(
                    "database_endpoint_clusterip",
                    name=name,
                    namespace=namespace,
                    host=host,
                    port=port,
                    type="ClusterIP"
                )

            if host:
                return {
                    "host": host,
                    "port": port,
                }

            return None

        except ApiException as e:
            logger.warning(
                "failed_to_get_database_endpoint",
                name=name,
                namespace=namespace,
                error=str(e)
            )
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(ApiException),
    )
    async def patch_database(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        replicas: Optional[int] = None,
        storage_gb: Optional[int] = None,
        size: Optional[DatabaseSize] = None,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Patch/update a KubeDB database resource using production-grade OpsRequest approach.

        Production-grade scaling strategy:
        - Vertical scaling (CPU/memory): Uses OpsRequest for controlled, safe updates
        - Horizontal scaling (replicas): Uses OpsRequest for controlled scaling
        - Storage expansion: Uses direct patch (handled by Kubernetes PVC expansion)
        - Combined operations: Single OpsRequest when both replicas and size change

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            replicas: New replica count (triggers OpsRequest if provided)
            storage_gb: New storage size in GB (direct patch, can only increase)
            size: New instance size (triggers OpsRequest if provided)
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            OpsRequest details if scaling operation, or updated resource if storage-only

        Raises:
            KubeDBError: If patch operation fails
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        # Validate inputs
        if replicas is not None and replicas < 1:
            raise KubeDBError(f"Invalid replica count: {replicas}. Must be >= 1")
        
        if storage_gb is not None and storage_gb < 1:
            raise KubeDBError(f"Invalid storage size: {storage_gb}GB. Must be >= 1")

        # Check if we need scaling operations (replicas or size)
        needs_scaling_ops = replicas is not None or size is not None

        # Handle storage expansion separately (direct patch, no OpsRequest needed)
        if storage_gb is not None:
            try:
                # Get current CR to validate storage can only increase
                current_cr = await client_set.custom_api.get_namespaced_custom_object(
                    group=self._get_kubedb_group(engine),
                    version=self._get_kubedb_version(engine),
                    namespace=namespace,
                    plural=self._get_kubedb_plural(engine),
                    name=name,
                )
                
                current_storage = current_cr.get("spec", {}).get("storage", {}).get("resources", {}).get("requests", {}).get("storage", "")
                current_storage_gb = 0
                if current_storage:
                    try:
                        # Parse storage (e.g., "20Gi" -> 20)
                        current_storage_gb = int(current_storage.replace("Gi", "").replace("G", ""))
                    except (ValueError, AttributeError, TypeError) as e:
                        logger.warning(
                            "storage_parse_failed_during_expansion",
                            storage_str=current_storage,
                            error=str(e),
                        )
                
                if storage_gb < current_storage_gb:
                    raise KubeDBError(
                        f"Cannot decrease storage from {current_storage_gb}GB to {storage_gb}GB. "
                        "Storage can only be increased."
                    )

                # Patch storage directly (Kubernetes handles PVC expansion)
                patch_body = {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/managed-by": "kubedb-dbaas"
                        }
                    },
                    "spec": {
                        "storage": {
                            "resources": {
                                "requests": {
                                    "storage": f"{storage_gb}Gi"
                                }
                            }
                        }
                    }
                }

                logger.info(
                    "expanding_storage",
                    name=name,
                    engine=engine,
                    current_storage_gb=current_storage_gb,
                    new_storage_gb=storage_gb,
                )

                await client_set.custom_api.patch_namespaced_custom_object(
                    group=self._get_kubedb_group(engine),
                    version=self._get_kubedb_version(engine),
                    namespace=namespace,
                    plural=self._get_kubedb_plural(engine),
                    name=name,
                    body=patch_body,
                    _content_type="application/merge-patch+json",
                )

                logger.info(
                    "storage_expansion_initiated",
                    name=name,
                    engine=engine,
                    new_storage_gb=storage_gb,
                    message="Kubernetes will handle PVC expansion"
                )

            except ApiException as e:
                error_body = None
                try:
                    import json
                    error_body = json.loads(e.body) if e.body else None
                except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                    logger.debug("api_error_body_parse_failed", error=str(parse_error))
                    error_body = e.body
                logger.error(
                    "storage_expansion_failed",
                    engine=engine,
                    name=name,
                    error=e.reason,
                    status=e.status,
                    error_body=error_body,
                )
                raise KubeDBError(f"Failed to expand storage: {e.reason}. Details: {error_body}")

        # Handle scaling operations using OpsRequest (production-grade approach)
        if needs_scaling_ops:
            try:
                # Create combined OpsRequest if both replicas and size are changing
                # Otherwise create single-type OpsRequest
                if replicas is not None and size is not None:
                    # Combined horizontal + vertical scaling
                    ops_request = await self.create_combined_scaling_ops_request(
                        engine=engine,
                        name=name,
                        namespace=namespace,
                        replicas=replicas,
                        size=size,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                elif replicas is not None:
                    # Horizontal scaling only
                    ops_request = await self.create_horizontal_scaling_ops_request(
                        engine=engine,
                        name=name,
                        namespace=namespace,
                        replicas=replicas,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                elif size is not None:
                    # Vertical scaling only
                    ops_request = await self.create_vertical_scaling_ops_request(
                        engine=engine,
                        name=name,
                        namespace=namespace,
                        size=size,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )

                ops_request_name = ops_request["metadata"]["name"]
                ops_type = ops_request["spec"]["type"]

                logger.info(
                    "scaling_ops_request_created",
                    engine=engine,
                    name=name,
                    namespace=namespace,
                    ops_request_name=ops_request_name,
                    ops_type=ops_type,
                    replicas=replicas,
                    size=size.value if size else None,
                    message="Reconciler will monitor OpsRequest progress"
                )

                # Return OpsRequest details (fire-and-forget approach)
                # Reconciler will monitor status and update database accordingly
                return {
                    "ops_request_name": ops_request_name,
                    "ops_request_uid": ops_request["metadata"]["uid"],
                    "ops_type": ops_type,
                    "message": "OpsRequest created, reconciler will monitor progress",
                    "replicas": replicas,
                    "size": size.value if size else None,
                }

            except KubeDBError:
                # Re-raise KubeDBError as-is
                raise
            except Exception as e:
                logger.error(
                    "scaling_operation_failed",
                    name=name,
                    engine=engine,
                    replicas=replicas,
                    size=size.value if size else None,
                    error=str(e),
                    exc_info=True,
                )
                raise KubeDBError(f"Failed to create scaling OpsRequest: {str(e)}")

        # If only storage was updated (no scaling), return updated resource
        if storage_gb is not None and not needs_scaling_ops:
            return await self.get_database(engine, name, namespace, provider_id, kubeconfig_content)

        # No changes requested
        logger.warning("patch_database_called_with_no_changes", name=name)
        return await self.get_database(engine, name, namespace, provider_id, kubeconfig_content)

    def _get_ops_request_plural(self, engine: DatabaseEngine) -> str:
        """Get OpsRequest resource plural name for database engine."""
        plurals = {
            DatabaseEngine.POSTGRES: "postgresopsrequests",
            DatabaseEngine.MYSQL: "mysqlopsrequests",
            DatabaseEngine.MARIADB: "mariadbopsrequests",
            DatabaseEngine.MONGODB: "mongodbopsrequests",
            DatabaseEngine.REDIS: "redisopsrequests",
            DatabaseEngine.ELASTICSEARCH: "elasticsearchopsrequests",
        }
        return plurals[engine]

    def _get_ops_request_kind(self, engine: DatabaseEngine) -> str:
        """Get OpsRequest resource Kind name for database engine."""
        kinds = {
            DatabaseEngine.POSTGRES: "PostgresOpsRequest",
            DatabaseEngine.MYSQL: "MySQLOpsRequest",
            DatabaseEngine.MARIADB: "MariaDBOpsRequest",
            DatabaseEngine.MONGODB: "MongoDBOpsRequest",
            DatabaseEngine.REDIS: "RedisOpsRequest",
            DatabaseEngine.ELASTICSEARCH: "ElasticsearchOpsRequest",
        }
        return kinds[engine]

    async def create_vertical_scaling_ops_request(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        size: DatabaseSize,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an OpsRequest for vertical scaling (CPU/memory update).

        This is the proper KubeDB way to update resources. The Ops Manager will:
        1. Halt the database to prevent conflicts
        2. Update PetSet resources
        3. Perform rolling restart of pods gracefully
        4. Resume database when complete

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            size: New instance size (CPU/memory configuration)
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            Created OpsRequest details

        Raises:
            KubeDBError: If OpsRequest creation fails
        """
        import time

        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)
        resources = self._get_resource_limits(size)

        # Build OpsRequest spec based on database topology
        ops_request_name = f"{name}-scale-{int(time.time())}"

        # Build vertical scaling spec based on engine
        vertical_scaling_spec = {
            "resources": {
                "requests": resources,
                "limits": resources,
            }
        }

        # MongoDB uses replicaSet topology
        if engine == DatabaseEngine.MONGODB:
            vertical_scaling = {
                "replicaSet": vertical_scaling_spec
            }
        # PostgreSQL uses standalone/cluster topology
        elif engine == DatabaseEngine.POSTGRES:
            vertical_scaling = {
                "postgres": vertical_scaling_spec
            }
        # MySQL uses standalone/cluster topology
        elif engine == DatabaseEngine.MYSQL:
            vertical_scaling = {
                "mysql": vertical_scaling_spec
            }
        # MariaDB uses standalone/cluster topology
        elif engine == DatabaseEngine.MARIADB:
            vertical_scaling = {
                "mariadb": vertical_scaling_spec
            }
        # Redis uses standalone/cluster topology
        elif engine == DatabaseEngine.REDIS:
            vertical_scaling = {
                "redis": vertical_scaling_spec
            }
        # Elasticsearch uses cluster topology
        elif engine == DatabaseEngine.ELASTICSEARCH:
            vertical_scaling = {
                "node": vertical_scaling_spec
            }
        else:
            # Default to replicaSet for unknown engines
            vertical_scaling = {
                "replicaSet": vertical_scaling_spec
            }

        ops_request_body = {
            "apiVersion": "ops.kubedb.com/v1alpha1",
            "kind": self._get_ops_request_kind(engine),
            "metadata": {
                "name": ops_request_name,
                "namespace": namespace,
            },
            "spec": {
                "type": "VerticalScaling",
                "databaseRef": {
                    "name": name
                },
                "verticalScaling": vertical_scaling
            }
        }

        try:
            logger.info(
                "creating_ops_request_for_vertical_scaling",
                engine=engine,
                name=name,
                namespace=namespace,
                ops_request_name=ops_request_name,
                size=size.value,
                resources=resources,
            )

            result = await client_set.custom_api.create_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=namespace,
                plural=self._get_ops_request_plural(engine),
                body=ops_request_body,
            )

            logger.info(
                "ops_request_created",
                engine=engine,
                name=name,
                ops_request_name=ops_request_name,
                uid=result.get("metadata", {}).get("uid"),
            )

            return result

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            logger.error(
                "ops_request_creation_failed",
                engine=engine,
                name=name,
                ops_request_name=ops_request_name,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to create OpsRequest: {e.reason}. Details: {error_body}")

    async def delete_ops_request(
        self,
        engine: DatabaseEngine,
        ops_request_name: str,
        namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> bool:
        """
        Delete an OpsRequest after completion.

        Args:
            engine: Database engine type
            ops_request_name: Name of the OpsRequest to delete
            namespace: Kubernetes namespace
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            True if deleted successfully, False otherwise

        Raises:
            KubeDBError: If deletion fails critically
        """
        try:
            client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

            logger.info(
                "deleting_ops_request",
                engine=engine,
                ops_request_name=ops_request_name,
                namespace=namespace,
            )

            await client_set.custom_api.delete_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=namespace,
                plural=self._get_ops_request_plural(engine),
                name=ops_request_name,
            )

            logger.info(
                "ops_request_deleted",
                engine=engine,
                ops_request_name=ops_request_name,
                namespace=namespace,
            )

            return True

        except ApiException as e:
            if e.status == 404:
                # Already deleted or never existed
                logger.warning(
                    "ops_request_not_found_skipping_delete",
                    ops_request_name=ops_request_name,
                    namespace=namespace,
                )
                return False

            logger.error(
                "ops_request_deletion_failed",
                ops_request_name=ops_request_name,
                namespace=namespace,
                error=e.reason,
                status=e.status,
            )
            # Don't raise error, just log and return False
            # We don't want operation to fail just because cleanup failed
            return False

    async def check_and_delete_stuck_pods(
        self,
        database_name: str,
        namespace: str,
        stuck_threshold_seconds: int = 300,  # 5 minutes default
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> int:
        """
        Check for pods stuck in Init/Pending state and delete them.

        Args:
            database_name: Name of the database (used for pod selector)
            namespace: Kubernetes namespace
            stuck_threshold_seconds: How long a pod can be stuck before deletion (default: 300s = 5min)
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            Number of stuck pods deleted

        Raises:
            KubeDBError: If pod listing or deletion fails critically
        """
        try:
            from datetime import datetime, timezone

            client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

            # List pods for this database
            label_selector = f"app.kubernetes.io/instance={database_name}"

            pods = await client_set.core_api.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )

            stuck_pods_deleted = 0
            current_time = datetime.now(timezone.utc)

            for pod in pods.items:
                pod_name = pod.metadata.name
                phase = pod.status.phase
                start_time = pod.status.start_time

                # Check if pod is stuck in Init or Pending phase
                if phase in ["Pending", "Init"] and start_time:
                    # Calculate how long pod has been in this state
                    elapsed_seconds = (current_time - start_time).total_seconds()

                    if elapsed_seconds > stuck_threshold_seconds:
                        logger.warning(
                            "deleting_stuck_pod",
                            pod_name=pod_name,
                            phase=phase,
                            elapsed_seconds=int(elapsed_seconds),
                            threshold_seconds=stuck_threshold_seconds,
                            database=database_name,
                            namespace=namespace,
                        )

                        try:
                            # Delete the stuck pod
                            await client_set.core_api.delete_namespaced_pod(
                                name=pod_name, namespace=namespace
                            )

                            logger.info(
                                "stuck_pod_deleted",
                                pod_name=pod_name,
                                phase=phase,
                                elapsed_seconds=int(elapsed_seconds),
                                database=database_name,
                                namespace=namespace,
                            )

                            stuck_pods_deleted += 1

                        except ApiException as e:
                            if e.status == 404:
                                # Pod already deleted
                                logger.debug(
                                    "stuck_pod_already_deleted",
                                    pod_name=pod_name,
                                    database=database_name,
                                )
                            else:
                                logger.error(
                                    "stuck_pod_deletion_failed",
                                    pod_name=pod_name,
                                    error=e.reason,
                                    status=e.status,
                                    database=database_name,
                                )

            if stuck_pods_deleted > 0:
                logger.info(
                    "stuck_pods_cleanup_completed",
                    pods_deleted=stuck_pods_deleted,
                    database=database_name,
                    namespace=namespace,
                )

            return stuck_pods_deleted

        except ApiException as e:
            logger.error(
                "stuck_pod_check_failed",
                database=database_name,
                namespace=namespace,
                error=e.reason,
                status=e.status,
            )
            # Don't raise - we don't want reconciliation to fail just because pod check failed
            return 0
        except Exception as e:
            logger.error(
                "stuck_pod_check_error",
                database=database_name,
                namespace=namespace,
                error=str(e),
            )
            return 0

    async def create_horizontal_scaling_ops_request(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        replicas: int,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an OpsRequest for horizontal scaling (replica count update).

        Production-grade approach that:
        1. Validates replica count
        2. Creates OpsRequest for controlled scaling
        3. Provides status tracking and error handling

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            replicas: New replica count
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            Created OpsRequest details

        Raises:
            KubeDBError: If OpsRequest creation fails
        """
        import time

        if replicas < 1:
            raise KubeDBError(f"Invalid replica count: {replicas}. Must be >= 1")

        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        # Build OpsRequest spec based on database topology
        ops_request_name = f"{name}-scale-{int(time.time())}"

        # Build horizontal scaling spec based on engine
        # For MongoDB replica sets, use top-level replicas field
        # (not nested under replicaSet - that's not in the CRD schema!)
        logger.debug(
            "building_horizontal_scaling_spec",
            engine=engine,
            engine_type=type(engine),
            is_mongodb=engine == DatabaseEngine.MONGODB,
            replicas=replicas,
        )
        if engine == DatabaseEngine.MONGODB:
            horizontal_scaling = {
                "replicas": replicas  # Top-level replicas field for replica sets
            }
            logger.debug("mongodb_horizontal_scaling_built", horizontal_scaling=horizontal_scaling)
        # PostgreSQL uses standalone/cluster topology
        elif engine == DatabaseEngine.POSTGRES:
            horizontal_scaling = {
                "replicas": replicas  # Top-level for postgres
            }
        # MySQL uses standalone/cluster topology
        elif engine == DatabaseEngine.MYSQL:
            horizontal_scaling = {
                "replicas": replicas  # Top-level for mysql
            }
        # MariaDB uses standalone/cluster topology
        elif engine == DatabaseEngine.MARIADB:
            horizontal_scaling = {
                "replicas": replicas  # Top-level for mariadb
            }
        # Redis uses standalone/cluster topology
        elif engine == DatabaseEngine.REDIS:
            horizontal_scaling = {
                "replicas": replicas  # Top-level for redis
            }
        # Elasticsearch uses cluster topology
        elif engine == DatabaseEngine.ELASTICSEARCH:
            horizontal_scaling = {
                "replicas": replicas  # Top-level for elasticsearch
            }
        else:
            # Default to top-level replicas
            horizontal_scaling = {
                "replicas": replicas
            }

        ops_request_body = {
            "apiVersion": "ops.kubedb.com/v1alpha1",
            "kind": self._get_ops_request_kind(engine),
            "metadata": {
                "name": ops_request_name,
                "namespace": namespace,
            },
            "spec": {
                "type": "HorizontalScaling",
                "databaseRef": {
                    "name": name
                },
                "horizontalScaling": horizontal_scaling
            }
        }

        logger.debug(
            "ops_request_body_built",
            horizontal_scaling_field=ops_request_body["spec"]["horizontalScaling"],
            full_spec=ops_request_body["spec"],
        )

        try:
            import json
            logger.info(
                "creating_ops_request_for_horizontal_scaling",
                engine=engine,
                name=name,
                namespace=namespace,
                ops_request_name=ops_request_name,
                replicas=replicas,
            )

            logger.debug(
                "sending_to_kubernetes_api",
                body_json=json.dumps(ops_request_body, indent=2),
            )

            result = await client_set.custom_api.create_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=namespace,
                plural=self._get_ops_request_plural(engine),
                body=ops_request_body,
            )

            logger.debug(
                "received_from_kubernetes_api",
                result_horizontal_scaling=result.get("spec", {}).get("horizontalScaling"),
            )

            logger.info(
                "horizontal_scaling_ops_request_created",
                engine=engine,
                name=name,
                ops_request_name=ops_request_name,
                uid=result.get("metadata", {}).get("uid"),
                replicas=replicas,
            )

            return result

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            logger.error(
                "horizontal_scaling_ops_request_creation_failed",
                engine=engine,
                name=name,
                ops_request_name=ops_request_name,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to create horizontal scaling OpsRequest: {e.reason}. Details: {error_body}")

    async def create_combined_scaling_ops_request(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        replicas: int,
        size: DatabaseSize,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an OpsRequest for combined horizontal and vertical scaling.

        Production-grade approach that performs both operations atomically:
        1. Scales replicas (horizontal)
        2. Updates CPU/memory (vertical)
        3. All in a single coordinated operation

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            replicas: New replica count
            size: New instance size (CPU/memory configuration)
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            Created OpsRequest details

        Raises:
            KubeDBError: If OpsRequest creation fails
        """
        import time

        if replicas < 1:
            raise KubeDBError(f"Invalid replica count: {replicas}. Must be >= 1")

        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)
        resources = self._get_resource_limits(size)

        # Build OpsRequest spec based on database topology
        ops_request_name = f"{name}-scale-{int(time.time())}"

        # Build combined scaling spec
        vertical_scaling_spec = {
            "resources": {
                "requests": resources,
                "limits": resources,
            }
        }

        horizontal_scaling_spec = {
            "replicas": replicas
        }

        # MongoDB uses replicaSet topology
        if engine == DatabaseEngine.MONGODB:
            vertical_scaling = {
                "replicaSet": vertical_scaling_spec
            }
            horizontal_scaling = {
                "replicaSet": horizontal_scaling_spec
            }
        # PostgreSQL uses standalone/cluster topology
        elif engine == DatabaseEngine.POSTGRES:
            vertical_scaling = {
                "postgres": vertical_scaling_spec
            }
            horizontal_scaling = {
                "postgres": horizontal_scaling_spec
            }
        # MySQL uses standalone/cluster topology
        elif engine == DatabaseEngine.MYSQL:
            vertical_scaling = {
                "mysql": vertical_scaling_spec
            }
            horizontal_scaling = {
                "mysql": horizontal_scaling_spec
            }
        # MariaDB uses standalone/cluster topology
        elif engine == DatabaseEngine.MARIADB:
            vertical_scaling = {
                "mariadb": vertical_scaling_spec
            }
            horizontal_scaling = {
                "mariadb": horizontal_scaling_spec
            }
        # Redis uses standalone/cluster topology
        elif engine == DatabaseEngine.REDIS:
            vertical_scaling = {
                "redis": vertical_scaling_spec
            }
            horizontal_scaling = {
                "redis": horizontal_scaling_spec
            }
        # Elasticsearch uses cluster topology
        elif engine == DatabaseEngine.ELASTICSEARCH:
            vertical_scaling = {
                "node": vertical_scaling_spec
            }
            horizontal_scaling = {
                "node": horizontal_scaling_spec
            }
        else:
            # Default to replicaSet for unknown engines
            vertical_scaling = {
                "replicaSet": vertical_scaling_spec
            }
            horizontal_scaling = {
                "replicaSet": horizontal_scaling_spec
            }

        # Note: KubeDB OpsRequest supports both verticalScaling and horizontalScaling
        # in the same request, but the type should be one of them.
        # We'll use "VerticalScaling" as the type and include both specs.
        # Some KubeDB versions may require separate OpsRequests - check your version.
        ops_request_body = {
            "apiVersion": "ops.kubedb.com/v1alpha1",
            "kind": self._get_ops_request_kind(engine),
            "metadata": {
                "name": ops_request_name,
                "namespace": namespace,
            },
            "spec": {
                "type": "VerticalScaling",  # Primary type, but we include both
                "databaseRef": {
                    "name": name
                },
                "verticalScaling": vertical_scaling,
                "horizontalScaling": horizontal_scaling,
            }
        }

        try:
            logger.info(
                "creating_combined_scaling_ops_request",
                engine=engine,
                name=name,
                namespace=namespace,
                ops_request_name=ops_request_name,
                replicas=replicas,
                size=size.value,
                resources=resources,
            )

            result = await client_set.custom_api.create_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=namespace,
                plural=self._get_ops_request_plural(engine),
                body=ops_request_body,
            )

            logger.info(
                "combined_scaling_ops_request_created",
                engine=engine,
                name=name,
                ops_request_name=ops_request_name,
                uid=result.get("metadata", {}).get("uid"),
                replicas=replicas,
                size=size.value,
            )

            return result

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            # If combined OpsRequest fails, fall back to creating separate OpsRequests
            # This handles KubeDB versions that don't support combined operations
            if e.status == 400 or "invalid" in str(error_body).lower():
                logger.warning(
                    "combined_ops_request_not_supported_falling_back",
                    engine=engine,
                    name=name,
                    error=e.reason,
                    message="Creating separate OpsRequests for horizontal and vertical scaling"
                )
                
                # Create horizontal scaling OpsRequest first
                horizontal_ops = await self.create_horizontal_scaling_ops_request(
                    engine=engine,
                    name=name,
                    namespace=namespace,
                    replicas=replicas,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                
                # Then create vertical scaling OpsRequest
                vertical_ops = await self.create_vertical_scaling_ops_request(
                    engine=engine,
                    name=name,
                    namespace=namespace,
                    size=size,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                
                # Return the vertical scaling OpsRequest (last one created)
                # Both will be monitored by the reconciler
                logger.info(
                    "separate_scaling_ops_requests_created",
                    engine=engine,
                    name=name,
                    horizontal_ops=horizontal_ops["metadata"]["name"],
                    vertical_ops=vertical_ops["metadata"]["name"],
                )
                return vertical_ops

            logger.error(
                "combined_scaling_ops_request_creation_failed",
                engine=engine,
                name=name,
                ops_request_name=ops_request_name,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to create combined scaling OpsRequest: {e.reason}. Details: {error_body}")

    @retry_on_k8s_error(max_retries=3, initial_delay=1.0, max_delay=10.0)
    async def get_ops_request_status(
        self,
        engine: DatabaseEngine,
        ops_request_name: str,
        namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get the status of an OpsRequest.

        Args:
            engine: Database engine type
            ops_request_name: Name of the OpsRequest
            namespace: Kubernetes namespace
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            OpsRequest status details including phase and conditions

        Raises:
            KubeDBError: If getting OpsRequest status fails
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            ops_request = await client_set.custom_api.get_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=namespace,
                plural=self._get_ops_request_plural(engine),
                name=ops_request_name,
            )

            status = ops_request.get("status", {})
            phase = status.get("phase", "Unknown")
            conditions = status.get("conditions", [])

            return {
                "phase": phase,
                "conditions": conditions,
                "observedGeneration": status.get("observedGeneration"),
            }

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            logger.error(
                "failed_to_get_ops_request_status",
                engine=engine,
                ops_request_name=ops_request_name,
                namespace=namespace,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to get OpsRequest status: {e.reason}")

    async def wait_for_ops_request_completion(
        self,
        engine: DatabaseEngine,
        ops_request_name: str,
        namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
        timeout_seconds: int = 600,
        check_interval: int = 5,
    ) -> Dict[str, Any]:
        """
        Wait for an OpsRequest to complete (reach Successful or Failed phase).

        Args:
            engine: Database engine type
            ops_request_name: Name of the OpsRequest
            namespace: Kubernetes namespace
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content
            timeout_seconds: Maximum time to wait (default: 600 seconds / 10 minutes)
            check_interval: Seconds between status checks (default: 5)

        Returns:
            Final OpsRequest status

        Raises:
            KubeDBError: If OpsRequest fails or times out
        """
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "waiting_for_ops_request_completion",
            engine=engine,
            ops_request_name=ops_request_name,
            namespace=namespace,
            timeout_seconds=timeout_seconds,
        )

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time

            if elapsed > timeout_seconds:
                logger.error(
                    "ops_request_timeout",
                    engine=engine,
                    ops_request_name=ops_request_name,
                    namespace=namespace,
                    elapsed_seconds=int(elapsed),
                )
                raise KubeDBError(
                    f"OpsRequest {ops_request_name} timed out after {timeout_seconds} seconds"
                )

            try:
                status = await self.get_ops_request_status(
                    engine=engine,
                    ops_request_name=ops_request_name,
                    namespace=namespace,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )

                phase = status["phase"]

                logger.info(
                    "ops_request_status_check",
                    ops_request_name=ops_request_name,
                    phase=phase,
                    elapsed_seconds=int(elapsed),
                )

                # Check if completed
                if phase == "Successful":
                    logger.info(
                        "ops_request_completed_successfully",
                        engine=engine,
                        ops_request_name=ops_request_name,
                        elapsed_seconds=int(elapsed),
                    )
                    return status

                elif phase == "Failed":
                    conditions = status.get("conditions", [])
                    failure_reason = "Unknown"
                    for condition in conditions:
                        if condition.get("type") == "Failed":
                            failure_reason = condition.get("message", "Unknown")
                            break

                    logger.error(
                        "ops_request_failed",
                        engine=engine,
                        ops_request_name=ops_request_name,
                        reason=failure_reason,
                        conditions=conditions,
                    )
                    raise KubeDBError(
                        f"OpsRequest {ops_request_name} failed: {failure_reason}"
                    )

                # Still in progress (Pending, Progressing, etc.)
                await asyncio.sleep(check_interval)

            except KubeDBError:
                # Re-raise KubeDBError (includes failures and timeouts)
                raise
            except Exception as e:
                logger.warning(
                    "error_checking_ops_request_status",
                    ops_request_name=ops_request_name,
                    error=str(e),
                )
                # Continue checking - transient errors are possible
                await asyncio.sleep(check_interval)

    async def pause_database(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Pause a KubeDB database instance.

        Paused databases stop consuming compute resources but retain data.
        This is done by setting spec.halted = true in the KubeDB resource.

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            Updated resource details

        Raises:
            KubeDBError: If pause operation fails
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        patch_body = {
            "spec": {
                "halted": True
            }
        }

        try:
            logger.info(
                "pausing_database",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            result = await client_set.custom_api.patch_namespaced_custom_object(
                group=self._get_kubedb_group(engine),
                version=self._get_kubedb_version(engine),
                namespace=namespace,
                plural=self._get_kubedb_plural(engine),
                name=name,
                body=patch_body,
                _content_type="application/merge-patch+json",
            )

            logger.info(
                "database_paused",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            return result

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            logger.error(
                "database_pause_failed",
                engine=engine,
                name=name,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to pause database: {e.reason}. Details: {error_body}")

    async def resume_database(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resume a paused KubeDB database instance.

        Resumed databases start consuming compute resources again.
        This is done by setting spec.halted = false in the KubeDB resource.

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace
            provider_id: Optional provider ID for multi-cluster
            kubeconfig_content: Optional kubeconfig content

        Returns:
            Updated resource details

        Raises:
            KubeDBError: If resume operation fails
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        patch_body = {
            "spec": {
                "halted": False
            }
        }

        try:
            logger.info(
                "resuming_database",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            result = await client_set.custom_api.patch_namespaced_custom_object(
                group=self._get_kubedb_group(engine),
                version=self._get_kubedb_version(engine),
                namespace=namespace,
                plural=self._get_kubedb_plural(engine),
                name=name,
                body=patch_body,
                _content_type="application/merge-patch+json",
            )

            logger.info(
                "database_resumed",
                engine=engine,
                name=name,
                namespace=namespace,
            )

            return result

        except ApiException as e:
            error_body = None
            try:
                import json
                error_body = json.loads(e.body) if e.body else None
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as parse_error:
                logger.debug("api_error_body_parse_failed", error=str(parse_error))
                error_body = e.body

            logger.error(
                "database_resume_failed",
                engine=engine,
                name=name,
                error=e.reason,
                status=e.status,
                error_body=error_body,
            )
            raise KubeDBError(f"Failed to resume database: {e.reason}. Details: {error_body}")

    async def reconcile_database_pods(
        self, engine: DatabaseEngine,
        name: str,
        namespace: str,
        max_attempts: int = 10,
        check_interval: int = 3,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
        desired_resources_override: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Reconcile database pods to match desired state.

        This implements a reconciliation loop that ensures pods have the correct
        resources as specified in the MongoDB/Database CR. If pods have outdated
        resources, they are deleted to trigger recreation with correct resources.

        This solves the race condition where pods might be created before PetSet
        is updated, by continuously checking until pods match the desired state.

        Args:
            engine: Database engine type
            name: Database resource name (e.g., "mydb-demo-demo")
            namespace: Kubernetes namespace
            max_attempts: Maximum reconciliation attempts (default: 10)
            check_interval: Seconds between checks (default: 3)
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        label_selector = f"app.kubernetes.io/instance={name}"

        logger.info(
            "starting_pod_reconciliation",
            name=name,
            namespace=namespace,
            max_attempts=max_attempts,
        )

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "reconciliation_attempt",
                    name=name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )

                # Get desired resources - use override if provided, otherwise read from CR
                desired_resources = desired_resources_override
                container_name = None
                
                # Determine container name
                container_names = {
                    DatabaseEngine.MONGODB: "mongodb",
                    DatabaseEngine.POSTGRES: "postgres",
                    DatabaseEngine.MYSQL: "mysql",
                    DatabaseEngine.MARIADB: "mariadb",
                    DatabaseEngine.REDIS: "redis",
                    DatabaseEngine.ELASTICSEARCH: "elasticsearch",
                }
                container_name = container_names.get(engine, "mongodb")
                
                if not desired_resources:
                    # Get desired resources from MongoDB/Database CR
                    try:
                        cr = await client_set.custom_api.get_namespaced_custom_object(
                            group=self._get_kubedb_group(engine),
                            version=self._get_kubedb_version(engine),
                            namespace=namespace,
                            plural=self._get_kubedb_plural(engine),
                            name=name,
                        )

                        # Extract desired resources from CR spec
                        if "spec" in cr and "podTemplate" in cr["spec"]:
                            containers = cr["spec"]["podTemplate"]["spec"].get("containers", [])
                            for container in containers:
                                if container.get("name") == container_name:
                                    desired_resources = container.get("resources", {})
                                    break

                        if not desired_resources:
                            logger.warning(
                                "no_desired_resources_found",
                                name=name,
                                namespace=namespace,
                                engine=engine,
                                container_name=container_name,
                            )
                            await asyncio.sleep(check_interval)
                            continue

                        logger.info(
                            "desired_resources_extracted",
                            name=name,
                            desired_resources=desired_resources,
                        )

                    except ApiException as e:
                        logger.error("failed_to_get_cr", name=name, error=str(e))
                        await asyncio.sleep(check_interval)
                        continue
                else:
                    logger.info(
                        "using_override_resources_for_reconciliation",
                        name=name,
                        desired_resources=desired_resources,
                    )

                # Get current pods
                pods = await client_set.core_api.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=label_selector,
                )

                if not pods.items:
                    logger.info(
                        "no_pods_found_waiting",
                        name=name,
                        attempt=attempt,
                    )
                    await asyncio.sleep(check_interval)
                    continue

                # Check each pod's resources
                pods_to_delete = []
                all_synced = True

                for pod in pods.items:
                    pod_name = pod.metadata.name

                    # Find the main database container in pod and get its resources
                    actual_resources = None
                    for container in pod.spec.containers:
                        if container.name == container_name:
                            # Extract resources, handling ResourceQuantity objects
                            requests = {}
                            limits = {}
                            
                            if container.resources and container.resources.requests:
                                requests = {k: str(v) for k, v in container.resources.requests.items()}
                            if container.resources and container.resources.limits:
                                limits = {k: str(v) for k, v in container.resources.limits.items()}
                            
                            actual_resources = {
                                "requests": requests,
                                "limits": limits,
                            }
                            break

                    if not actual_resources:
                        logger.warning(
                            "could_not_extract_pod_resources",
                            pod_name=pod_name,
                            engine=engine,
                            container_name=container_name,
                        )
                        continue

                    # Compare resources (normalize to string for comparison)
                    desired_requests = {k: str(v) for k, v in desired_resources.get("requests", {}).items()}
                    desired_limits = {k: str(v) for k, v in desired_resources.get("limits", {}).items()}

                    resources_match = (
                        actual_resources.get("requests", {}) == desired_requests
                        and actual_resources.get("limits", {}) == desired_limits
                    )

                    if not resources_match:
                        logger.info(
                            "pod_resources_mismatch",
                            pod_name=pod_name,
                            desired=desired_resources,
                            actual=actual_resources,
                        )
                        pods_to_delete.append(pod_name)
                        all_synced = False
                    else:
                        logger.info(
                            "pod_resources_synced",
                            pod_name=pod_name,
                        )

                # If all pods are synced, we're done
                if all_synced:
                    logger.info(
                        "reconciliation_complete_all_synced",
                        name=name,
                        namespace=namespace,
                        attempts=attempt,
                    )
                    return

                # Delete pods with mismatched resources
                if pods_to_delete:
                    for pod_name in pods_to_delete:
                        logger.info(
                            "deleting_pod_for_reconciliation",
                            pod_name=pod_name,
                            namespace=namespace,
                        )

                        await client_set.core_api.delete_namespaced_pod(
                            name=pod_name,
                            namespace=namespace,
                        )

                    logger.info(
                        "deleted_pods_for_reconciliation",
                        count=len(pods_to_delete),
                        pods=pods_to_delete,
                    )

                # Wait before next check
                await asyncio.sleep(check_interval)

            except ApiException as e:
                logger.error(
                    "reconciliation_attempt_failed",
                    name=name,
                    attempt=attempt,
                    error=e.reason,
                )
                await asyncio.sleep(check_interval)

        logger.warning(
            "reconciliation_max_attempts_reached",
            name=name,
            namespace=namespace,
            max_attempts=max_attempts,
        )


    async def get_database_metrics(
        self, engine: DatabaseEngine, name: str, namespace: str,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch database metrics from KubeDB monitoring service.

        Uses cluster DNS for scalable, production-ready monitoring without port-forward.
        Delegates to MetricsService for comprehensive metric parsing.

        Args:
            engine: Database engine type
            name: Database resource name
            namespace: Kubernetes namespace

        Returns:
            Dictionary containing parsed metrics or None if unavailable
        """
        from app.services.metrics_service import metrics_service

        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            # Delegate to metrics_service for comprehensive metrics collection
            # This uses cluster DNS (no port-forward) and provides detailed engine-specific metrics
            metrics = await metrics_service.get_database_metrics(
                engine=engine,
                name=name,
                namespace=namespace,
            )

            return metrics

        except Exception as e:
            logger.error(
                "failed_to_fetch_metrics",
                database=name,
                error=str(e),
                exc_info=True,
            )
            return None

    async def get_available_versions(
        self,
        engine: DatabaseEngine,
        provider_id: Optional[str] = None,
        kubeconfig_content: Optional[str] = None,
    ) -> Dict[str, list]:
        """
        Get available database versions from KubeDB version CRDs in the cluster.

        Args:
            engine: Database engine type
            provider_id: Provider ID for multi-cluster support
            kubeconfig_content: Provider's kubeconfig content

        Returns:
            Dictionary with engine name and list of available versions
        """
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        # Map engine to KubeDB version CRD kind
        version_kind_map = {
            DatabaseEngine.MONGODB: "MongoDBVersion",
            DatabaseEngine.POSTGRES: "PostgresVersion",
            DatabaseEngine.MYSQL: "MySQLVersion",
            DatabaseEngine.REDIS: "RedisVersion",
            DatabaseEngine.ELASTICSEARCH: "ElasticsearchVersion",
        }

        kind = version_kind_map.get(engine)
        if not kind:
            engine_name = engine.value if engine else "unknown"
            logger.warning("unsupported_engine_for_version_query", engine=engine_name)
            return {"engine": engine_name, "versions": []}

        try:
            # Query the KubeDB version CRD from catalog.kubedb.com
            versions_response = await client_set.custom_api.list_cluster_custom_object(
                group="catalog.kubedb.com",
                version="v1alpha1",
                plural=f"{kind.lower()}s",
            )

            versions = []
            for item in versions_response.get("items", []):
                version_name = item.get("metadata", {}).get("name", "")
                spec = item.get("spec", {})
                version_value = spec.get("version", version_name)
                deprecated = spec.get("deprecated", False)

                if not deprecated:  # Only return non-deprecated versions
                    versions.append({
                        "name": version_name,
                        "version": version_value,
                        "deprecated": deprecated,
                    })

            # Sort versions (newest first)
            versions.sort(key=lambda x: x["version"], reverse=True)

            logger.info(
                "fetched_available_versions",
                engine=engine.value,
                provider_id=provider_id,
                count=len(versions),
            )

            return {
                "engine": engine.value,
                "versions": versions,
            }

        except ApiException as e:
            if e.status == 404:
                logger.warning(
                    "version_crd_not_found",
                    engine=engine.value,
                    kind=kind,
                    provider_id=provider_id,
                )
                return {
                    "engine": engine.value,
                    "versions": [],
                    "error": f"KubeDB {kind} CRD not found in cluster"
                }
            else:
                logger.error(
                    "failed_to_fetch_versions",
                    engine=engine.value,
                    error=str(e),
                    provider_id=provider_id,
                )
                raise KubernetesError(f"Failed to fetch versions for {engine.value}: {str(e)}")
        except Exception as e:
            logger.error(
                "unexpected_error_fetching_versions",
                engine=engine.value,
                error=str(e),
                provider_id=provider_id,
                exc_info=True,
            )
            raise KubernetesError(f"Failed to fetch versions for {engine.value}: {str(e)}")

    # ===============================================================================
    # VERSION UPGRADE METHODS
    # ===============================================================================

    async def get_versions_by_region(
        self, engine: str, region: Optional[str] = None, availability_zone: Optional[str] = None
    ) -> list[Dict[str, Any]]:
        """
        Get available versions for a specific database engine from a provider by region/AZ.

        Args:
            engine: Database engine type (postgres, mysql, mongodb, etc.)
            region: Optional region filter
            availability_zone: Optional AZ filter

        Returns:
            List of available version dictionaries with version details

        Raises:
            ValueError: If engine is invalid or provider not found
            KubernetesError: If failed to fetch versions
        """
        from app.models.database import DatabaseEngine

        try:
            # Validate engine
            try:
                db_engine = DatabaseEngine(engine.lower())
            except ValueError:
                raise ValueError(f"Invalid database engine: {engine}")

            # Get provider for region/AZ
            from app.services.provider_service import provider_service

            providers = await provider_service.list_providers(
                region=region,
                availability_zone=availability_zone,
                is_active=True,
                skip=0,
                limit=1,
            )

            if not providers:
                logger.warning(
                    "no_providers_found_for_versions",
                    engine=engine,
                    region=region,
                    az=availability_zone,
                )
                return []

            provider = providers[0]
            provider_id = provider.id

            logger.info(
                "fetching_versions_for_engine",
                engine=engine,
                provider_id=provider_id,
            )

            # Fetch versions using get_available_versions
            result = await self.get_available_versions(
                engine=db_engine, provider_id=provider_id
            )

            return result.get("versions", [])

        except Exception as e:
            logger.error(
                "error_fetching_available_versions",
                engine=engine,
                error=str(e),
                exc_info=True,
            )
            raise

    async def upgrade_database_version(
        self,
        domain: str,
        project: str,
        database_id: str,
        target_version: str,
        skip_backup: bool = False,
    ) -> str:
        """
        Upgrade a database to a new version using KubeDB OpsRequest.

        Creates a version update OpsRequest that upgrades the database.
        Supports patch, minor, and major version upgrades (if allowed by KubeDB).

        Args:
            domain: Domain name
            project: Project name
            database_id: Database unique identifier
            target_version: Target version to upgrade to
            skip_backup: Whether to skip pre-upgrade backup

        Returns:
            Operation ID (OpsRequest name) for tracking upgrade progress

        Raises:
            ValueError: If database not found or invalid upgrade
            KubernetesError: If OpsRequest creation fails
        """
        from app.services.database_service import database_service
        from app.utils.version import (
            is_upgrade_compatible,
            get_upgrade_type,
            UpgradeType,
        )

        logger.info(
            "upgrading_database_version",
            database_id=database_id,
            domain=domain,
            project=project,
            target_version=target_version,
        )

        # Get database document directly from MongoDB to access all fields
        from app.repositories.models import Database
        db = await Database.find_one({"_id": database_id, "domain": domain, "project": project})
        if not db:
            raise ValueError(f"Database not found: {database_id}")

        # Use kubedb_resource_name (the actual K8s resource name) not name (user-friendly name)
        db_name = db.kubedb_resource_name
        current_version = db.version
        engine = db.engine
        provider_id = db.provider_id
        namespace = db.namespace or "default"

        # Validate upgrade compatibility
        is_compatible, reason = is_upgrade_compatible(
            current_version, target_version, allow_major=True
        )

        if not is_compatible:
            raise ValueError(f"Incompatible upgrade: {reason}")

        upgrade_type = get_upgrade_type(current_version, target_version)

        logger.info(
            "upgrade_validation_passed",
            database_id=database_id,
            current_version=current_version,
            target_version=target_version,
            upgrade_type=upgrade_type.value,
        )

        # Get kubeconfig for provider
        from app.repositories.models import Provider
        provider = await Provider.find_one({"_id": provider_id})
        if not provider:
            raise ValueError(f"Provider {provider_id} not found")
        kubeconfig_content = provider.kubeconfig_content
        if not kubeconfig_content:
            raise ValueError(f"kubeconfig_content is required for provider {provider_id}")

        # Generate OpsRequest name
        import time
        timestamp = int(time.time())
        ops_request_name = f"{db_name}-upgrade-{timestamp}"

        # Create OpsRequest spec based on engine type
        ops_request = await self._create_version_upgrade_ops_request(
            engine=engine,
            db_name=db_name,
            namespace=namespace,
            ops_request_name=ops_request_name,
            target_version=target_version,
            upgrade_type=upgrade_type,
        )

        # Create the OpsRequest in Kubernetes
        try:
            # Convert engine enum to string if needed
            engine_str = engine.value if hasattr(engine, 'value') else str(engine)
            await self._create_ops_request(
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
                ops_request=ops_request,
                namespace=namespace,
                engine=engine_str,
            )

            logger.info(
                "version_upgrade_ops_request_created",
                database_id=database_id,
                ops_request_name=ops_request_name,
                target_version=target_version,
                upgrade_type=upgrade_type.value,
            )

            return ops_request_name

        except Exception as e:
            logger.error(
                "failed_to_create_upgrade_ops_request",
                database_id=database_id,
                ops_request_name=ops_request_name,
                error=str(e),
                exc_info=True,
            )
            raise KubernetesError(f"Failed to create upgrade OpsRequest: {str(e)}")

    async def _create_version_upgrade_ops_request(
        self,
        engine: str,
        db_name: str,
        namespace: str,
        ops_request_name: str,
        target_version: str,
        upgrade_type,
    ) -> Dict[str, Any]:
        """
        Create version upgrade OpsRequest spec for a database engine.

        Args:
            engine: Database engine type
            db_name: Database resource name
            namespace: Kubernetes namespace
            ops_request_name: Name for the OpsRequest
            target_version: Target version to upgrade to
            upgrade_type: Type of upgrade (patch/minor/major)

        Returns:
            OpsRequest spec dictionary
        """
        # Map engine to KubeDB resource kind and OpsRequest kind
        engine_map = {
            "postgres": ("Postgres", "PostgresOpsRequest"),
            "mysql": ("MySQL", "MySQLOpsRequest"),
            "mongodb": ("MongoDB", "MongoDBOpsRequest"),
            "redis": ("Redis", "RedisOpsRequest"),
            "elasticsearch": ("Elasticsearch", "ElasticsearchOpsRequest"),
        }

        if engine not in engine_map:
            raise ValueError(f"Unsupported engine for version upgrade: {engine}")

        db_kind, ops_kind = engine_map[engine]

        logger.info(
            "creating_version_upgrade_ops_request_spec",
            engine=engine,
            db_name=db_name,
            target_version=target_version,
            upgrade_type=upgrade_type.value,
        )

        # Base OpsRequest structure
        ops_request = {
            "apiVersion": "ops.kubedb.com/v1alpha1",
            "kind": ops_kind,
            "metadata": {
                "name": ops_request_name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "kubedb-dbaas",
                    "dbaas.kubedb.com/operation-type": "version-upgrade",
                    "dbaas.kubedb.com/upgrade-type": upgrade_type.value,
                },
            },
            "spec": {
                "type": "UpdateVersion",
                "databaseRef": {"name": db_name},
                "updateVersion": {"targetVersion": target_version},
            },
        }

        # Add apply policy based on upgrade type
        # For major upgrades, use IfReady (more cautious)
        # For minor/patch, can use Always
        if upgrade_type.value == "major":
            ops_request["spec"]["apply"] = "IfReady"
        else:
            ops_request["spec"]["apply"] = "Always"

        return ops_request

    async def _create_ops_request(
        self,
        provider_id: Optional[str],
        kubeconfig_content: str,
        ops_request: Dict[str, Any],
        namespace: str,
        engine: str,
    ):
        """
        Create a KubeDB OpsRequest in the cluster.

        Args:
            provider_id: Provider ID for getting the client
            kubeconfig_content: Kubeconfig YAML content
            ops_request: OpsRequest spec dictionary
            namespace: Kubernetes namespace
            engine: Database engine type

        Raises:
            KubernetesError: If creation fails
        """
        # Map engine to API group
        engine_group_map = {
            "postgres": "ops.kubedb.com",
            "mysql": "ops.kubedb.com",
            "mongodb": "ops.kubedb.com",
            "redis": "ops.kubedb.com",
            "elasticsearch": "ops.kubedb.com",
        }

        group = engine_group_map.get(engine, "ops.kubedb.com")
        version = "v1alpha1"
        plural_map = {
            "postgres": "postgresopsrequests",
            "mysql": "mysqlopsrequests",
            "mongodb": "mongodbopsrequests",
            "redis": "redisopsrequests",
            "elasticsearch": "elasticsearchopsrequests",
        }
        plural = plural_map.get(engine, f"{engine}opsrequests")

        # Get Kubernetes client for this provider
        client_set = await self.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            await client_set.custom_api.create_namespaced_custom_object(
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                body=ops_request,
            )

            logger.info(
                "ops_request_created_successfully",
                name=ops_request["metadata"]["name"],
                namespace=namespace,
                engine=engine,
            )

        except ApiException as e:
            logger.error(
                "api_exception_creating_ops_request",
                name=ops_request["metadata"]["name"],
                namespace=namespace,
                status=e.status,
                reason=e.reason,
                body=e.body,
            )
            raise KubernetesError(f"Failed to create OpsRequest: {e.reason}")


# Global instance
kubedb_service = KubeDBService()
