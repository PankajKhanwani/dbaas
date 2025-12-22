# KubeDB DBaaS Platform - Architecture & Network Topology

## Table of Contents
1. [System Overview](#system-overview)
2. [Network Topology](#network-topology)
3. [Component Architecture](#component-architecture)
4. [Data Flow](#data-flow)
5. [Monitoring Architecture](#monitoring-architecture)
6. [Security & Multi-tenancy](#security--multi-tenancy)

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         KubeDB DBaaS Platform                                │
│                    Database-as-a-Service on Kubernetes                       │
└─────────────────────────────────────────────────────────────────────────────┘

Components:
├── Frontend (React)          - User Interface
├── Backend API (FastAPI)     - REST API & Business Logic (Single Pod)
│   ├── API Server            - FastAPI endpoints
│   ├── Operation Workers     - Process async operations (3 concurrent)
│   ├── Reconciliation Worker - Detect & fix drift (leader election)
│   └── Status Sync Service  - Sync database status from K8s
├── MongoDB                   - Metadata Storage (databases, operations, audit logs)
├── Redis                     - Operation Queue & Caching
├── Multi-Provider Support    - Manage databases across multiple K8s clusters
│   ├── Provider Management  - Onboard clusters with kubeconfigs
│   └── Provider Selection   - Auto-select based on resources/region
├── KubeDB Operator           - Database Provisioning (per cluster)
├── Kubernetes Clusters       - Multiple clusters (providers)
└── Managed Databases         - MongoDB, PostgreSQL, MySQL, MariaDB, Redis, ES
```

---

## Network Topology

### High-Level Network Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              INTERNET                                        │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
                                 │ HTTPS (443)
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         LOAD BALANCER / INGRESS                              │
│                      (nginx-ingress / cloud LB)                              │
│                                                                               │
│  Endpoints:                                                                   │
│  ├─ https://dbaas.example.com       → Frontend (React SPA)                  │
│  └─ https://api.dbaas.example.com   → Backend API                           │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
                ┌────────────────┴────────────────┐
                │                                  │
                ▼                                  ▼
┌─────────────────────────────┐  ┌──────────────────────────────────────────┐
│      FRONTEND PODS          │  │         BACKEND API PODS                  │
│   (React - Port 3000)       │  │      (FastAPI - Port 8000)               │
│                             │  │                                           │
│  ┌─────────┐  ┌─────────┐ │  │  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │  Pod 1  │  │  Pod 2  │ │  │  │  Pod 1  │  │  Pod 2  │  │  Pod 3  │ │
│  └─────────┘  └─────────┘ │  │  └─────────┘  └─────────┘  └─────────┘ │
│                             │  │                                           │
│  Service: frontend-svc      │  │  Service: api-svc                        │
│  ClusterIP: 10.0.1.100      │  │  ClusterIP: 10.0.1.101                   │
└─────────────────────────────┘  └──────────────┬───────────────────────────┘
                                                 │
                    ┌────────────────────────────┼────────────────────────────┐
                    │                            │                            │
                    ▼                            ▼                            ▼
      ┌─────────────────────┐    ┌──────────────────────┐    ┌────────────────────────┐
      │   MONGODB (Meta)    │    │      REDIS CACHE     │    │  KUBERNETES API        │
      │   Port: 27017       │    │     Port: 6379       │    │    Port: 6443          │
      │                     │    │                      │    │                        │
      │  Service:           │    │  Service:            │    │  Used for:             │
      │  mongodb-svc        │    │  redis-svc           │    │  - Database CRUD       │
      │  ClusterIP:         │    │  ClusterIP:          │    │  - Status monitoring   │
      │  10.0.2.100         │    │  10.0.2.101          │    │  - Metrics collection  │
      └─────────────────────┘    └──────────────────────┘    └────────────────────────┘
                                                                        │
                                                                        │ Manages
                                                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         KUBEDB OPERATOR NAMESPACE                            │
│                                                                               │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │  KubeDB Operator Pod                                                │    │
│  │  - Watches for Database CRDs                                        │    │
│  │  - Provisions StatefulSets, Services, PVCs                          │    │
│  │  - Manages Backup & Restore                                         │    │
│  └────────────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ Creates & Manages
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    MANAGED DATABASES NAMESPACE (default)                     │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  MongoDB Database: "customer-db-domain-project"                      │   │
│  │                                                                       │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐                          │   │
│  │  │ Primary  │  │ Secondary│  │ Secondary│  (ReplicaSet)             │   │
│  │  │  Pod 0   │  │  Pod 1   │  │  Pod 2   │                          │   │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘                          │   │
│  │       │             │             │                                  │   │
│  │       └─────────────┴─────────────┘                                 │   │
│  │                     │                                                │   │
│  │  Services:          ▼                                                │   │
│  │  ├─ customer-db-domain-project (ClusterIP: 10.100.1.50)            │   │
│  │  │  └─ Port: 27017 (Database connection)                            │   │
│  │  │                                                                   │   │
│  │  ├─ customer-db-domain-project-pods (Headless)                      │   │
│  │  │  └─ For ReplicaSet internal communication                        │   │
│  │  │                                                                   │   │
│  │  └─ customer-db-domain-project-stats (ClusterIP: 10.100.1.51)      │   │
│  │     └─ Port: 56790 (Prometheus metrics exporter)                    │   │
│  │                                                                       │   │
│  │  Storage:                                                            │   │
│  │  └─ PersistentVolumeClaims (one per pod)                            │   │
│  │     ├─ data-customer-db-0 (10Gi)                                    │   │
│  │     ├─ data-customer-db-1 (10Gi)                                    │   │
│  │     └─ data-customer-db-2 (10Gi)                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  PostgreSQL Database: "analytics-db-domain-project"                  │   │
│  │                                                                       │   │
│  │  ┌──────────┐  ┌──────────┐                                         │   │
│  │  │ Primary  │  │ Standby  │  (Streaming Replication)                │   │
│  │  │  Pod 0   │  │  Pod 1   │                                         │   │
│  │  └────┬─────┘  └────┬─────┘                                         │   │
│  │       │             │                                                │   │
│  │  Services:          │                                                │   │
│  │  ├─ analytics-db-domain-project (ClusterIP: 10.100.2.50)           │   │
│  │  │  └─ Port: 5432 (Database connection)                             │   │
│  │  │                                                                   │   │
│  │  └─ analytics-db-domain-project-stats (ClusterIP: 10.100.2.51)     │   │
│  │     └─ Port: 56790 (Prometheus metrics exporter)                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                               │
│  [Additional databases: MySQL, MariaDB, Redis, Elasticsearch...]            │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Network Policies & Security Groups

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          NETWORK SEGMENTATION                                │
└─────────────────────────────────────────────────────────────────────────────┘

Frontend Namespace:
├─ Ingress: ALLOW from Internet (443)
├─ Egress:  ALLOW to Backend API only
└─ Deny:    All other traffic

Backend API Namespace:
├─ Ingress: ALLOW from Frontend + Ingress Controller
├─ Egress:  ALLOW to MongoDB, Redis, K8s API, Managed DBs
└─ Deny:    Direct internet access (except configured)

Data Namespace (MongoDB Metadata):
├─ Ingress: ALLOW from Backend API only
├─ Egress:  DENY all
└─ Isolation: Complete network isolation

Cache Namespace (Redis):
├─ Ingress: ALLOW from Backend API only
├─ Egress:  DENY all
└─ Isolation: Complete network isolation

Managed Databases Namespace:
├─ Ingress: ALLOW from Backend API only (for operations)
│           ALLOW from Customer VPCs (via LoadBalancer)
├─ Egress:  ALLOW for replication, backup
└─ Isolation: Inter-database isolation via NetworkPolicy
```

---

## Component Architecture

### Backend API (FastAPI) - Single Pod Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BACKEND API POD (Single Process)                     │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI Application (Uvicorn)                                       │   │
│  │  Port: 8000                                                           │   │
│  └──────────────────────────┬────────────────────────────────────────────┘   │
│                             │                                                │
│  ┌──────────────────────────┴────────────────────────────────────────────┐ │
│  │                      API Layer                                         │ │
│  │  /api/v1/                                                             │ │
│  │  ├─ /auth          - Authentication & Authorization                   │ │
│  │  ├─ /databases     - CRUD operations                                  │ │
│  │  ├─ /operations    - Operation status & history                       │ │
│  │  ├─ /providers     - Multi-provider management                        │ │
│  │  ├─ /tenants       - Tenant management                                │ │
│  │  ├─ /versions      - Available DB versions                           │ │
│  │  └─ /health        - Health checks                                    │ │
│  └──────────────────────────┬────────────────────────────────────────────┘ │
│                             │                                                │
│  ┌──────────────────────────┴────────────────────────────────────────────┐ │
│  │                   Service Layer                                        │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │ DatabaseService     │  │  KubeDBService        │                   │ │
│  │  │ - Business Logic    │  │  - Multi-provider     │                   │ │
│  │  │ - Validation        │  │  - K8s API Client    │                   │ │
│  │  │ - Quota Management  │  │  - CRD Management     │                   │ │
│  │  └─────────────────────┘  └──────────────────────┘                   │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │ ProviderService     │  │  AuditService         │                   │ │
│  │  │ - Provider CRUD     │  │  - Audit Logging      │                   │ │
│  │  │ - Kubeconfig Mgmt   │  │  - All Operations     │                   │ │
│  │  └─────────────────────┘  └──────────────────────┘                   │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │ MetricsCacheService │  │  PrometheusService    │                   │ │
│  │  │ - Multi-layer Cache │  │  - Metrics Queries    │                   │ │
│  │  │ - Rate Limiting     │  │  - PromQL Builder     │                   │ │
│  │  │ - Circuit Breaker   │  │                        │                   │ │
│  │  └─────────────────────┘  └──────────────────────┘                   │ │
│  └──────────────────────────┬────────────────────────────────────────────┘ │
│                             │                                                │
│  ┌──────────────────────────┴────────────────────────────────────────────┐ │
│  │              Background Workers (Same Process)                          │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │ Operation Workers  │  │  Reconciliation      │                   │ │
│  │  │ (3 concurrent)      │  │  Worker              │                   │ │
│  │  │ - Process queue     │  │  - Leader Election   │                   │ │
│  │  │ - Create OpsRequest│  │  - Detect drift      │                   │ │
│  │  │ - Monitor progress  │  │  - Create operations │                   │ │
│  │  └─────────────────────┘  └──────────────────────┘                   │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐                                              │ │
│  │  │  Status Sync Service│                                              │ │
│  │  │  - Sync from K8s   │                                              │ │
│  │  │  - Update status   │                                              │ │
│  │  │  - All pods run    │                                              │ │
│  │  └─────────────────────┘                                              │ │
│  └──────────────────────────┬────────────────────────────────────────────┘ │
│                             │                                                │
│  ┌──────────────────────────┴────────────────────────────────────────────┐ │
│  │                   Data Layer                                           │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │  MongoDB (Beanie)   │  │  Redis                │                   │ │
│  │  │  - Database Models  │  │  - Operation Queue   │                   │ │
│  │  │  - Operation Models │  │  - Cache              │                   │ │
│  │  │  - Audit Logs       │  │  - Rate Limiting      │                   │ │
│  │  │  - Provider Models   │  │                        │                   │ │
│  │  └─────────────────────┘  └──────────────────────┘                   │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐                                              │ │
│  │  │  Kubernetes Clients │                                              │ │
│  │  │  (Per Provider)     │                                              │ │
│  │  │  - Cached by ID     │                                              │ │
│  │  │  - TTL-based cleanup│                                              │ │
│  │  └─────────────────────┘                                              │ │
│  └─────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘
```

### Database Provisioning Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    DATABASE PROVISIONING SEQUENCE                            │
└─────────────────────────────────────────────────────────────────────────────┘

1. User Request
   ↓
   POST /api/v1/domain/demo/project/demo/databases
   {
     "name": "myapp-db",
     "engine": "mongodb",
     "version": "8.0.4",
     "size": "db.t3.small",
     "storage_gb": 10,
     "replicas": 3
   }

2. Backend API Processing
   ↓
   FastAPI receives request
   │
   ├─→ Validate request (DatabaseService)
   │   ├─ Check domain/project permissions
   │   ├─ Validate engine & version
   │   └─ Check resource quotas
   │
   ├─→ Generate unique identifiers
   │   ├─ Database ID: db-{uuid}
   │   └─ KubeDB Resource Name: myapp-db-demo-demo
   │
   ├─→ Store in MongoDB
   │   └─ Status: "provisioning"
   │
   └─→ Call KubeDB Service

3. Kubernetes Resource Creation
   ↓
   KubeDBService.create_database()
   │
   ├─→ Build Custom Resource Spec
   │   ├─ apiVersion: kubedb.com/v1alpha2
   │   ├─ kind: MongoDB
   │   ├─ metadata:
   │   │   name: myapp-db-demo-demo
   │   │   namespace: default
   │   │   labels: {domain, project, engine}
   │   └─ spec:
   │       ├─ version: "8.0.4"
   │       ├─ replicas: 3
   │       ├─ storage: 10Gi
   │       ├─ podTemplate: {resources, affinity}
   │       └─ terminationPolicy: WipeOut
   │
   ├─→ Apply to Kubernetes API
   │   └─ kubectl apply -f mongodb-crd.yaml
   │
   └─→ Patch auth secret (optional custom credentials)

4. KubeDB Operator Processing
   ↓
   Operator watches MongoDB CRD
   │
   ├─→ Create StatefulSet
   │   ├─ Name: myapp-db-demo-demo
   │   ├─ Replicas: 3 pods
   │   ├─ Image: mongo:8.0.4
   │   └─ Init containers for setup
   │
   ├─→ Create Services
   │   ├─ myapp-db-demo-demo (ClusterIP)
   │   ├─ myapp-db-demo-demo-pods (Headless)
   │   └─ myapp-db-demo-demo-stats (Metrics)
   │
   ├─→ Create PersistentVolumeClaims
   │   ├─ data-myapp-db-0 (10Gi)
   │   ├─ data-myapp-db-1 (10Gi)
   │   └─ data-myapp-db-2 (10Gi)
   │
   ├─→ Create Secrets
   │   └─ myapp-db-demo-demo-auth (username, password)
   │
   └─→ Configure ReplicaSet
       └─ Initialize MongoDB replica set

5. Status Monitoring (Background)
   ↓
   Backend API polls K8s API every 30s
   │
   ├─→ Check CRD status
   │   ├─ Phase: Provisioning → Ready
   │   ├─ Ready replicas: 3/3
   │   └─ Conditions: [...]
   │
   ├─→ Get endpoint information
   │   ├─ Service IP: 10.100.1.50
   │   └─ Port: 27017
   │
   └─→ Update MongoDB record
       └─ Status: "provisioning" → "running"

6. User Response
   ↓
   {
     "id": "db-abc123",
     "name": "myapp-db",
     "engine": "mongodb",
     "status": "running",
     "endpoint": "10.100.1.50",
     "port": 27017,
     "created_at": "2025-11-25T..."
   }
```

---

## Multi-Provider Architecture

### Provider Management

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      MULTI-PROVIDER ARCHITECTURE                              │
└─────────────────────────────────────────────────────────────────────────────┘

Provider (Kubernetes Cluster):
├── Onboarding
│   ├── POST /api/v1/providers
│   ├── Upload kubeconfig (content or path)
│   ├── Specify region, availability zone
│   ├── Set resource capacity (CPU, memory, storage)
│   └── Configure reservation limits
│
├── Provider Selection
│   ├── Automatic selection based on:
│   │   ├─ Resource availability
│   │   ├─ Region/availability zone
│   │   ├─ Priority (higher = preferred)
│   │   ├─ Maintenance status
│   │   └─ Tags/metadata
│   └── Manual selection via provider_id
│
└── Kubernetes Client Management
    ├── Per-provider client caching
    ├── TTL-based cleanup (1 hour default)
    ├── Automatic reconnection on failure
    └── SSL verification configurable per provider

Database → Provider Association:
├── Each database linked to one provider
├── All operations use provider's kubeconfig
├── Provider can be changed (migration)
└── Provider deletion requires database migration
```

### Provider Resource Management

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PROVIDER RESOURCE TRACKING                                │
└─────────────────────────────────────────────────────────────────────────────┘

Provider Resource Model:
├── Total Capacity
│   ├── cpu_total_cores: 100.0
│   ├── memory_total_gb: 200.0
│   └── storage_total_gb: 1000.0
│
├── Allocated Resources
│   ├── cpu_allocated_cores: 45.0 (tracked)
│   ├── memory_allocated_gb: 90.0 (tracked)
│   └── storage_allocated_gb: 450.0 (tracked)
│
├── Reservation Limits (safety margin)
│   ├── cpu_reservation_percent: 80% (max 80 cores allocatable)
│   ├── memory_reservation_percent: 80% (max 160GB allocatable)
│   └── storage_reservation_percent: 80% (max 800GB allocatable)
│
└── Available Resources
    ├── Available CPU = (100 * 0.8) - 45 = 35 cores
    ├── Available Memory = (200 * 0.8) - 90 = 70GB
    └── Available Storage = (1000 * 0.8) - 450 = 350GB

Database Creation Flow:
1. User requests database with size "db.t3.medium" (2 CPU, 4GB RAM)
2. ProviderSelector finds providers with available resources
3. Selects provider with highest priority and sufficient capacity
4. Creates database in selected provider's cluster
5. Updates provider's allocated resources
```

---

## Operation Queue & Workers

### Asynchronous Operation Processing

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    OPERATION QUEUE ARCHITECTURE                              │
└─────────────────────────────────────────────────────────────────────────────┘

Operation Types:
├── SCALE_HORIZONTAL    - Change replica count
├── SCALE_VERTICAL      - Change instance size (CPU/memory)
├── EXPAND_STORAGE      - Increase storage size
├── UPGRADE_VERSION     - Upgrade database version
├── BACKUP              - Create backup
├── RESTORE             - Restore from backup
├── PAUSE               - Pause database
└── RESUME              - Resume paused database

Operation Lifecycle:
1. API Endpoint
   ↓ Creates Operation record (status: QUEUED)
   ↓ Enqueues to Redis queue
   ↓ Returns operation_id to user
   
2. Operation Worker (3 concurrent workers)
   ↓ Dequeues operation from Redis
   ↓ Updates status: IN_PROGRESS
   ↓ Executes operation (creates OpsRequest, monitors)
   ↓ Updates status: COMPLETED or FAILED
   
3. Status Sync Service
   ↓ Polls K8s for actual state
   ↓ Updates database.current_* fields
   ↓ Detects completion
```

### Operation Worker Details

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      OPERATION WORKER PROCESS                                │
└─────────────────────────────────────────────────────────────────────────────┘

Operation Worker (3 concurrent instances):
├── Dequeue from Redis (priority-based)
├── Process operation:
│   ├── Get database & provider info
│   ├── Create KubeDB OpsRequest
│   ├── Monitor OpsRequest progress
│   ├── Update operation status
│   └── Update database state on completion
│
└── Error Handling:
    ├── Retry on transient failures (max 3 retries)
    ├── Mark as FAILED on permanent errors
    ├── Log errors for debugging
    └── Update operation.error_message

Reconciliation Worker (Leader Election):
├── Only one instance runs (leader)
├── Detects drift between desired vs current state
├── Creates operations for:
│   ├── Replica count changes
│   ├── Size changes
│   └── Storage expansion
└── Runs every 30 seconds (configurable)

Status Sync Service (All Pods):
├── Runs in all pod instances
├── Syncs database status from K8s
├── Updates current_replicas, current_size, current_storage_gb
├── Updates database version from K8s CR
└── Runs every 30 seconds (configurable)
```

---

## Audit Logging

### Comprehensive Operation Tracking

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         AUDIT LOGGING SYSTEM                                 │
└─────────────────────────────────────────────────────────────────────────────┘

Audit Log Model:
├── action              - Operation type (database.create, database.scale, etc.)
├── resource_type       - "database", "provider", "tenant"
├── resource_id         - ID of the resource
├── domain & project    - Multi-tenancy context
├── user_id             - User who performed action
├── user_email          - User email
├── ip_address          - Request IP
├── user_agent          - Client user agent
├── details             - Operation-specific details (JSON)
├── timestamp           - When action occurred
└── status              - Success/failure

Audited Operations:
├── Database Operations
│   ├── database.create
│   ├── database.update
│   ├── database.delete
│   ├── database.scale
│   ├── database.pause
│   ├── database.resume
│   ├── database.upgrade_version
│   ├── database.trigger_backup
│   └── database.restore
│
├── Provider Operations
│   ├── provider.create
│   ├── provider.update
│   └── provider.delete
│
└── Tenant Operations
    ├── tenant.create
    └── tenant.update

Storage:
├── MongoDB collection: "audit_logs"
├── Indexed on: action, domain, project, timestamp, resource_type
├── Retention: Configurable (default: 1 year)
└── Queryable via API: GET /api/v1/audit-logs
```

---

## Data Flow

### Database Creation Flow

```
┌─────────┐      ┌─────────┐      ┌──────────┐      ┌────────────┐      ┌──────────┐
│ Browser │ ───→ │ React   │ ───→ │  FastAPI │ ───→ │  MongoDB   │ ───→ │  K8s API │
│  (User) │      │   App   │      │    API   │      │ (Metadata) │      │  Server  │
└─────────┘      └─────────┘      └──────────┘      └────────────┘      └──────────┘
     │                │                  │                  │                   │
     │ 1. Fill Form   │                  │                  │                   │
     ├────────────────→                  │                  │                   │
     │                │                  │                  │                   │
     │ 2. POST /databases                │                  │                   │
     │                ├──────────────────→                  │                   │
     │                │                  │ 3. Select Provider                  │
     │                │                  │    (auto-select or manual)           │
     │                │                  │                  │                   │
     │                │                  │ 4. Save record   │                   │
     │                │                  ├──────────────────→                   │
     │                │                  │                  │ 5. Create CRD     │
     │                │                  │                  ├───────────────────→
     │                │                  │                  │                   │
     │                │                  │                  │  ┌──────────────┐ │
     │                │                  │                  │  │ KubeDB       │ │
     │                │                  │                  │  │ Operator     │ │
     │                │                  │                  │  │ (watches)    │ │
     │                │                  │                  │  └──────┬───────┘ │
     │                │                  │                  │         │         │
     │                │                  │                  │  6. Provisions    │
     │                │                  │                  │  - StatefulSet    │
     │                │                  │                  │  - Services       │
     │                │                  │                  │  - PVCs           │
     │                │                  │                  │  - Secrets        │
     │                │                  │                  │         │         │
     │                │ 7. Return 201    │                  │         ▼         │
     │                │ Created          │                  │  ┌──────────────┐ │
     │                │◀─────────────────┤                  │  │  Database    │ │
     │ 8. Show success│                  │                  │  │  Running     │ │
     │◀───────────────┤                  │                  │  └──────────────┘ │
     │                │                  │                  │                   │
     │                │ 9. Poll /status  │                  │                   │
     │                ├──────────────────→                  │                   │
     │                │                  │ 10. Status Sync │                   │
     │                │                  │     (background) │                   │
     │                │                  │     polls K8s   │                   │
     │                │                  ├──────────────────────────────────────→
     │                │                  │ status           │                   │
     │                │◀─────────────────┤                  │                   │
     │ 11. Update UI  │                  │                  │                   │
     │◀───────────────┤                  │                  │                   │
```

### Database Scaling Flow (Operation Queue)

```
┌─────────┐      ┌─────────┐      ┌──────────┐      ┌────────────┐      ┌──────────┐
│ Browser │ ───→ │ React   │ ───→ │  FastAPI │ ───→ │   Redis    │ ───→ │  Worker  │
│  (User) │      │   App   │      │    API   │      │   Queue    │      │          │
└─────────┘      └─────────┘      └──────────┘      └────────────┘      └──────────┘
     │                │                  │                  │                   │
     │ 1. Scale Request                  │                  │                   │
     │    (replicas: 3)                   │                  │                   │
     ├────────────────→                  │                  │                   │
     │                │                  │                  │                   │
     │ 2. POST /scale │                  │                  │                   │
     │                ├──────────────────→                  │                   │
     │                │                  │ 3. Create Operation                │
     │                │                  │    (status: QUEUED)                 │
     │                │                  │                  │                   │
     │                │                  │ 4. Enqueue to Redis                │
     │                │                  ├──────────────────→                   │
     │                │                  │                  │ 5. Worker dequeues│
     │                │                  │                  ├───────────────────→
     │                │                  │                  │                   │
     │                │ 6. Return 202    │                  │ 6. Process operation│
     │                │ Accepted          │                  │    - Create OpsRequest│
     │                │◀─────────────────┤                  │    - Monitor progress│
     │ 7. Show status │                  │                  │                   │
     │◀───────────────┤                  │                  │                   │
     │                │                  │                  │ 7. Update status │
     │                │                  │                  │    COMPLETED      │
     │                │ 8. Poll /operations/{id}            │                   │
     │                ├──────────────────→                  │                   │
     │                │                  │ 9. Return status │                   │
     │                │◀─────────────────┤                  │                   │
     │ 10. Update UI  │                  │                  │                   │
     │◀───────────────┤                  │                  │                   │
```

### Metrics Collection Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PRODUCTION METRICS FLOW                                   │
│               (Multi-layer Cache for 100K+ users)                            │
└─────────────────────────────────────────────────────────────────────────────┘

User Request: GET /databases/{id}/metrics
        │
        ▼
┌───────────────────┐
│  Rate Limiter     │
│  (Redis)          │  ◀─── Check: user:{user_id}, db:{db_id}
│                   │        Limits: 100/min per user, 1000/min per DB
└────────┬──────────┘
         │ ✅ Allowed
         ▼
┌───────────────────┐
│  L1 Cache         │
│  (In-Memory)      │  ◀─── Python dict, 5s TTL, process-local
│                   │        {cache_key: (data, expires_at)}
└────────┬──────────┘
         │ ❌ Miss
         ▼
┌───────────────────┐
│  L2 Cache         │
│  (Redis)          │  ◀─── Distributed, 15s TTL
│                   │        Fresh key + Stale key (5min backup)
└────────┬──────────┘
         │ ❌ Miss
         ▼
┌───────────────────┐
│ Request Coalescer │  ◀─── Multiple concurrent requests?
│  (asyncio.Future) │        → Single backend fetch, all get same result
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ Circuit Breaker   │  ◀─── Healthy? (< 5 failures)
│  (Per Database)   │        ✅ Closed: Allow request
└────────┬──────────┘        ❌ Open: Return stale or error
         │ ✅ Closed
         ▼
┌───────────────────────────────────────────────┐
│          Metrics Source (Multi-try)           │
│                                               │
│  Try 1: http://localhost:56790/metrics       │  ◀─── Dev (port-forward)
│         ├─ Success? Return & cache            │
│         └─ Fail? Try next                     │
│                                               │
│  Try 2: http://localhost:56791/metrics       │  ◀─── 2nd DB port-forward
│         ├─ Success? Return & cache            │
│         └─ Fail? Try next                     │
│                                               │
│  Try 3: http://{db}-stats.{ns}.svc:56790     │  ◀─── In-cluster (production)
│         ├─ Success? Return & cache            │
│         └─ Fail? Circuit breaker opens        │
└───────────────────┬───────────────────────────┘
                    │
                    ▼
        ┌─────────────────────┐
        │  KubeDB Stats       │
        │  Service            │  ◀─── MongoDB Exporter embedded
        │  Port: 56790        │        Exposes 200+ metrics
        │  /metrics endpoint  │        Prometheus format
        └─────────────────────┘
                    │
                    ▼
        ┌─────────────────────┐
        │  Parse & Format     │
        │  - Extract key      │
        │    metrics          │
        │  - Calculate        │
        │    derived values   │
        │  - Add metadata     │
        └─────────┬───────────┘
                  │
                  ▼
        ┌─────────────────────┐
        │  Store in Caches    │
        │  - L1 (memory)      │
        │  - L2 (Redis)       │
        │  - Stale backup     │
        └─────────┬───────────┘
                  │
                  ▼
        ┌─────────────────────┐
        │  Return to User     │
        │  {                  │
        │    cpu_usage: 1.2,  │
        │    memory: 24MB,    │
        │    connections: 103 │
        │  }                  │
        └─────────────────────┘

Performance:
├─ L1 Hit: < 1ms latency
├─ L2 Hit: < 10ms latency
├─ Source Fetch: 100-300ms
└─ Expected cache hit rate: 85-95%
```

---

## Monitoring Architecture

### Metrics Collection & Observability

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   DATABASE MONITORING ARCHITECTURE                           │
└─────────────────────────────────────────────────────────────────────────────┘

Per Database:

┌──────────────────────────────────────────┐
│  MongoDB Pod (myapp-db-0)                │
│                                           │
│  ┌────────────────────────────────────┐ │
│  │  MongoDB Process                    │ │
│  │  Port: 27017                        │ │
│  └────────────┬───────────────────────┘ │
│               │                          │
│  ┌────────────▼───────────────────────┐ │
│  │  MongoDB Exporter (Sidecar)        │ │  ◀─── Embedded by KubeDB
│  │  - Scrapes MongoDB metrics         │ │
│  │  - Exposes on :56790/metrics       │ │
│  │  - 200+ metrics in Prometheus fmt  │ │
│  └────────────────────────────────────┘ │
└────────────────────┬─────────────────────┘
                     │
                     │ Exposed via
                     ▼
┌────────────────────────────────────────┐
│  Service: myapp-db-stats               │
│  ClusterIP: 10.100.1.51                │
│  Port: 56790                           │
│  Endpoint: /metrics                    │
└────────────────────┬───────────────────┘
                     │
         ┌───────────┴───────────┐
         │                       │
         ▼                       ▼
┌─────────────────┐    ┌──────────────────┐
│  Backend API    │    │  Prometheus      │
│  (Direct Query) │    │  (Data Source)   │
│                 │    │                  │
│  - On demand    │    │  - Scrapes 15s   │
│  - With caching │    │  - Historical    │
│  - Rate limited │    │  - Aggregation   │
└─────────────────┘    └────────┬─────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │  Grafana Dashboard     │
                    │  (Database Monitoring)  │
                    │                        │
                    │  Panels:               │
                    │  ├─ CPU Usage          │
                    │  ├─ Memory Usage       │
                    │  ├─ Disk I/O           │
                    │  ├─ QPS                │
                    │  ├─ Replication Lag    │
                    │  ├─ Index Usage        │
                    │  ├─ Slow Queries       │
                    │  ├─ Node Health (HA)    │
                    │  ├─ Backup Status      │
                    │  └─ Storage Growth     │
                    │                        │
                    │  Filter: db-id         │
                    └───────────────────────┘
```

### Grafana Dashboard

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      GRAFANA DASHBOARD FEATURES                             │
└─────────────────────────────────────────────────────────────────────────────┘

Dashboard: Database Monitoring
├── Template Variable: db-id
│   └── Dropdown to select database by ID
│
├── Panels (13 total):
│   ├── CPU Usage (%) - Per pod CPU utilization
│   ├── Memory Usage (GB) - Memory consumption
│   ├── Disk I/O Read (MB/s) - Read throughput
│   ├── Disk I/O Write (MB/s) - Write throughput
│   ├── Queries Per Second (QPS) - Database query rate
│   ├── Replication Lag (seconds) - HA replication delay
│   ├── Index Usage - Index count and usage
│   ├── Slow Queries (per second) - Performance issues
│   ├── Node Health (HA) - Healthy vs total replicas
│   ├── Backup Job Success/Failure - Backup status
│   ├── Storage Consumption (GB) - Current usage
│   └── Storage Growth Rate (GB/hour) - Capacity planning
│
├── Multi-Engine Support:
│   ├── MongoDB - mongodb_op_counters_total, mongodb_mongod_replset_*
│   ├── PostgreSQL - pg_stat_database_*, pg_replication_*
│   ├── MySQL - mysql_global_status_*, mysql_slave_status_*
│   └── Redis, Elasticsearch - Engine-specific metrics
│
└── Features:
    ├── Auto-refresh (30 seconds)
    ├── Alerting (replication lag threshold)
    ├── Flexible queries (pod name pattern + label-based)
    └── Prometheus data source integration

Location: k8s/monitoring/grafana-dashboard-database.json
Deployment: ConfigMap or Grafana API import
```

---

## Security & Multi-tenancy

### Multi-tenancy Isolation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          MULTI-TENANT ARCHITECTURE                           │
└─────────────────────────────────────────────────────────────────────────────┘

Tenant: Domain "acme-corp" → Project "production"

API Request:
  POST /api/v1/domain/acme-corp/project/production/databases

Backend Processing:
  │
  ├─→ Authentication & Authorization
  │   ├─ Verify user has access to domain "acme-corp"
  │   └─ Verify user has role in project "production"
  │
  ├─→ Resource Naming (Isolation by naming)
  │   ├─ KubeDB Resource: {name}-acme-corp-production
  │   ├─ Namespace: default (shared, but isolated by name)
  │   └─ Labels: {domain: acme-corp, project: production}
  │
  ├─→ Network Isolation
  │   └─ NetworkPolicy: Only allow connections from:
  │       ├─ Backend API (for management)
  │       └─ Tenant's application namespace
  │
  ├─→ Resource Quotas
  │   ├─ CPU: 10 cores per project
  │   ├─ Memory: 20GB per project
  │   ├─ Storage: 100GB per project
  │   └─ Databases: 10 per project
  │
  └─→ Metadata Storage (MongoDB)
      └─ Queries filtered by domain + project
          SELECT * FROM databases
          WHERE domain = 'acme-corp'
            AND project = 'production'

Kubernetes Resources Created:
  ├─ Name: myapp-acme-corp-production
  ├─ Labels:
  │   ├─ domain: acme-corp
  │   ├─ project: production
  │   ├─ app: kubedb
  │   └─ engine: mongodb
  └─ NetworkPolicy:
      ├─ Allow from: backend-api
      └─ Deny: all other namespaces
```

### Security Layers

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SECURITY ARCHITECTURE                                │
└─────────────────────────────────────────────────────────────────────────────┘

Layer 1: Network Security
├─ Ingress Controller: TLS termination (Let's Encrypt)
├─ NetworkPolicies: Namespace isolation
├─ Service Mesh (Optional): mTLS between services
└─ Cloud Security Groups: Firewall rules

Layer 2: Authentication & Authorization
├─ API Authentication: JWT tokens / API keys
├─ RBAC: Role-Based Access Control
│   ├─ Domain Admin: Full access to domain
│   ├─ Project Admin: Full access to project
│   └─ Developer: Read-only access
└─ Service Accounts: K8s RBAC for API ↔ K8s

Layer 3: Data Security
├─ Database Credentials: Kubernetes Secrets
│   ├─ Encrypted at rest
│   └─ Accessed only by authorized pods
├─ Encryption at Rest: PVC encryption (cloud provider)
└─ Encryption in Transit: TLS for all connections

Layer 4: Secrets Management (Optional)
└─ HashiCorp Vault:
    ├─ Dynamic database credentials
    ├─ Secrets rotation
    └─ Audit logging

Layer 5: Monitoring & Auditing
├─ Access Logs: All API requests logged
├─ Audit Trail: Who created/deleted what
├─ Security Scanning: Container image scanning
└─ Compliance: GDPR, SOC2, HIPAA
```

---

## Scaling & High Availability

### Horizontal Scaling

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SCALING ARCHITECTURE                                 │
└─────────────────────────────────────────────────────────────────────────────┘

API Tier (Stateless):
  HorizontalPodAutoscaler:
    minReplicas: 3
    maxReplicas: 20
    targetCPU: 70%

  Load Balancing:
    ├─ Ingress: Round-robin
    └─ Service: kube-proxy

Cache Tier (Redis):
  Redis Cluster Mode:
    ├─ 3 master nodes
    ├─ 3 replica nodes
    └─ Automatic sharding

Metadata Tier (MongoDB):
  ReplicaSet:
    ├─ 1 primary
    ├─ 2 secondaries
    └─ Automatic failover

Managed Databases:
  Per-Database Scaling:
    ├─ Vertical: Change instance size (CPU/Memory)
    ├─ Horizontal: Add replicas
    └─ Storage: Expand PVCs
```

### Disaster Recovery

```
Backup Strategy:
├─ Metadata (MongoDB):
│   ├─ Continuous backup to S3
│   └─ Point-in-time recovery
│
├─ Managed Databases:
│   ├─ KubeDB Stash (backup operator)
│   ├─ Schedule: Daily full + hourly incremental
│   ├─ Retention: 30 days
│   └─ Storage: S3-compatible

Recovery Scenarios:
├─ Pod Failure: Automatic restart (K8s)
├─ Node Failure: Automatic rescheduling
├─ Zone Failure: Multi-AZ deployment
└─ Region Failure: Multi-region + manual failover
```

---

## Technology Stack Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TECHNOLOGY STACK                                     │
└─────────────────────────────────────────────────────────────────────────────┘

Frontend:
├─ Framework: React 18
├─ Build: Vite
├─ Styling: CSS
└─ HTTP Client: Fetch API

Backend:
├─ Framework: FastAPI (Python 3.14)
├─ ASGI Server: Uvicorn
├─ ODM: Beanie (MongoDB)
├─ K8s Client: kubernetes-asyncio
├─ Caching: Redis (aioredis)
├─ Queue: Redis (operation queue)
├─ Workers: Operation Worker (3x), Reconciliation Worker, Status Sync
├─ Leader Election: Redis-based (for reconciliation)
└─ Services:
    ├─ DatabaseService - Business logic & validation
    ├─ KubeDBService - Multi-provider K8s integration
    ├─ ProviderService - Provider management
    ├─ AuditService - Audit logging
    ├─ MetricsService - Metrics collection
    ├─ MetricsCacheService - Multi-layer caching
    ├─ PrometheusService - PromQL queries
    └─ TenantService - Tenant & quota management

Infrastructure:
├─ Orchestration: Kubernetes 1.28+
├─ Database Operator: KubeDB
├─ Ingress: nginx-ingress
├─ Monitoring: Prometheus + Grafana (optional)
└─ Storage: CSI drivers (cloud provider)

Data Stores:
├─ Metadata: MongoDB 8.0
├─ Cache: Redis 7.x
└─ Managed: MongoDB, PostgreSQL, MySQL, MariaDB, Redis, Elasticsearch

Supporting Services:
├─ Secrets: Kubernetes Secrets / Vault (optional)
├─ Backup: KubeDB Stash / Velero
├─ CI/CD: GitHub Actions / GitLab CI
└─ Container Registry: Docker Hub / ECR / GCR
```

---

## Deployment Topology

### Production Deployment (Multi-AZ)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           AWS/GCP/AZURE REGION                               │
└─────────────────────────────────────────────────────────────────────────────┘

Availability Zone 1          Availability Zone 2          Availability Zone 3
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│  K8s Node 1      │        │  K8s Node 3      │        │  K8s Node 5      │
│  ├─ API Pod 1    │        │  ├─ API Pod 3    │        │  ├─ API Pod 5    │
│  ├─ Frontend 1   │        │  ├─ Frontend 2   │        │  ├─ Frontend 3   │
│  └─ Redis 1      │        │  └─ Redis 3      │        │  └─ Redis 5      │
└──────────────────┘        └──────────────────┘        └──────────────────┘

┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│  K8s Node 2      │        │  K8s Node 4      │        │  K8s Node 6      │
│  ├─ MongoDB 1    │        │  ├─ MongoDB 2    │        │  ├─ MongoDB 3    │
│  ├─ Managed DB 1 │        │  ├─ Managed DB 2 │        │  ├─ Managed DB 3 │
│  └─ KubeDB Op    │        │  └─ Prometheus   │        │  └─ Grafana      │
└──────────────────┘        └──────────────────┘        └──────────────────┘

├─ Load Balancer: Distributes across all API pods
├─ Anti-affinity: Spread pods across zones
└─ Persistent Storage: EBS/Persistent Disk (replicated)
```

---

## Summary

**Key Features:**
- ✅ Multi-tenant isolation (domain/project)
- ✅ Multi-provider architecture (manage multiple K8s clusters)
- ✅ Single pod architecture (all workers in one process)
- ✅ Asynchronous operation processing (Redis queue)
- ✅ Comprehensive audit logging (all operations tracked)
- ✅ Production-grade monitoring (Grafana dashboard)
- ✅ Multi-layer caching (Memory → Redis)
- ✅ High availability (multi-AZ)
- ✅ Auto-scaling (HPA)
- ✅ Network isolation (NetworkPolicies)
- ✅ Secure credentials (K8s Secrets)
- ✅ Disaster recovery (backups)
- ✅ Leader election (reconciliation worker)
- ✅ Status synchronization (all pods)

**Supported Databases:**
- MongoDB, PostgreSQL, MySQL, MariaDB, Redis, Elasticsearch

**Architecture Highlights:**
- Single pod runs: API server, 3 operation workers, reconciliation worker, status sync
- Multi-provider: Each database linked to a provider (K8s cluster)
- Operation queue: Redis-based queue for async operations
- Audit logging: All DBaaS operations logged with user context
- Grafana dashboard: 13 panels for comprehensive database monitoring

**Deployment Modes:**
- Development (local K8s with port-forwards)
- Production (in-cluster, multi-AZ, HA, multi-provider)
