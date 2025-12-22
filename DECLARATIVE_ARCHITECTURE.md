# Declarative Architecture - Kubernetes Style

## Overview

The system now follows a **declarative, Kubernetes-style architecture** where:
- **User declares desired state** via API
- **API does pre-checks only**, saves desired state to database
- **Reconciler detects drift** (desired != current)
- **Reconciler creates operations** to fix drift
- **Operation workers execute** the changes

This is similar to how Kubernetes itself works: You declare "I want 3 replicas", Kubernetes ensures you have 3 replicas.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    User / Client                             │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ PATCH /databases/db-123
                       │ {"size": "db.t3.large"}
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                     FastAPI API                               │
│  • Pre-checks only (validation, auth, business rules)        │
│  • Updates database.size = db.t3.large (DESIRED STATE)       │
│  • Returns immediately (< 50ms)                              │
│  • Does NOT create operations                                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ Saves to MongoDB
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                 MongoDB Database Record                       │
│  Desired State:                                              │
│    size: db.t3.large       ← What user wants                │
│    replicas: 3                                                │
│    storage_gb: 50                                             │
│                                                               │
│  Current State:                                              │
│    current_size: db.t3.medium  ← What's actually running    │
│    current_replicas: 3                                        │
│    current_storage_gb: 50                                     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ Reconciler polls every 5 min
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                 Reconciliation Worker                         │
│  Every 5 minutes:                                            │
│  1. Fetch KubeDB CR from Kubernetes                          │
│  2. Update current_* fields (sync actual state)              │
│  3. Compare: desired vs current                              │
│  4. If drift → Create Operation → Enqueue to Redis           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ Enqueues operation
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                    Redis Queue                                │
│  Operations waiting to be processed                          │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ Workers dequeue
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                 Operation Worker                              │
│  1. Dequeue operation from Redis                             │
│  2. Create KubeDB OpsRequest                                 │
│  3. Monitor until complete                                   │
│  4. Update current_* = desired_* (sync state)                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ Creates OpsRequest
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   KubeDB Operator                             │
│  Executes the actual scaling/update                          │
└──────────────────────────────────────────────────────────────┘
```

---

## Key Concepts

### 1. **Desired State vs Current State**

**Database Model Fields:**

```python
class Database(Document):
    # DESIRED STATE (what user wants)
    size: DatabaseSize              # db.t3.large
    replicas: int                   # 3
    storage_gb: int                 # 50

    # CURRENT STATE (what's actually running in KubeDB)
    current_size: Optional[DatabaseSize]      # db.t3.medium
    current_replicas: Optional[int]           # 3
    current_storage_gb: Optional[int]         # 30
```

**How it works:**
- **API updates desired state** (size, replicas, storage_gb)
- **Reconciler syncs current state** from KubeDB CR
- **Reconciler compares** desired vs current
- **If different → drift detected** → create operation

---

### 2. **API Responsibilities**

**What API DOES:**
✅ Authentication & authorization
✅ Input validation (e.g., replicas >= 1)
✅ Business rule checks (e.g., can't decrease storage)
✅ Update desired state in database
✅ Return immediately (< 50ms)

**What API DOES NOT:**
❌ Create operations
❌ Enqueue to Redis
❌ Call KubeDB directly
❌ Block waiting for completion

**Example:**
```python
# OLD (imperative):
@router.patch("/databases/{id}")
async def update_database(...):
    operation = Operation(...)  # Create operation
    await operation_queue.enqueue(...)  # Enqueue
    return {"operation_id": operation.id}  # Return operation ID

# NEW (declarative):
@router.patch("/databases/{id}")
async def update_database(...):
    db.size = update_data.size  # Update desired state
    await db.save()  # Save to DB
    return db  # Return immediately
```

---

### 3. **Reconciler Responsibilities**

**What Reconciler DOES:**
✅ Runs every 5 minutes (configurable)
✅ Fetches KubeDB CR from Kubernetes
✅ Updates current_* fields (sync actual state)
✅ Compares desired vs current
✅ Creates operations when drift detected
✅ Enqueues operations to Redis

**Reconciliation Loop:**
```python
async def reconcile_all_databases():
    for database in databases:
        # 1. Fetch current state from KubeDB
        cr = await fetch_kubedb_cr(database)
        database.current_size = get_size_from_cr(cr)
        database.current_replicas = cr.spec.replicas
        database.current_storage_gb = parse_storage(cr.spec.storage)
        await database.save()

        # 2. Compare desired vs current
        if database.size != database.current_size:
            # Drift detected!
            operation = Operation(
                type=OperationType.SCALE_VERTICAL,
                desired_state={"size": database.size}
            )
            await operation_queue.enqueue(operation.id)
```

---

### 4. **Operation Worker Responsibilities**

**What Worker DOES:**
✅ Dequeues operations from Redis
✅ Creates KubeDB OpsRequests
✅ Monitors OpsRequest progress
✅ Updates current_* after completion
✅ Records metrics

**After Operation Completes:**
```python
# Operation completes successfully
operation.status = OperationStatus.COMPLETED
await operation.save()

# Update current state to match desired state
db.current_size = DatabaseSize(operation.desired_state["size"])
db.current_replicas = operation.desired_state.get("replicas")
db.current_storage_gb = operation.desired_state.get("storage_gb")
await db.save()

# Now: desired == current (no drift)
```

---

## Flow Examples

### Example 1: User Scales Database

```
1. User: PATCH /databases/db-123 {"size": "db.t3.large"}

2. API:
   - Validates: size is valid enum value ✓
   - Checks auth: user has access ✓
   - Updates: db.size = db.t3.large (desired state)
   - Saves to MongoDB
   - Returns: {"id": "db-123", "size": "db.t3.large", ...} in < 50ms

   (API does NOT create operation)

3. Database state:
   - desired: size = db.t3.large
   - current: size = db.t3.medium
   - DRIFT EXISTS

4. Reconciler (next cycle, ~5 min later):
   - Fetches KubeDB CR
   - current_size = db.t3.medium (from CR)
   - Compares: desired (db.t3.large) != current (db.t3.medium)
   - Detects drift!
   - Creates Operation(type=SCALE_VERTICAL, desired_state={size: db.t3.large})
   - Enqueues to Redis

5. Operation Worker:
   - Dequeues operation
   - Creates MongoDBOpsRequest
   - Monitors until Successful
   - Updates: db.current_size = db.t3.large
   - Now: desired == current ✓

6. Reconciler (next cycle):
   - desired (db.t3.large) == current (db.t3.large)
   - No drift ✓
   - No action needed
```

**Timeline:**
- 0:00 - User request (API returns in 50ms)
- 5:00 - Reconciler detects drift, creates operation
- 5:01 - Operation worker starts processing
- 5:08 - OpsRequest completes, current_size updated
- 10:00 - Reconciler verifies no drift

---

### Example 2: Manual Change to KubeDB

```
1. Admin manually changes KubeDB:
   kubectl patch mongodb my-db --patch '{"spec":{"replicas":5}}'

   (Changes replicas from 3 to 5 directly in Kubernetes)

2. Database state:
   - desired: replicas = 3 (in MongoDB)
   - current: replicas = 3 (not yet synced)

3. Reconciler (next cycle):
   - Fetches KubeDB CR: spec.replicas = 5
   - Updates: db.current_replicas = 5 (sync actual state)
   - Compares: desired (3) != current (5)
   - Detects drift!
   - Creates Operation(type=SCALE_HORIZONTAL, desired_state={replicas: 3})
   - Enqueues to Redis

4. Operation Worker:
   - Creates HorizontalScaling OpsRequest (back to 3 replicas)
   - Monitors until complete
   - Updates: db.current_replicas = 3
   - Now: desired == current ✓

5. Result: Database automatically returns to 3 replicas
```

**This prevents configuration drift!** Manual changes are automatically reverted to desired state.

---

### Example 3: Fast Multiple Updates

```
1. User: PATCH /databases/db-123 {"size": "db.t3.medium"}
   API: Updates db.size = db.t3.medium, returns immediately

2. User (10 seconds later): PATCH /databases/db-123 {"size": "db.t3.large"}
   API: Updates db.size = db.t3.large, returns immediately

   (User changed their mind)

3. Reconciler (5 min later):
   - current_size: db.t3.small (actual state)
   - desired_size: db.t3.large (latest user request)
   - Creates operation for db.t3.large (NOT db.t3.medium)

4. Result: Only ONE operation created, for the final desired state ✓
```

**Benefits:**
- Multiple rapid API calls don't create multiple operations
- Only the final desired state matters
- Reconciler naturally batches changes

---

## Benefits of Declarative Architecture

### 1. **Eventually Consistent**
- System eventually reaches desired state
- Even if operations fail, reconciler retries
- Even if manual changes happen, reconciler corrects

### 2. **Fast API Responses**
- API returns in < 50ms
- No blocking on operations
- Better UX

### 3. **Simple API Logic**
- API just updates database record
- No complex operation creation
- Easier to maintain

### 4. **Self-Healing**
- Manual changes automatically corrected
- Failed operations retried
- Drift detected and fixed

### 5. **Kubernetes-Native**
- Same pattern as Kubernetes itself
- Declarative: "I want 3 replicas" not "Add 1 replica"
- Familiar to Kubernetes users

### 6. **No Race Conditions**
- Reconciler is single-threaded for each database
- Operation queue handles concurrency
- Deduplication prevents duplicates

---

## Comparison: Old vs New

### Old (Imperative)

```python
# API
@router.patch("/databases/{id}")
async def update_database(...):
    operation = Operation(...)
    await operation_queue.enqueue(operation.id)  # Create operation
    return {"operation_id": operation.id}        # Return operation ID

# User must poll:
GET /operations/{operation_id}
```

**Problems:**
- API creates operations (complex logic)
- User must poll operation status
- No natural way to handle rapid updates
- If API crashes, operations not created

### New (Declarative)

```python
# API
@router.patch("/databases/{id}")
async def update_database(...):
    db.size = update_data.size  # Update desired state
    await db.save()             # Save to database
    return db                   # Return updated database

# User just reads database:
GET /databases/{id}
```

**Benefits:**
- API is simple (just update database)
- User polls database status (same endpoint)
- Rapid updates naturally handled (last write wins)
- Reconciler handles operation creation (robust)

---

## Configuration

### Reconciliation Interval

**Default:** 300 seconds (5 minutes)

**Environment Variable:** `RECONCILIATION_INTERVAL`

```yaml
# docker-compose.yml
reconciler:
  environment:
    - RECONCILIATION_INTERVAL=300  # 5 minutes

# For faster reconciliation (development):
    - RECONCILIATION_INTERVAL=60   # 1 minute
```

**Trade-offs:**
- **Shorter (60s):** Faster drift detection, more K8s API calls
- **Longer (600s):** Slower drift detection, fewer K8s API calls

---

## Monitoring

### Key Metrics

```prometheus
# Drift detection
dbaas_drift_detected_total{type="size|replicas|storage"}

# Current vs desired state mismatch
dbaas_state_drift{database_id="db-123", field="size"}

# Reconciliation cycles
dbaas_reconciliation_cycle_total
dbaas_reconciliation_cycle_duration_seconds

# Operations created by reconciler
dbaas_operations_created_total{source="reconciler"}
```

### Grafana Dashboards

**1. Drift Detection**
- Databases with drift over time
- Drift by type (size, replicas, storage)
- Time to resolve drift (P50, P95, P99)

**2. State Sync**
- Databases with desired != current
- Age of oldest drift
- Reconciliation lag

---

## Operational Guide

### Deployment

```bash
# Docker Compose (includes all components)
docker-compose up -d

# Services:
# - app: API (does pre-checks, updates DB)
# - reconciler: Detects drift (1 replica)
# - worker: Executes operations (2+ replicas)
# - mongodb, redis, prometheus, grafana
```

### Scaling

```bash
# Scale operation workers for throughput
docker-compose up -d --scale worker=5

# Kubernetes
kubectl scale deployment kubedb-dbaas-worker --replicas=5

# NEVER scale reconciler beyond 1 replica
# (Would create duplicate operations)
```

### Troubleshooting

**Problem:** Database not reaching desired state

**Diagnosis:**
```bash
# 1. Check desired vs current state
curl http://localhost:8000/api/v1/databases/db-123 | jq '{
  desired: {size: .size, replicas: .replicas, storage_gb: .storage_gb},
  current: {size: .current_size, replicas: .current_replicas, storage_gb: .current_storage_gb}
}'

# 2. Check if reconciler is running
docker-compose ps reconciler

# 3. Check reconciler logs
docker-compose logs -f reconciler | grep drift

# 4. Check for operations
curl http://localhost:8000/api/v1/operations?database_id=db-123
```

**Problem:** Drift not detected fast enough

**Solution:** Reduce reconciliation interval
```yaml
RECONCILIATION_INTERVAL=60  # 1 minute instead of 5
```

---

## Summary

**Declarative Architecture:**
1. **API** → Updates desired state → Returns immediately
2. **Reconciler** → Detects drift → Creates operations
3. **Worker** → Executes operations → Updates current state
4. **KubeDB** → Performs actual scaling

**Key Principles:**
- Desired state in database (what user wants)
- Current state synced from KubeDB (what's actually running)
- Reconciler ensures eventual consistency
- Self-healing and drift correction

**Benefits:**
- Fast API (<50ms)
- Simple API logic
- Kubernetes-native pattern
- Self-healing
- Eventually consistent

This is how production systems like Kubernetes, Terraform, and AWS CloudFormation work!
