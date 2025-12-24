# High-Level Design (HLD)
# KubeDB DBaaS Platform

**Version:** 1.0.0
**Date:** December 23, 2025
**Author:** KubeDB DBaaS Team

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Components](#3-components)
4. [Data Flow](#4-data-flow)
5. [Technology Stack](#5-technology-stack)
6. [Deployment Architecture](#6-deployment-architecture)
7. [Security](#7-security)
8. [Scalability](#8-scalability)
9. [High Availability](#9-high-availability)
10. [Monitoring & Observability](#10-monitoring--observability)

---

## 1. System Overview

### 1.1 Purpose
KubeDB DBaaS (Database as a Service) is a production-grade platform that provides on-demand database provisioning, management, and operations on Kubernetes clusters using KubeDB operators.

### 1.2 Key Features
- **Multi-Database Support**: PostgreSQL, MySQL, MongoDB, MariaDB, Redis, Elasticsearch
- **Automated Operations**: Provisioning, scaling, backup/restore, version upgrades
- **Multi-Provider**: Support for multiple Kubernetes clusters across different cloud providers
- **Self-Healing**: Automated reconciliation and drift detection
- **Backup & Recovery**: Automated backups to S3-compatible storage
- **Real-time Monitoring**: Metrics collection and status synchronization
- **Multi-Tenancy**: Domain and project-based isolation

### 1.3 Objectives
- Simplify database management on Kubernetes
- Reduce operational overhead
- Provide enterprise-grade reliability
- Enable self-service database provisioning
- Support multiple environments and regions

---

## 2. Architecture

### 2.1 System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         End Users                                │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│                      Load Balancer                               │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│                    Frontend (React + Vite)                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ - Dashboard  - Database Management  - Monitoring         │  │
│  │ - Operations - Backup/Restore      - User Interface      │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────┬────────────────────────────────────────────────┘
                 │ REST API
┌────────────────▼────────────────────────────────────────────────┐
│                  Backend API (FastAPI)                           │
│  ┌──────────────┬─────────────┬──────────────┬─────────────┐   │
│  │ API Layer    │ Service     │ Repository   │ Models      │   │
│  │              │ Layer       │ Layer        │             │   │
│  └──────────────┴─────────────┴──────────────┴─────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Background Workers                           │  │
│  │  - Reconciliation Worker (Drift Detection)               │  │
│  │  - Status Sync Worker (Real-time Status)                 │  │
│  │  - Operation Workers (Parallel Processing)               │  │
│  └──────────────────────────────────────────────────────────┘  │
└───┬────────────┬────────────────────────────────────────────────┘
    │            │
    ├────────────▼───────────────┐     ┌────────────────────────┐
    │      MongoDB               │     │       Redis            │
    │  (Metadata Storage)        │     │  (Cache + Queue)       │
    │                            │     │                        │
    │  - Databases metadata      │     │  - Operation queue     │
    │  - Operations history      │     │  - Session cache       │
    │  - Providers config        │     │  - Status cache        │
    │  - Audit logs              │     │  - Metrics cache       │
    └────────────────────────────┘     └────────────────────────┘
                 │
    ┌────────────▼──────────────────────────────────────────────┐
    │           Kubernetes API (Multiple Clusters)              │
    ├───────────────┬────────────────┬──────────────────────────┤
    │  Cluster 1    │   Cluster 2    │     Cluster N            │
    │               │                │                          │
    │  ┌─────────┐  │  ┌─────────┐  │    ┌─────────┐          │
    │  │ KubeDB  │  │  │ KubeDB  │  │    │ KubeDB  │          │
    │  │Operators│  │  │Operators│  │    │Operators│          │
    │  └────┬────┘  │  └────┬────┘  │    └────┬────┘          │
    │       │       │       │        │         │                │
    │  ┌────▼────┐  │  ┌────▼────┐  │    ┌────▼────┐          │
    │  │Database │  │  │Database │  │    │Database │          │
    │  │ Pods    │  │  │ Pods    │  │    │ Pods    │          │
    │  └─────────┘  │  └─────────┘  │    └─────────┘          │
    └───────────────┴────────────────┴──────────────────────────┘
                 │
    ┌────────────▼──────────────────────────────────────────────┐
    │              S3-Compatible Storage                        │
    │                  (Backup Storage)                         │
    └───────────────────────────────────────────────────────────┘
```

### 2.2 Architectural Patterns

#### 2.2.1 Layered Architecture
- **Presentation Layer**: React frontend
- **API Layer**: FastAPI REST endpoints
- **Business Logic Layer**: Service classes
- **Data Access Layer**: Repository pattern
- **Data Layer**: MongoDB + Redis

#### 2.2.2 Microservices-Ready Design
- Single deployment with modular services
- Independent scaling capability
- Separation of concerns
- Event-driven communication via Redis queues

#### 2.2.3 Multi-Tenancy
- **Domain Level**: Top-level isolation
- **Project Level**: Namespace within domains
- **Resource Isolation**: Kubernetes namespaces per project

---

## 3. Components

### 3.1 Frontend Application

**Technology**: React + Vite + TailwindCSS

**Responsibilities**:
- User interface for database management
- Dashboard and monitoring views
- Database lifecycle operations (CRUD)
- Backup/restore management
- Real-time status updates

**Key Features**:
- Responsive design
- Real-time metrics visualization
- Operation status tracking
- Multi-provider support

### 3.2 Backend API Server

**Technology**: FastAPI + Python 3.11

**Core Modules**:

#### 3.2.1 API Layer (`app/api/v1/`)
- `databases.py`: Database CRUD operations
- `providers.py`: Multi-cluster provider management
- `operations.py`: Operation queue and execution
- `versions.py`: Database version management
- `health.py`: Health check endpoints
- `auth.py`: Authentication endpoints

#### 3.2.2 Service Layer (`app/services/`)
- `database_service.py`: Database lifecycle management
- `kubedb_service.py`: KubeDB operator interactions
- `provider_service.py`: Kubernetes cluster management
- `status_sync_service.py`: Real-time status synchronization
- `metrics_service.py`: Metrics collection and aggregation
- `operation_queue.py`: Async operation management

#### 3.2.3 Repository Layer (`app/repositories/`)
- Abstract database operations
- MongoDB connection pooling
- Query optimization

#### 3.2.4 Models Layer (`app/models/`)
- Beanie ODM models for MongoDB
- Pydantic schemas for validation
- Enum definitions for constants

### 3.3 Background Workers

#### 3.3.1 Reconciliation Worker
**Purpose**: Drift detection and automatic remediation

**Responsibilities**:
- Compare desired state vs actual state
- Detect configuration drifts
- Create OpsRequests for corrections
- Handle scaling, version updates

**Frequency**: Configurable (default: 5 minutes)

**Algorithm**:
```
FOR each database in RUNNING/UPDATING state:
  1. Fetch current state from Kubernetes
  2. Compare with desired state in MongoDB
  3. IF drift detected:
     - Check for active operations
     - IF no active operations:
       - Create OpsRequest (Scale/Update/Restart)
       - Update database status to UPDATING
  4. Monitor OpsRequest completion
```

#### 3.3.2 Status Sync Worker
**Purpose**: Real-time status synchronization

**Responsibilities**:
- Sync database status from Kubernetes
- Update health metrics
- Track pod readiness
- Update connection endpoints

**Frequency**: 30 seconds (configurable)

**Data Synced**:
- Database phase (Provisioning, Ready, Failed, etc.)
- Ready replicas count
- Resource metrics
- Health status
- Endpoint and port information

#### 3.3.3 Operation Workers
**Purpose**: Parallel operation execution

**Responsibilities**:
- Process queued operations
- Execute database operations
- Update operation status
- Handle failures and retries

**Concurrency**: 3 workers (configurable)

### 3.4 Data Stores

#### 3.4.1 MongoDB
**Purpose**: Persistent metadata storage

**Collections**:
- `databases`: Database definitions and state
- `providers`: Kubernetes cluster configurations
- `operations`: Operation history and status
- `audit_logs`: Audit trail
- `users`: User management

**Features**:
- Replica set for HA
- Connection pooling
- Index optimization
- TTL for operation logs

#### 3.4.2 Redis
**Purpose**: Cache and message queue

**Usage**:
- Operation queue (FIFO)
- Session management
- Metrics cache
- Status cache
- Rate limiting

**Features**:
- Persistence (AOF + RDB)
- Connection pooling
- Pub/Sub for events

### 3.5 External Dependencies

#### 3.5.1 KubeDB Operators
- Database provisioning
- Backup management
- Version upgrades
- Monitoring

#### 3.5.2 Kubernetes Clusters
- Multiple clusters support
- Dynamic kubeconfig management
- RBAC integration

#### 3.5.3 S3-Compatible Storage
- Backup storage
- Point-in-time recovery
- Automated retention policies

---

## 4. Data Flow

### 4.1 Database Creation Flow

```
1. User submits create request via UI/API
   ↓
2. API validates request (schema, quotas, permissions)
   ↓
3. DatabaseService creates database record (status: PENDING)
   ↓
4. Provider selector chooses optimal cluster
   ↓
5. KubeDBService creates Kubernetes resources:
   - Database CR (MongoDB/PostgreSQL/etc.)
   - Secret for credentials
   - Service for connectivity
   ↓
6. Status changes to PROVISIONING
   ↓
7. Status Sync Worker monitors creation:
   - Polls Kubernetes API
   - Updates status in MongoDB
   ↓
8. When database is ready:
   - Status → RUNNING
   - Endpoint populated
   - Credentials available
   ↓
9. (Optional) Initial backup created
   ↓
10. User receives database connection details
```

### 4.2 Scaling Flow (Vertical/Horizontal)

```
1. User requests scaling via UI/API
   ↓
2. DatabaseService updates desired state
   ↓
3. Database status → SCALING
   ↓
4. Reconciliation Worker detects drift
   ↓
5. Creates OpsRequest:
   - VerticalScaling (CPU/Memory change)
   - HorizontalScaling (Replica change)
   ↓
6. KubeDB Operator processes OpsRequest
   ↓
7. Status Sync monitors OpsRequest:
   - Phase: Pending → Progressing → Successful
   ↓
8. When OpsRequest succeeds:
   - Database status → UPDATING (during pod restarts)
   - Wait for all replicas ready
   - Database status → RUNNING
   ↓
9. Reconciliation Worker verifies final state
```

### 4.3 Backup Flow

```
1. Trigger: Manual or Scheduled
   ↓
2. DatabaseService.trigger_backup()
   ↓
3. KubeDBService creates Backup Job:
   - Mount database credentials
   - Run native dump tool (pg_dump/mongodump)
   - Compress backup
   - Upload to S3
   ↓
4. Job monitors:
   - Success: Backup record created
   - Failure: Retry logic (max 3 times)
   ↓
5. Cleanup: Job auto-deleted after TTL
```

### 4.4 Status Sync Flow

```
Every 30 seconds:
1. Status Sync Worker fetches all active databases
   ↓
2. For each database:
   - Query Kubernetes API for current state
   - Get pod status, phase, replicas
   - Check for active OpsRequests
   ↓
3. Map KubeDB phase to DBaaS status:
   - Provisioning → PROVISIONING
   - Ready/Running → RUNNING
   - Failed → FAILED (with safeguards)
   ↓
4. Update MongoDB with latest state
   ↓
5. Cache status in Redis for fast API responses
```

---

## 5. Technology Stack

### 5.1 Backend
- **Framework**: FastAPI 0.104+
- **Language**: Python 3.11
- **ODM**: Beanie (async MongoDB ODM)
- **Validation**: Pydantic v2
- **Async**: asyncio, uvloop
- **HTTP Client**: httpx
- **Kubernetes**: kubernetes-asyncio

### 5.2 Frontend
- **Framework**: React 18
- **Build Tool**: Vite 7
- **Styling**: TailwindCSS
- **State Management**: React Context
- **HTTP Client**: Axios
- **Routing**: React Router

### 5.3 Data Layer
- **Primary DB**: MongoDB 7.0 (Replica Set)
- **Cache/Queue**: Redis 7.0
- **Storage**: S3-compatible (MinIO/AWS S3)

### 5.4 Infrastructure
- **Container**: Docker
- **Orchestration**: Kubernetes 1.28+
- **Operators**: KubeDB 0.40+
- **Ingress**: Nginx Ingress Controller
- **Service Mesh**: (Optional) Istio

### 5.5 Observability
- **Metrics**: Prometheus
- **Logging**: Structured JSON logs
- **Tracing**: Jaeger (optional)
- **APM**: Sentry (production)

---

## 6. Deployment Architecture

### 6.1 Kubernetes Deployment

```yaml
Components:
  - Backend API: 3 replicas (HPA: 3-10)
  - Frontend: 2 replicas (HPA: 2-5)
  - MongoDB: 3-node replica set
  - Redis: 1 master + 2 replicas
  - Ingress: Nginx LoadBalancer

Resources:
  Backend Pod:
    requests: 500m CPU, 512Mi memory
    limits: 1000m CPU, 1Gi memory

  Frontend Pod:
    requests: 100m CPU, 128Mi memory
    limits: 200m CPU, 256Mi memory
```

### 6.2 Single-Pod Architecture

All components run in one process:
- API Server (uvicorn)
- Reconciliation Worker
- Status Sync Worker
- 3x Operation Workers

**Benefits**:
- Simplified deployment
- Reduced resource overhead
- Easier local development
- No inter-service networking overhead

**Leader Election**: Only one pod runs background workers

### 6.3 Multi-Cluster Setup

```
Control Plane Cluster:
  - DBaaS Backend API
  - MongoDB + Redis
  - Frontend UI

Managed Clusters (1..N):
  - KubeDB Operators
  - Database Instances
  - Monitoring Agents
```

---

## 7. Security

### 7.1 Authentication & Authorization
- JWT-based authentication
- Role-based access control (RBAC)
- API key support for automation

### 7.2 Network Security
- TLS/SSL encryption in transit
- Private VPC networking
- Network policies for pod isolation

### 7.3 Secret Management
- Kubernetes Secrets for credentials
- Vault integration (optional)
- Automated credential rotation

### 7.4 Database Credentials
- Auto-generated strong passwords
- Encrypted storage in MongoDB
- Scoped access per database

### 7.5 Audit Logging
- All API calls logged
- Database operation history
- User action tracking

---

## 8. Scalability

### 8.1 Horizontal Scaling
- **API**: HPA based on CPU/memory
- **Workers**: Increase replica count
- **Databases**: Native replication

### 8.2 Vertical Scaling
- Dynamic resource adjustment
- No downtime for most engines
- Automated rollout

### 8.3 Multi-Region Support
- Multiple Kubernetes clusters
- Provider-based routing
- Geographic distribution

### 8.4 Performance Optimization
- **Caching**: Redis for frequently accessed data
- **Connection Pooling**: MongoDB/Redis connections
- **Async I/O**: Non-blocking operations
- **Background Processing**: Queue-based operations

---

## 9. High Availability

### 9.1 Application Layer
- Multiple replicas (3+ in production)
- Rolling updates (zero downtime)
- Health checks (liveness, readiness)

### 9.2 Data Layer
- MongoDB replica set (3 nodes)
- Redis sentinel/cluster mode
- Automated failover

### 9.3 Database Layer
- Native database replication
- Automated backup and recovery
- Point-in-time recovery (PITR)

### 9.4 Disaster Recovery
- Regular backups to S3
- Cross-region replication
- Documented recovery procedures
- RTO: < 4 hours, RPO: < 1 hour

---

## 10. Monitoring & Observability

### 10.1 Metrics
- **Application Metrics**: Request rate, latency, errors
- **Business Metrics**: Database count, operations/day
- **Infrastructure Metrics**: CPU, memory, disk, network
- **Database Metrics**: Connections, queries, replication lag

### 10.2 Logging
- Structured JSON logging
- Correlation IDs for tracing
- Log aggregation (ELK/Loki)
- Log retention policies

### 10.3 Alerting
- Prometheus AlertManager
- Critical alerts: Database down, backup failed
- Warning alerts: High resource usage, slow queries
- Notification channels: Email, Slack, PagerDuty

### 10.4 Dashboards
- Grafana dashboards for visualization
- Real-time status dashboard
- Historical trends and analytics
- Custom metrics per tenant

---

## Appendix

### A. Supported Database Engines

| Engine         | Versions Supported | Features                          |
|----------------|-------------------|-----------------------------------|
| PostgreSQL     | 12, 13, 14, 15, 16| Streaming replication, PITR       |
| MySQL          | 5.7, 8.0, 8.4     | Group replication, binlog backup  |
| MongoDB        | 4.4, 5.0, 6.0, 7.0| Replica set, sharding             |
| MariaDB        | 10.5, 10.6, 11.0  | Galera cluster, replication       |
| Redis          | 6.2, 7.0, 7.2     | Cluster mode, sentinel            |
| Elasticsearch  | 7.x, 8.x          | Multi-node cluster                |

### B. System Limits

| Resource                    | Default Limit | Max Limit  |
|-----------------------------|---------------|------------|
| Databases per Project       | 10            | 100        |
| Max Replicas per Database   | 9             | 9          |
| Max Storage per Database    | 1TB           | 10TB       |
| Concurrent Operations       | 3             | 10         |
| Backup Retention            | 30 days       | 365 days   |

### C. API Rate Limits

| Endpoint Type    | Rate Limit           |
|------------------|----------------------|
| Read Operations  | 100 req/min          |
| Write Operations | 60 req/min           |
| Bulk Operations  | 10 req/min           |

---

**Document Status**: Draft v1.0
**Last Updated**: December 23, 2025
**Next Review**: Q1 2026
