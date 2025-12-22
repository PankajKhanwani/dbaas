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
├── Backend API (FastAPI)     - REST API & Business Logic
├── MongoDB                   - Metadata Storage
├── Redis                     - Caching & Rate Limiting
├── KubeDB Operator           - Database Provisioning
├── Kubernetes Cluster        - Orchestration Platform
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

### Backend API (FastAPI) - Detailed View

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BACKEND API POD                                      │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI Application (Uvicorn)                                       │   │
│  │  Port: 8000                                                           │   │
│  └──────────────────────────┬────────────────────────────────────────────┘   │
│                             │                                                │
│  ┌──────────────────────────┴────────────────────────────────────────────┐ │
│  │                      API Layer                                         │ │
│  │  /api/v1/                                                             │ │
│  │  ├─ /databases     - CRUD operations                                  │ │
│  │  ├─ /credentials   - Get DB credentials                              │ │
│  │  ├─ /metrics       - Database metrics                                 │ │
│  │  ├─ /versions      - Available DB versions                           │ │
│  │  └─ /health        - Health checks                                    │ │
│  └──────────────────────────┬────────────────────────────────────────────┘ │
│                             │                                                │
│  ┌──────────────────────────┴────────────────────────────────────────────┐ │
│  │                   Service Layer                                        │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │ DatabaseService     │  │  KubeDBService        │                   │ │
│  │  │ - Business Logic    │  │  - K8s API Client     │                   │ │
│  │  │ - Validation        │  │  - CRD Management     │                   │ │
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
│  │                   Data Layer                                           │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────┐                   │ │
│  │  │  MongoDB (Beanie)   │  │  Redis Client         │                   │ │
│  │  │  - Database Models  │  │  - Cache Operations   │                   │ │
│  │  │  - ODM Queries      │  │  - Rate Limit Keys    │                   │ │
│  │  └─────────────────────┘  └──────────────────────┘                   │ │
│  │                                                                         │ │
│  │  ┌─────────────────────┐                                              │ │
│  │  │  Kubernetes Client  │                                              │ │
│  │  │  - Custom Resources │                                              │ │
│  │  │  - Services, Pods   │                                              │ │
│  │  │  - Secrets          │                                              │ │
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
     │                │                  │ 3. Save record   │                   │
     │                │                  ├──────────────────→                   │
     │                │                  │                  │ 4. Create CRD     │
     │                │                  │                  ├───────────────────→
     │                │                  │                  │                   │
     │                │                  │                  │  ┌──────────────┐ │
     │                │                  │                  │  │ KubeDB       │ │
     │                │                  │                  │  │ Operator     │ │
     │                │                  │                  │  │ (watches)    │ │
     │                │                  │                  │  └──────┬───────┘ │
     │                │                  │                  │         │         │
     │                │                  │                  │  5. Provisions    │
     │                │                  │                  │  - StatefulSet    │
     │                │                  │                  │  - Services       │
     │                │                  │                  │  - PVCs           │
     │                │                  │                  │  - Secrets        │
     │                │                  │                  │         │         │
     │                │ 6. Return 201    │                  │         ▼         │
     │                │ Created          │                  │  ┌──────────────┐ │
     │                │◀─────────────────┤                  │  │  Database    │ │
     │ 7. Show success│                  │                  │  │  Running     │ │
     │◀───────────────┤                  │                  │  └──────────────┘ │
     │                │                  │                  │                   │
     │                │ 8. Poll /status  │                  │                   │
     │                ├──────────────────→                  │                   │
     │                │                  │ 9. Check K8s     │                   │
     │                │                  ├──────────────────────────────────────→
     │                │                  │ status           │                   │
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

### Metrics Collection (Current Implementation)

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
│  (Direct Query) │    │  (Optional)      │
│                 │    │                  │
│  - On demand    │    │  - Scrapes 15s   │
│  - With caching │    │  - Historical    │
│  - Rate limited │    │  - Aggregation   │
└─────────────────┘    └──────────────────┘
```

### Optional: Prometheus Integration

```
┌─────────────────────────────────────────────────────────────────────────────┐
│             ENHANCED MONITORING WITH PROMETHEUS (Optional)                   │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  All Database Stats Services            │
│  ├─ myapp-db-stats:56790                │
│  ├─ analytics-db-stats:56790            │
│  ├─ cache-db-stats:56790                │
│  └─ ... (100s of databases)             │
└────────────┬────────────────────────────┘
             │ ServiceMonitor
             │ (Auto-discovery)
             ▼
┌─────────────────────────────────────────┐
│  Prometheus Server                      │
│  - Scrapes every 15s                    │
│  - Stores time-series (15 days)         │
│  - Retention: 15d                       │
│  - Query: PromQL                        │
└────────────┬────────────────────────────┘
             │
             ├─────────────┬─────────────┬──────────────┐
             ▼             ▼             ▼              ▼
    ┌─────────────┐ ┌───────────┐ ┌──────────┐ ┌────────────┐
    │  Backend    │ │  Grafana  │ │ Alert   │ │  Thanos    │
    │  API        │ │  (Viz)    │ │ Manager │ │  (Storage) │
    │             │ │           │ │         │ │            │
    │  Production │ │  Explore  │ │  Slack  │ │  Long-term │
    │  Caching    │ │  Dashbds  │ │  PagerD │ │  Retention │
    └─────────────┘ └───────────┘ └─────────┘ └────────────┘
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
└─ Caching: Redis (aioredis)

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
- ✅ Production-grade monitoring (100K+ users)
- ✅ Multi-layer caching (Memory → Redis)
- ✅ High availability (multi-AZ)
- ✅ Auto-scaling (HPA)
- ✅ Network isolation (NetworkPolicies)
- ✅ Secure credentials (K8s Secrets)
- ✅ Disaster recovery (backups)

**Supported Databases:**
- MongoDB, PostgreSQL, MySQL, MariaDB, Redis, Elasticsearch

**Deployment Modes:**
- Development (local K8s with port-forwards)
- Production (in-cluster, multi-AZ, HA)
