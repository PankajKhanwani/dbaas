# Low-Level Design (LLD)
# KubeDB DBaaS Platform

**Version:** 1.0.0
**Date:** December 23, 2025
**Author:** KubeDB DBaaS Team

---

## Table of Contents

1. [Database Schema](#1-database-schema)
2. [API Specifications](#2-api-specifications)
3. [Class Diagrams](#3-class-diagrams)
4. [Sequence Diagrams](#4-sequence-diagrams)
5. [Data Models](#5-data-models)
6. [Service Layer Details](#6-service-layer-details)
7. [Worker Algorithms](#7-worker-algorithms)
8. [Error Handling](#8-error-handling)
9. [Configuration Management](#9-configuration-management)
10. [Testing Strategy](#10-testing-strategy)

---

## 1. Database Schema

### 1.1 MongoDB Collections

#### 1.1.1 `databases` Collection

```python
{
  "_id": ObjectId,
  "id": str,                    # UUID v4
  "name": str,                  # DNS-1123 compliant
  "domain": str,                # Multi-tenancy domain
  "project": str,               # Project/namespace

  # Database Configuration
  "engine": str,                # postgres, mysql, mongodb, etc.
  "version": str,               # Database version
  "size": str,                  # db.t3.micro, db.t3.small, etc.
  "storage_gb": int,            # Storage size
  "replicas": int,              # Number of replicas

  # Resource Specifications
  "cpu": str,                   # e.g., "500m"
  "memory": str,                # e.g., "1Gi"

  # High Availability
  "high_availability": bool,
  "topology_mode": str,         # replicaset, cluster, standalone

  # Backup Configuration
  "backup_enabled": bool,
  "backup_schedule": str,       # hourly, daily, weekly
  "backup_retention_days": int,

  # Version Upgrade Policy
  "upgrade_policy": {
    "strategy": str,            # disabled, latest_patch, etc.
    "target_version": str,
    "auto_approve": bool,
    "pre_upgrade_backup": bool
  },

  # Status Information
  "status": str,                # pending, provisioning, running, etc.
  "health_status": str,         # healthy, degraded, unhealthy
  "phase": str,                 # KubeDB phase
  "endpoint": str,              # Connection endpoint
  "port": int,                  # Connection port
  "ready_replicas": int,

  # Kubernetes Information
  "namespace": str,
  "kubedb_resource_name": str,
  "provider_id": str,           # Reference to provider

  # Metadata
  "labels": dict,
  "annotations": dict,
  "created_at": datetime,
  "updated_at": datetime,
  "deleted_at": datetime,       # Soft delete

  # Indexes
  "indexes": [
    {"keys": [("id", 1)], "unique": True},
    {"keys": [("domain", 1), ("project", 1), ("name", 1)], "unique": True},
    {"keys": [("status", 1), ("updated_at", -1)]},
    {"keys": [("provider_id", 1)]},
    {"keys": [("deleted_at", 1)], "sparse": True}
  ]
}
```

#### 1.1.2 `providers` Collection

```python
{
  "_id": ObjectId,
  "id": str,                    # UUID v4
  "name": str,

  # Cloud Provider Information
  "provider_type": str,         # aws, gcp, azure, on-prem
  "region": str,
  "availability_zone": str,

  # Kubernetes Cluster Details
  "cluster_name": str,
  "cluster_endpoint": str,
  "kubeconfig": str,            # Base64 encoded

  # Capacity Information
  "total_cpu": int,
  "available_cpu": int,
  "total_memory": int,
  "available_memory": int,
  "total_storage": int,
  "available_storage": int,

  # Supported Engines
  "supported_engines": [
    {
      "engine": str,
      "versions": [str],
      "features": [str]
    }
  ],

  # Status
  "status": str,                # active, maintenance, disabled
  "is_default": bool,
  "priority": int,              # For selection algorithm

  # Metadata
  "created_at": datetime,
  "updated_at": datetime,

  # Indexes
  "indexes": [
    {"keys": [("id", 1)], "unique": True},
    {"keys": [("status", 1), ("priority", -1)]},
    {"keys": [("region", 1), ("availability_zone", 1)]}
  ]
}
```

#### 1.1.3 `operations` Collection

```python
{
  "_id": ObjectId,
  "id": str,                    # UUID v4
  "database_id": str,

  # Operation Details
  "operation_type": str,        # scale_vertical, scale_horizontal, etc.
  "operation_subtype": str,     # cpu_only, memory_only, replicas, etc.
  "status": str,                # pending, in_progress, completed, failed

  # Operation Payload
  "payload": {
    "from_state": dict,
    "to_state": dict,
    "parameters": dict
  },

  # Kubernetes OpsRequest
  "ops_request_name": str,
  "ops_request_phase": str,

  # Timing
  "created_at": datetime,
  "started_at": datetime,
  "completed_at": datetime,
  "duration_seconds": float,

  # Error Information
  "error_message": str,
  "error_details": dict,
  "retry_count": int,

  # Indexes
  "indexes": [
    {"keys": [("id", 1)], "unique": True},
    {"keys": [("database_id", 1), ("created_at", -1)]},
    {"keys": [("status", 1), ("created_at", -1)]},
    {"keys": [("created_at", 1)], "expireAfterSeconds": 2592000}  # 30 days TTL
  ]
}
```

#### 1.1.4 `audit_logs` Collection

```python
{
  "_id": ObjectId,
  "id": str,

  # Event Information
  "event_type": str,            # api_call, operation, error
  "action": str,                # create, update, delete, scale, etc.
  "resource_type": str,         # database, provider, operation
  "resource_id": str,

  # User Information
  "user_id": str,
  "user_email": str,
  "domain": str,
  "project": str,

  # Request Details
  "request_method": str,
  "request_path": str,
  "request_params": dict,
  "request_body": dict,

  # Response Details
  "status_code": int,
  "response_time_ms": float,

  # Context
  "ip_address": str,
  "user_agent": str,
  "correlation_id": str,

  # Timestamp
  "timestamp": datetime,

  # Indexes
  "indexes": [
    {"keys": [("resource_id", 1), ("timestamp", -1)]},
    {"keys": [("user_id", 1), ("timestamp", -1)]},
    {"keys": [("event_type", 1), ("timestamp", -1)]},
    {"keys": [("timestamp", 1)], "expireAfterSeconds": 7776000}  # 90 days TTL
  ]
}
```

### 1.2 Redis Data Structures

#### 1.2.1 Operation Queue

```python
# FIFO Queue for database operations
Key: "operation:queue"
Type: LIST
Structure: [operation_id_1, operation_id_2, ...]
TTL: None (persistent)
```

#### 1.2.2 Status Cache

```python
# Database status cache
Key: f"status:{database_id}"
Type: HASH
Structure: {
  "status": "running",
  "health_status": "healthy",
  "ready_replicas": "3",
  "endpoint": "10.0.0.1",
  "port": "5432",
  "updated_at": "2025-12-23T12:00:00Z"
}
TTL: 60 seconds
```

#### 1.2.3 Metrics Cache

```python
# Database metrics cache
Key: f"metrics:{database_id}"
Type: HASH
Structure: {
  "cpu_usage": "45.5",
  "memory_usage": "1024",
  "connections": "10",
  "queries_per_second": "150",
  "replication_lag": "0"
}
TTL: 30 seconds
```

#### 1.2.4 Session Store

```python
# User session data
Key: f"session:{session_id}"
Type: STRING (JSON)
Structure: {
  "user_id": "uuid",
  "email": "user@example.com",
  "roles": ["admin"],
  "expires_at": "timestamp"
}
TTL: 3600 seconds (1 hour)
```

---

## 2. API Specifications

### 2.1 Database Endpoints

#### 2.1.1 Create Database

```http
POST /api/v1/domains/{domain}/projects/{project}/databases
Content-Type: application/json
Authorization: Bearer <token>

Request Body:
{
  "name": "my-postgres-db",
  "engine": "postgres",
  "version": "15.0",
  "size": "db.t3.small",
  "storage_gb": 50,
  "replicas": 3,
  "high_availability": true,
  "backup_enabled": true,
  "backup_schedule": "daily",
  "backup_retention_days": 30,
  "labels": {
    "environment": "production",
    "team": "backend"
  }
}

Response: 201 Created
{
  "database_id": "db-550e8400-e29b-41d4-a716-446655440000",
  "name": "my-postgres-db",
  "status": "provisioning",
  "message": "Database creation initiated",
  "created_at": "2025-12-23T12:00:00Z"
}
```

#### 2.1.2 Get Database Status

```http
GET /api/v1/domains/{domain}/projects/{project}/databases/{database_id}/status
GET /api/v1/domains/{domain}/projects/{project}/databases/{database_id}/status?realtime=true

Response: 200 OK
{
  "database_id": "db-550e8400-e29b-41d4-a716-446655440000",
  "name": "my-postgres-db",
  "status": "running",
  "health_status": "healthy",
  "endpoint": "my-postgres-db.default.svc.cluster.local",
  "port": 5432,
  "replicas": {
    "desired": 3,
    "ready": 3,
    "status": "3/3"
  },
  "resources": {
    "cpu": "500m",
    "memory": "2Gi",
    "storage": "50Gi"
  },
  "cached": true,
  "updated_at": "2025-12-23T12:05:00Z",
  "message": "Cached status (updated every 30s). Use ?realtime=true for real-time K8s status."
}
```

#### 2.1.3 Scale Database

```http
PUT /api/v1/domains/{domain}/projects/{project}/databases/{database_id}/scale
Content-Type: application/json

Request Body (Vertical Scaling):
{
  "scale_type": "vertical",
  "size": "db.t3.medium"
}

Request Body (Horizontal Scaling):
{
  "scale_type": "horizontal",
  "replicas": 5
}

Response: 202 Accepted
{
  "operation_id": "op-660e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "Scaling operation queued"
}
```

#### 2.1.4 Update Database

```http
PATCH /api/v1/domains/{domain}/projects/{project}/databases/{database_id}
Content-Type: application/json

Request Body:
{
  "backup_enabled": true,
  "backup_schedule": "hourly",
  "labels": {
    "environment": "staging"
  }
}

Response: 200 OK
{
  "database_id": "db-550e8400-e29b-41d4-a716-446655440000",
  "message": "Database updated successfully",
  "updated_fields": ["backup_enabled", "backup_schedule", "labels"]
}
```

#### 2.1.5 Delete Database

```http
DELETE /api/v1/domains/{domain}/projects/{project}/databases/{database_id}?force=true

Response: 202 Accepted
{
  "database_id": "db-550e8400-e29b-41d4-a716-446655440000",
  "status": "deleting",
  "message": "Database deletion initiated"
}
```

### 2.2 Backup Endpoints

#### 2.2.1 Trigger Backup

```http
POST /api/v1/domains/{domain}/projects/{project}/databases/{database_id}/backup

Response: 202 Accepted
{
  "backup_id": "backup-770e8400-e29b-41d4-a716-446655440000",
  "database_id": "db-550e8400-e29b-41d4-a716-446655440000",
  "status": "initiated",
  "s3_path": "s3://ashok21/backups/postgres/my-postgres-db/20251223-120000/"
}
```

#### 2.2.2 List Backups

```http
GET /api/v1/domains/{domain}/projects/{project}/databases/{database_id}/backups

Response: 200 OK
{
  "backups": [
    {
      "backup_id": "backup-770e8400-e29b-41d4-a716-446655440000",
      "created_at": "2025-12-23T12:00:00Z",
      "size_bytes": 1073741824,
      "status": "completed",
      "s3_path": "s3://ashok21/backups/postgres/my-postgres-db/20251223-120000/"
    }
  ],
  "total": 1
}
```

### 2.3 Provider Endpoints

#### 2.3.1 List Providers

```http
GET /api/v1/providers?region=south&availability_zone=az1

Response: 200 OK
{
  "providers": [
    {
      "id": "prov-880e8400-e29b-41d4-a716-446655440000",
      "name": "southaz1-cluster",
      "region": "south",
      "availability_zone": "az1",
      "status": "active",
      "capacity": {
        "cpu_available": 48,
        "memory_available_gb": 96,
        "storage_available_gb": 1000
      }
    }
  ]
}
```

#### 2.3.2 Get Available Database Engines

```http
GET /api/v1/providers/engines?region=south

Response: 200 OK
{
  "engines": [
    {
      "engine": "postgres",
      "versions": ["12.0", "13.0", "14.0", "15.0", "16.0"],
      "features": ["streaming_replication", "logical_replication", "pitr"]
    },
    {
      "engine": "mongodb",
      "versions": ["4.4", "5.0", "6.0", "7.0"],
      "features": ["replica_set", "sharding", "change_streams"]
    }
  ]
}
```

### 2.4 Operation Endpoints

#### 2.4.1 Get Operation Status

```http
GET /api/v1/operations/{operation_id}

Response: 200 OK
{
  "operation_id": "op-660e8400-e29b-41d4-a716-446655440000",
  "database_id": "db-550e8400-e29b-41d4-a716-446655440000",
  "operation_type": "scale_vertical",
  "status": "in_progress",
  "progress": 50,
  "started_at": "2025-12-23T12:00:00Z",
  "estimated_completion": "2025-12-23T12:15:00Z"
}
```

---

## 3. Class Diagrams

### 3.1 Service Layer Classes

```python
# app/services/database_service.py

class DatabaseService:
    """
    Core service for database lifecycle management.
    """

    def __init__(
        self,
        database_repository: DatabaseRepository,
        kubedb_service: KubeDBService,
        provider_service: ProviderService,
        redis_connection: RedisConnection
    ):
        self.database_repo = database_repository
        self.kubedb_service = kubedb_service
        self.provider_service = provider_service
        self.redis = redis_connection

    async def create_database(
        self,
        domain: str,
        project: str,
        request: DatabaseCreateRequest
    ) -> Database:
        """
        Create a new database instance.

        Steps:
        1. Validate request and check quotas
        2. Select optimal provider
        3. Create database record (status: PENDING)
        4. Provision Kubernetes resources
        5. Update status to PROVISIONING
        6. Return database object
        """
        pass

    async def get_database(
        self,
        database_id: str,
        domain: str,
        project: str
    ) -> Database:
        """Get database by ID with domain/project scoping."""
        pass

    async def update_database(
        self,
        database_id: str,
        domain: str,
        project: str,
        update_request: DatabaseUpdateRequest
    ) -> Database:
        """Update database configuration."""
        pass

    async def delete_database(
        self,
        database_id: str,
        domain: str,
        project: str,
        force: bool = False
    ) -> None:
        """Delete database and cleanup resources."""
        pass

    async def scale_database(
        self,
        database_id: str,
        scale_request: ScaleRequest
    ) -> Operation:
        """
        Scale database vertically or horizontally.
        Creates operation and queues for processing.
        """
        pass

    async def get_database_status_realtime(
        self,
        database_id: str,
        domain: str,
        project: str
    ) -> DatabaseStatus:
        """Fetch real-time status from Kubernetes."""
        pass

    async def trigger_backup(
        self,
        database_id: str,
        domain: str,
        project: str
    ) -> Backup:
        """Trigger manual backup."""
        pass

    async def get_credentials(
        self,
        database_id: str,
        domain: str,
        project: str
    ) -> DatabaseCredentials:
        """Retrieve database connection credentials."""
        pass
```

```python
# app/services/kubedb_service.py

class KubeDBService:
    """
    Low-level Kubernetes and KubeDB API interactions.
    """

    async def create_database_resource(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        version: str,
        replicas: int,
        resources: dict,
        storage_size: str,
        provider_id: str,
        kubeconfig_content: str
    ) -> dict:
        """
        Create KubeDB custom resource (MongoDB, Postgres, etc.).

        Returns:
            dict: Created resource object
        """
        pass

    async def get_database_status(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        provider_id: str,
        kubeconfig_content: str
    ) -> dict:
        """Get database status from Kubernetes."""
        pass

    async def create_vertical_scaling_ops_request(
        self,
        engine: DatabaseEngine,
        database_name: str,
        namespace: str,
        target_size: DatabaseSize,
        provider_id: str,
        kubeconfig_content: str
    ) -> dict:
        """Create OpsRequest for vertical scaling."""
        pass

    async def create_horizontal_scaling_ops_request(
        self,
        engine: DatabaseEngine,
        database_name: str,
        namespace: str,
        replicas: int,
        provider_id: str,
        kubeconfig_content: str
    ) -> dict:
        """Create OpsRequest for horizontal scaling."""
        pass

    async def create_backup_job_direct(
        self,
        database_name: str,
        database_engine: DatabaseEngine,
        namespace: str,
        database_host: str,
        database_port: int,
        database_user: str,
        database_password: str,
        database_name_db: str,
        bucket: str,
        region: str,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
        provider_id: str,
        kubeconfig_content: str
    ) -> dict:
        """
        Create Kubernetes Job for direct database backup.
        Uses native tools: pg_dump, mongodump, mysqldump.
        """
        pass

    async def delete_database_resource(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
        provider_id: str,
        kubeconfig_content: str
    ) -> None:
        """Delete KubeDB database resource."""
        pass

    def _get_resource_limits(self, size: DatabaseSize) -> dict:
        """Map database size to CPU/memory limits."""
        return {
            DatabaseSize.MICRO: {"cpu": "500m", "memory": "1Gi"},
            DatabaseSize.SMALL: {"cpu": "1000m", "memory": "2Gi"},
            DatabaseSize.MEDIUM: {"cpu": "2000m", "memory": "4Gi"},
            DatabaseSize.LARGE: {"cpu": "2000m", "memory": "8Gi"},
            DatabaseSize.XLARGE: {"cpu": "4000m", "memory": "16Gi"},
            DatabaseSize.XXLARGE: {"cpu": "8000m", "memory": "32Gi"},
        }[size]

    def _get_vertical_scaling_topology_key(self, engine: DatabaseEngine) -> str:
        """
        Get topology key for vertical scaling based on engine.
        Different engines use different keys in OpsRequest spec.
        """
        topology_keys = {
            DatabaseEngine.MONGODB: "replicaSet",
            DatabaseEngine.POSTGRES: "postgres",
            DatabaseEngine.MYSQL: "mysql",
            DatabaseEngine.MARIADB: "mariadb",
            DatabaseEngine.REDIS: "redis",
            DatabaseEngine.ELASTICSEARCH: "node",
        }
        return topology_keys.get(engine, "replicaSet")
```

### 3.2 Worker Classes

```python
# app/workers/reconciliation_worker.py

class ReconciliationWorker:
    """
    Detects drift between desired and actual state.
    Creates OpsRequests to remediate drift.
    """

    def __init__(
        self,
        database_service: DatabaseService,
        kubedb_service: KubeDBService,
        provider_service: ProviderService
    ):
        self.database_service = database_service
        self.kubedb_service = kubedb_service
        self.provider_service = provider_service
        self.interval = settings.reconcile_interval  # 300 seconds
        self.batch_size = settings.reconcile_batch_size  # 50

    async def run(self) -> None:
        """Main reconciliation loop."""
        while True:
            try:
                await self.reconcile_databases()
            except Exception as e:
                logger.error("reconciliation_error", error=str(e))
            await asyncio.sleep(self.interval)

    async def reconcile_databases(self) -> None:
        """
        Reconcile all active databases.

        Process:
        1. Fetch databases in RUNNING/UPDATING state
        2. For each database:
           - Get current state from Kubernetes
           - Compare with desired state
           - If drift detected, create OpsRequest
        """
        databases = await self.database_service.get_active_databases(
            limit=self.batch_size
        )

        for db in databases:
            await self._reconcile_single_database(db)

    async def _reconcile_single_database(self, db: Database) -> None:
        """Reconcile single database instance."""
        try:
            # Get current state from K8s
            current_state = await self.kubedb_service.get_database_status(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=db.provider_id,
                kubeconfig_content=provider.kubeconfig
            )

            # Compare states
            drift = self._detect_drift(db, current_state)

            if drift:
                await self._remediate_drift(db, drift)

        except Exception as e:
            logger.error("reconciliation_failed", database_id=db.id, error=str(e))

    def _detect_drift(self, db: Database, current_state: dict) -> dict:
        """
        Detect configuration drift.

        Returns:
            dict: Drift details or None if no drift
        """
        drift = {}

        # Check replica count
        current_replicas = current_state.get("spec", {}).get("replicas", 0)
        if current_replicas != db.replicas:
            drift["replicas"] = {
                "desired": db.replicas,
                "current": current_replicas
            }

        # Check resources (CPU/Memory)
        current_resources = current_state.get("spec", {}).get("podTemplate", {}).get("spec", {}).get("resources", {})
        desired_resources = self.kubedb_service._get_resource_limits(db.size)

        if current_resources != desired_resources:
            drift["resources"] = {
                "desired": desired_resources,
                "current": current_resources
            }

        # Check version
        current_version = current_state.get("spec", {}).get("version", "")
        if current_version != db.version:
            drift["version"] = {
                "desired": db.version,
                "current": current_version
            }

        return drift if drift else None

    async def _remediate_drift(self, db: Database, drift: dict) -> None:
        """Create OpsRequest to remediate drift."""
        if "replicas" in drift:
            await self.kubedb_service.create_horizontal_scaling_ops_request(...)

        if "resources" in drift:
            await self.kubedb_service.create_vertical_scaling_ops_request(...)

        if "version" in drift:
            await self.kubedb_service.create_version_update_ops_request(...)
```

```python
# app/workers/status_sync_worker.py

class StatusSyncWorker:
    """
    Synchronizes database status from Kubernetes to MongoDB.
    Runs every 30 seconds.
    """

    def __init__(
        self,
        database_service: DatabaseService,
        kubedb_service: KubeDBService,
        redis_connection: RedisConnection
    ):
        self.database_service = database_service
        self.kubedb_service = kubedb_service
        self.redis = redis_connection
        self.interval = settings.status_sync_interval  # 30 seconds

    async def run(self) -> None:
        """Main status sync loop."""
        while True:
            try:
                await self.sync_all_statuses()
            except Exception as e:
                logger.error("status_sync_error", error=str(e))
            await asyncio.sleep(self.interval)

    async def sync_all_statuses(self) -> None:
        """Sync status for all active databases."""
        databases = await self.database_service.get_all_databases()

        tasks = [
            self._sync_single_database(db)
            for db in databases
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync_single_database(self, db: Database) -> None:
        """Sync status for single database."""
        try:
            # Fetch status from Kubernetes
            k8s_status = await self.kubedb_service.get_database_status(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=db.provider_id,
                kubeconfig_content=provider.kubeconfig
            )

            # Map KubeDB phase to DBaaS status
            new_status = self._map_kubedb_phase_to_status(
                phase=k8s_status.get("status", {}).get("phase"),
                is_ready=k8s_status.get("status", {}).get("conditions", {}).get("Ready")
            )

            # Update database
            db.status = new_status
            db.phase = k8s_status.get("status", {}).get("phase")
            db.ready_replicas = k8s_status.get("status", {}).get("readyReplicas", 0)
            db.endpoint = k8s_status.get("status", {}).get("primaryServiceDNS")
            db.port = self._get_default_port(db.engine)
            db.updated_at = datetime.utcnow()

            await db.save()

            # Cache in Redis
            await self._cache_status(db)

        except Exception as e:
            logger.error("status_sync_failed", database_id=db.id, error=str(e))

    def _map_kubedb_phase_to_status(
        self,
        phase: str,
        is_ready: bool
    ) -> DatabaseStatus:
        """
        Map KubeDB phase to DBaaS status.

        Phases:
        - Provisioning → PROVISIONING
        - Ready/Running → RUNNING
        - Failed → FAILED
        - Halted → PAUSED
        """
        phase_mapping = {
            "Provisioning": DatabaseStatus.PROVISIONING,
            "Ready": DatabaseStatus.RUNNING,
            "Running": DatabaseStatus.RUNNING,
            "Failed": DatabaseStatus.FAILED,
            "Halted": DatabaseStatus.PAUSED,
        }

        return phase_mapping.get(phase, DatabaseStatus.PENDING)

    async def _cache_status(self, db: Database) -> None:
        """Cache status in Redis for fast API responses."""
        status_data = {
            "status": db.status.value,
            "health_status": db.health_status.value,
            "ready_replicas": str(db.ready_replicas),
            "endpoint": db.endpoint or "",
            "port": str(db.port),
            "updated_at": db.updated_at.isoformat()
        }

        await self.redis.hset(
            f"status:{db.id}",
            mapping=status_data
        )
        await self.redis.expire(f"status:{db.id}", 60)
```

---

## 4. Sequence Diagrams

### 4.1 Database Creation Sequence

```
User → Frontend: Create Database Request
Frontend → Backend API: POST /api/v1/.../databases
Backend API → DatabaseService: create_database()
DatabaseService → ProviderService: select_optimal_provider()
ProviderService → DatabaseService: provider_id
DatabaseService → DatabaseRepository: save(database, status=PENDING)
DatabaseRepository → MongoDB: insert_one()
DatabaseService → KubeDBService: create_database_resource()
KubeDBService → K8s API: Create Custom Resource
K8s API → KubeDB Operator: Watch Event
KubeDB Operator → K8s API: Create StatefulSet, Service, Secret
DatabaseService → DatabaseRepository: update(status=PROVISIONING)
DatabaseService → Backend API: database_object
Backend API → Frontend: 201 Created
Frontend → User: Success Message

[Background Process]
Status Sync Worker → K8s API: Get Database Status (every 30s)
K8s API → Status Sync Worker: Status Response
Status Sync Worker → MongoDB: Update status=RUNNING
Status Sync Worker → Redis: Cache status
```

### 4.2 Vertical Scaling Sequence

```
User → Frontend: Request Vertical Scaling
Frontend → Backend API: PUT /api/v1/.../databases/{id}/scale
Backend API → DatabaseService: scale_database()
DatabaseService → DatabaseRepository: update(size=new_size)
DatabaseRepository → MongoDB: update_one()
DatabaseService → OperationService: create_operation(type=SCALE_VERTICAL)
OperationService → MongoDB: insert_one(operation)
OperationService → Redis: lpush(operation_queue, operation_id)
DatabaseService → Backend API: operation_object
Backend API → Frontend: 202 Accepted

[Background Process - Reconciliation]
Reconciliation Worker → MongoDB: Get databases with drift
MongoDB → Reconciliation Worker: database_list
Reconciliation Worker → K8s API: Get current state
K8s API → Reconciliation Worker: current_state
Reconciliation Worker → [Detect Drift]: Compare desired vs current
Reconciliation Worker → KubeDBService: create_vertical_scaling_ops_request()
KubeDBService → K8s API: Create OpsRequest
K8s API → KubeDB Operator: Watch Event
KubeDB Operator → K8s API: Update StatefulSet
KubeDB Operator → K8s API: Rolling Update Pods
Reconciliation Worker → MongoDB: Update status=UPDATING

[Background Process - Status Sync]
Status Sync Worker → K8s API: Get OpsRequest Status
K8s API → Status Sync Worker: OpsRequest(phase=Successful)
Status Sync Worker → K8s API: Get Pod Status
K8s API → Status Sync Worker: All Pods Ready
Status Sync Worker → MongoDB: Update status=RUNNING
Status Sync Worker → Redis: Cache new status
```

### 4.3 Backup Creation Sequence

```
User → Frontend: Trigger Backup
Frontend → Backend API: POST /api/v1/.../databases/{id}/backup
Backend API → DatabaseService: trigger_backup()
DatabaseService → DatabaseService: get_credentials()
DatabaseService → KubeDBService: create_backup_job_direct()
KubeDBService → K8s API: Create Job
K8s API → K8s Scheduler: Schedule Pod
K8s Scheduler → Worker Node: Start Container

[Inside Backup Container]
Backup Container → Database Pod: Connect (pg_dump/mongodump)
Database Pod → Backup Container: Data Stream
Backup Container → [Local Disk]: Write dump file
Backup Container → [Local Disk]: Compress (tar.gz)
Backup Container → AWS CLI: Install
Backup Container → S3: Upload dump.tar.gz
S3 → Backup Container: Upload Complete
Backup Container → K8s API: Exit Success

[Cleanup]
K8s API → Job Controller: TTL Expired (1 hour)
Job Controller → K8s API: Delete Job and Pod

DatabaseService → Backend API: backup_object
Backend API → Frontend: 202 Accepted
Frontend → User: Backup Initiated
```

---

## 5. Data Models

### 5.1 Pydantic Models

```python
# app/models/database.py

from datetime import datetime
from enum import Enum
from typing import Optional
from beanie import Document
from pydantic import Field

class DatabaseEngine(str, Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MONGODB = "mongodb"
    MARIADB = "mariadb"
    REDIS = "redis"
    ELASTICSEARCH = "elasticsearch"

class DatabaseStatus(str, Enum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    UPDATING = "updating"
    SCALING = "scaling"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"

class DatabaseSize(str, Enum):
    MICRO = "db.t3.micro"      # 1 vCPU, 1GB RAM
    SMALL = "db.t3.small"      # 1 vCPU, 2GB RAM
    MEDIUM = "db.t3.medium"    # 2 vCPU, 4GB RAM
    LARGE = "db.t3.large"      # 2 vCPU, 8GB RAM
    XLARGE = "db.t3.xlarge"    # 4 vCPU, 16GB RAM
    XXLARGE = "db.t3.2xlarge"  # 8 vCPU, 32GB RAM

class Database(Document):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    domain: str
    project: str

    # Configuration
    engine: DatabaseEngine
    version: str
    size: DatabaseSize
    storage_gb: int
    replicas: int

    # Status
    status: DatabaseStatus = DatabaseStatus.PENDING
    health_status: str = "unknown"
    phase: Optional[str] = None
    endpoint: Optional[str] = None
    port: Optional[int] = None
    ready_replicas: int = 0

    # Kubernetes
    namespace: str
    kubedb_resource_name: str
    provider_id: str

    # Backup
    backup_enabled: bool = True
    backup_schedule: Optional[str] = "daily"
    backup_retention_days: int = 30

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    deleted_at: Optional[datetime] = None

    class Settings:
        name = "databases"
        indexes = [
            IndexModel([("id", 1)], unique=True),
            IndexModel([("domain", 1), ("project", 1), ("name", 1)], unique=True),
            IndexModel([("status", 1), ("updated_at", -1)]),
            IndexModel([("provider_id", 1)]),
            IndexModel([("deleted_at", 1)], sparse=True),
        ]
```

---

## 6. Service Layer Details

### 6.1 Provider Selection Algorithm

```python
async def select_optimal_provider(
    self,
    engine: DatabaseEngine,
    version: str,
    region: Optional[str] = None,
    availability_zone: Optional[str] = None,
    required_resources: dict = None
) -> str:
    """
    Select optimal Kubernetes cluster for database deployment.

    Algorithm:
    1. Filter providers by region/AZ (if specified)
    2. Filter providers supporting engine + version
    3. Filter providers with sufficient capacity
    4. Sort by priority + available resources
    5. Return top provider

    Returns:
        str: provider_id

    Raises:
        NoProviderAvailableError: If no suitable provider found
    """
    # Build filter query
    filter_query = {"status": "active"}

    if region:
        filter_query["region"] = region
    if availability_zone:
        filter_query["availability_zone"] = availability_zone

    # Fetch matching providers
    providers = await self.provider_repo.find_many(filter_query)

    # Filter by engine support
    providers = [
        p for p in providers
        if self._supports_engine_version(p, engine, version)
    ]

    # Filter by capacity
    if required_resources:
        providers = [
            p for p in providers
            if self._has_sufficient_capacity(p, required_resources)
        ]

    if not providers:
        raise NoProviderAvailableError(
            f"No provider available for {engine} {version}"
        )

    # Sort by priority (desc) and available CPU (desc)
    providers.sort(
        key=lambda p: (p.priority, p.available_cpu),
        reverse=True
    )

    return providers[0].id
```

### 6.2 Backup Script Generation

```python
def _generate_backup_script(
    self,
    engine: DatabaseEngine,
    database_host: str,
    database_port: int,
    database_user: str,
    database_password: str,
    database_name: str,
    s3_bucket: str,
    s3_endpoint: str,
    backup_path: str
) -> str:
    """
    Generate bash script for database backup.

    Handles:
    - Native database dump (pg_dump, mongodump, mysqldump)
    - AWS CLI installation
    - S3 upload
    - Empty database handling

    Returns:
        str: Complete bash script
    """
    if engine == DatabaseEngine.MONGODB:
        return f"""
        set -e &&
        echo 'Starting MongoDB backup...' &&
        mongodump --host {database_host}:{database_port} \\
                  --username {database_user} \\
                  --password {database_password} \\
                  --authenticationDatabase admin \\
                  --db {database_name} \\
                  --out /backup/dump || mongodump --host {database_host}:{database_port} --out /backup/dump &&
        echo 'Backup dump completed' &&
        (apk add --no-cache aws-cli 2>/dev/null || (apt-get update -qq && apt-get install -y -qq awscli) 2>/dev/null || true) &&
        echo 'Compressing and uploading to S3...' &&
        cd /backup &&
        ([ -d dump ] && tar -czf dump.tar.gz dump/ || (mkdir -p dump && tar -czf dump.tar.gz dump/)) &&
        aws --endpoint-url={s3_endpoint} --no-verify-ssl s3 cp dump.tar.gz s3://{s3_bucket}/{backup_path}/dump.tar.gz &&
        echo 'Backup uploaded successfully to s3://{s3_bucket}/{backup_path}/dump.tar.gz'
        """
    # Similar for PostgreSQL, MySQL, etc.
```

---

## 7. Worker Algorithms

### 7.1 Reconciliation Algorithm (Pseudocode)

```
FUNCTION reconcile_databases():
    databases = get_active_databases(status=[RUNNING, UPDATING], limit=50)

    FOR EACH database IN databases:
        TRY:
            # Fetch current state
            current_state = kubedb_service.get_database_status(database)

            # Detect drift
            drift = detect_drift(database.desired_state, current_state)

            IF drift IS NOT EMPTY:
                # Check for active operations
                active_ops = check_active_operations(database.id)

                IF active_ops IS EMPTY:
                    # Create remediation operation
                    IF drift.replicas:
                        create_horizontal_scaling_ops_request(database, drift.replicas)

                    IF drift.resources:
                        create_vertical_scaling_ops_request(database, drift.resources)

                    IF drift.version:
                        create_version_update_ops_request(database, drift.version)

                    # Update status
                    database.status = UPDATING
                    save(database)
                ELSE:
                    LOG("Skipping reconciliation: active operation exists")

            ELSE:
                LOG("No drift detected for database")

        CATCH exception:
            LOG_ERROR("Reconciliation failed", database.id, exception)
            CONTINUE

FUNCTION detect_drift(desired_state, current_state):
    drift = {}

    # Check replicas
    IF desired_state.replicas != current_state.replicas:
        drift["replicas"] = {
            "desired": desired_state.replicas,
            "current": current_state.replicas
        }

    # Check resources (CPU/Memory)
    desired_resources = get_resource_limits(desired_state.size)
    current_resources = current_state.resources

    IF desired_resources != current_resources:
        drift["resources"] = {
            "desired": desired_resources,
            "current": current_resources
        }

    # Check version
    IF desired_state.version != current_state.version:
        drift["version"] = {
            "desired": desired_state.version,
            "current": current_state.version
        }

    RETURN drift
```

### 7.2 Status Sync Algorithm (Pseudocode)

```
FUNCTION sync_all_statuses():
    databases = get_all_databases()

    # Process in parallel
    FOR EACH database IN databases CONCURRENTLY:
        TRY:
            # Fetch Kubernetes state
            k8s_status = kubedb_service.get_database_status(database)

            # Extract information
            phase = k8s_status.status.phase
            ready_replicas = k8s_status.status.readyReplicas
            endpoint = k8s_status.status.primaryServiceDNS
            conditions = k8s_status.status.conditions

            # Check for active OpsRequests
            active_ops = get_active_ops_requests(database)

            # Map phase to status with safeguards
            new_status = map_phase_to_status(
                phase=phase,
                old_status=database.status,
                active_ops=active_ops
            )

            # Update database
            database.status = new_status
            database.phase = phase
            database.ready_replicas = ready_replicas
            database.endpoint = endpoint
            database.updated_at = NOW()

            save(database)

            # Cache in Redis
            cache_status(database)

        CATCH exception:
            LOG_ERROR("Status sync failed", database.id, exception)

FUNCTION map_phase_to_status(phase, old_status, active_ops):
    # Critical safeguard: UPDATING should never go to FAILED based on phase alone
    IF old_status == UPDATING AND phase == "Failed":
        LOG_WARNING("Database was UPDATING but phase is Failed - keeping UPDATING")
        RETURN UPDATING

    # If active OpsRequest exists, keep UPDATING status
    IF phase == "Failed" AND active_ops IS NOT EMPTY:
        LOG_WARNING("Phase is Failed but active OpsRequest exists - setting UPDATING")
        RETURN UPDATING

    # Normal phase mapping
    phase_mapping = {
        "Provisioning": PROVISIONING,
        "Ready": RUNNING,
        "Running": RUNNING,
        "Failed": FAILED,
        "Halted": PAUSED
    }

    RETURN phase_mapping.get(phase, PENDING)
```

---

## 8. Error Handling

### 8.1 Exception Hierarchy

```python
# app/exceptions/__init__.py

class DBaaSException(Exception):
    """Base exception for all DBaaS errors."""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class ResourceNotFoundError(DBaaSException):
    """Resource not found (404)."""
    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            f"{resource_type} with id '{resource_id}' not found",
            status_code=404
        )

class ValidationError(DBaaSException):
    """Request validation failed (400)."""
    def __init__(self, message: str, details: dict = None):
        super().__init__(message, status_code=400)
        self.details = details

class QuotaExceededError(DBaaSException):
    """Resource quota exceeded (429)."""
    def __init__(self, resource: str, limit: int, requested: int):
        super().__init__(
            f"Quota exceeded for {resource}: limit={limit}, requested={requested}",
            status_code=429
        )

class ProviderError(DBaaSException):
    """Provider/cluster error (503)."""
    def __init__(self, provider_id: str, message: str):
        super().__init__(
            f"Provider {provider_id} error: {message}",
            status_code=503
        )

class KubeDBError(DBaaSException):
    """KubeDB operation failed (500)."""
    def __init__(self, message: str, k8s_error: Exception = None):
        super().__init__(f"KubeDB operation failed: {message}", status_code=500)
        self.k8s_error = k8s_error

class OperationInProgressError(DBaaSException):
    """Operation already in progress (409)."""
    def __init__(self, database_id: str, operation_type: str):
        super().__init__(
            f"Operation {operation_type} already in progress for database {database_id}",
            status_code=409
        )
```

### 8.2 Global Exception Handler

```python
# app/main.py

@app.exception_handler(DBaaSException)
async def dbaas_exception_handler(request: Request, exc: DBaaSException):
    """Handle all DBaaS-specific exceptions."""
    logger.error(
        "dbaas_exception",
        path=request.url.path,
        method=request.method,
        error=exc.message,
        status_code=exc.status_code
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.__class__.__name__,
            "message": exc.message,
            "path": str(request.url.path),
            "timestamp": datetime.utcnow().isoformat()
        }
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic validation errors."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "ValidationError",
            "message": "Request validation failed",
            "details": exc.errors(),
            "path": str(request.url.path)
        }
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    logger.error(
        "unexpected_exception",
        path=request.url.path,
        error=str(exc),
        traceback=traceback.format_exc()
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": "InternalServerError",
            "message": "An unexpected error occurred",
            "path": str(request.url.path)
        }
    )
```

---

## 9. Configuration Management

### 9.1 Settings Class

```python
# app/config/settings.py

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    # Application
    app_name: str = "KubeDB DBaaS Platform"
    app_version: str = "1.0.0"
    environment: str = "development"  # development, staging, production

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4

    # MongoDB
    mongodb_url: str
    mongodb_database: str = "kubedb_dbaas"
    mongodb_max_pool_size: int = 20
    mongodb_min_pool_size: int = 2

    # Redis
    redis_url: str
    redis_max_connections: int = 20

    # Security
    secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30

    # Backup & Restore
    backup_enabled: bool = True
    backup_s3_bucket: str = None
    backup_s3_region: str = "us-east-1"
    backup_s3_endpoint: str = None
    backup_s3_access_key_id: str = None
    backup_s3_secret_access_key: str = None

    # Background Workers
    reconcile_interval: int = 300  # 5 minutes
    status_sync_interval: int = 30  # 30 seconds
    num_operation_workers: int = 3

    # CORS
    cors_origins: list = ["http://localhost:3000", "http://localhost:5173"]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

# Global settings instance
settings = Settings()
```

---

## 10. Testing Strategy

### 10.1 Unit Tests

```python
# tests/unit/services/test_database_service.py

import pytest
from unittest.mock import AsyncMock, Mock
from app.services.database_service import DatabaseService
from app.models.database import Database, DatabaseEngine, DatabaseStatus

@pytest.mark.asyncio
async def test_create_database_success():
    # Arrange
    mock_repo = Mock()
    mock_repo.save = AsyncMock(return_value=Mock(id="db-123"))

    mock_kubedb = Mock()
    mock_kubedb.create_database_resource = AsyncMock(return_value={"status": "ok"})

    service = DatabaseService(
        database_repository=mock_repo,
        kubedb_service=mock_kubedb,
        provider_service=Mock(),
        redis_connection=Mock()
    )

    request = Mock(
        name="test-db",
        engine=DatabaseEngine.POSTGRES,
        version="15.0"
    )

    # Act
    result = await service.create_database("domain1", "project1", request)

    # Assert
    assert result.id == "db-123"
    assert result.status == DatabaseStatus.PROVISIONING
    mock_kubedb.create_database_resource.assert_called_once()

@pytest.mark.asyncio
async def test_create_database_provider_error():
    # Arrange
    service = DatabaseService(...)
    service.provider_service.select_optimal_provider = AsyncMock(
        side_effect=NoProviderAvailableError("No providers")
    )

    # Act & Assert
    with pytest.raises(NoProviderAvailableError):
        await service.create_database("domain1", "project1", request)
```

### 10.2 Integration Tests

```python
# tests/integration/test_database_lifecycle.py

@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_database_lifecycle(test_client, mongodb_connection):
    # Create database
    response = test_client.post(
        "/api/v1/domains/test/projects/test/databases",
        json={
            "name": "integration-test-db",
            "engine": "postgres",
            "version": "15.0",
            "size": "db.t3.micro"
        }
    )
    assert response.status_code == 201
    database_id = response.json()["database_id"]

    # Wait for provisioning (poll status)
    await wait_for_status(database_id, DatabaseStatus.RUNNING, timeout=300)

    # Scale database
    response = test_client.put(
        f"/api/v1/domains/test/projects/test/databases/{database_id}/scale",
        json={"scale_type": "vertical", "size": "db.t3.small"}
    )
    assert response.status_code == 202

    # Wait for scaling
    await wait_for_status(database_id, DatabaseStatus.RUNNING, timeout=600)

    # Delete database
    response = test_client.delete(
        f"/api/v1/domains/test/projects/test/databases/{database_id}"
    )
    assert response.status_code == 202
```

### 10.3 End-to-End Tests

```python
# tests/e2e/test_backup_restore.py

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_backup_and_restore(test_client, k8s_cluster):
    # Create database
    db_id = await create_test_database("backup-test-db", "postgres", "15.0")

    # Insert test data
    await insert_test_data(db_id, rows=1000)

    # Trigger backup
    response = test_client.post(
        f"/api/v1/domains/test/projects/test/databases/{db_id}/backup"
    )
    backup_id = response.json()["backup_id"]

    # Wait for backup completion
    await wait_for_backup_completion(backup_id, timeout=600)

    # Verify backup in S3
    assert s3_backup_exists(backup_id)

    # Restore to new database
    new_db_id = await restore_from_backup(backup_id, "restored-db")

    # Verify data
    original_data = await query_database(db_id, "SELECT COUNT(*) FROM test_table")
    restored_data = await query_database(new_db_id, "SELECT COUNT(*) FROM test_table")

    assert original_data == restored_data == 1000
```

---

## Appendix

### A. Code Organization

```
app/
├── __init__.py
├── main.py                     # FastAPI application entry point
│
├── api/                        # API layer
│   └── v1/
│       ├── __init__.py
│       ├── databases.py        # Database CRUD endpoints
│       ├── providers.py        # Provider management
│       ├── operations.py       # Operation status
│       ├── versions.py         # Version management
│       ├── health.py           # Health checks
│       └── auth.py             # Authentication
│
├── services/                   # Business logic layer
│   ├── __init__.py
│   ├── database_service.py    # Database lifecycle
│   ├── kubedb_service.py      # K8s/KubeDB interactions
│   ├── provider_service.py    # Provider selection
│   ├── status_sync_service.py # Status synchronization
│   ├── metrics_service.py     # Metrics collection
│   └── operation_queue.py     # Operation management
│
├── workers/                    # Background workers
│   ├── __init__.py
│   ├── reconciliation_worker.py  # Drift detection
│   └── status_sync_worker.py     # Status updates
│
├── repositories/               # Data access layer
│   ├── __init__.py
│   ├── database_repository.py
│   ├── provider_repository.py
│   └── operation_repository.py
│
├── models/                     # Data models
│   ├── __init__.py
│   ├── database.py            # Database model
│   ├── provider.py            # Provider model
│   └── operation.py           # Operation model
│
├── config/                     # Configuration
│   ├── __init__.py
│   ├── settings.py            # Environment settings
│   ├── database.py            # MongoDB connection
│   ├── redis.py               # Redis connection
│   └── logging.py             # Logging config
│
├── exceptions/                 # Custom exceptions
│   └── __init__.py
│
└── utils/                      # Utilities
    ├── __init__.py
    └── helpers.py
```

### B. Environment Variables

```bash
# Required
MONGODB_URL=mongodb://localhost:27017
MONGODB_DATABASE=kubedb_dbaas
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=your-secret-key-min-32-chars

# Optional
ENVIRONMENT=production
LOG_LEVEL=INFO
RECONCILIATION_INTERVAL=300
STATUS_SYNC_INTERVAL=30

# Backup
BACKUP_ENABLED=true
BACKUP_S3_BUCKET=ashok21
BACKUP_S3_REGION=south-az2
BACKUP_S3_ENDPOINT=https://south-az2-s3.cloud.airtel.in:10444
BACKUP_S3_ACCESS_KEY_ID=***
BACKUP_S3_SECRET_ACCESS_KEY=***

# CORS
CORS_ORIGINS=["http://localhost:3000","http://localhost:5173"]
```

---

**Document Status**: Draft v1.0
**Last Updated**: December 23, 2025
**Next Review**: Q1 2026
