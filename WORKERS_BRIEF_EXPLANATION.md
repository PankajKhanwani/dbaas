# Workers Brief Explanation

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    DECLARATIVE SYSTEM                        │
│                                                              │
│  API → Reconciler → Operation Queue → Worker → KubeDB      │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. Reconciliation Worker

### What It Does
**Detects drift** between what you want (desired state) and what's actually running (current state), then creates operations to fix the drift.

### Core Concept
```python
if desired_state != current_state:
    create_operation_to_fix_it()
```

### Workflow

```
┌──────────────────────────────────────────────────────────────┐
│                  RECONCILIATION WORKER                        │
└──────────────────────────────────────────────────────────────┘

Every 10-30 seconds:

1. Fetch all databases from MongoDB
   ├─ db.size (desired)
   ├─ db.current_size (actual)
   └─ db.replicas, storage_gb, etc.

2. For each database:
   ├─ Get KubeDB CR from Kubernetes
   ├─ Parse current state (replicas, storage, size)
   └─ Update db.current_* fields

3. Compare desired vs current:
   ├─ size != current_size? → Create SCALE_VERTICAL operation
   ├─ replicas != current_replicas? → Create SCALE_HORIZONTAL operation
   └─ storage_gb != current_storage_gb? → Create EXPAND_STORAGE operation

4. Enqueue operations to Redis queue
   └─ With deduplication (won't create duplicates)

5. Sleep and repeat
```

### Example

```python
# Database state:
db.size = "db.t3.large"           # Desired (user wants)
db.current_size = "db.t3.medium"   # Current (actually running)

# Reconciler detects:
"db.t3.large != db.t3.medium"  → DRIFT!

# Action:
operation = Operation(
    type="SCALE_VERTICAL",
    desired_state={"size": "db.t3.large"}
)
→ Enqueue to Redis
```

### Key Features
- ✅ **Runs continuously** (background loop)
- ✅ **Read-only** (only reads Kubernetes, doesn't modify)
- ✅ **Deduplication** (won't spam operations)
- ✅ **Self-healing** (detects manual changes and reverts)

### Configuration
```bash
RECONCILE_INTERVAL=30  # Seconds between cycles (default: 30)
```

---

## 2. Operation Worker

### What It Does
**Executes operations** by creating KubeDB OpsRequests and monitoring them until complete.

### Core Concept
```python
operation = dequeue_from_redis()
execute(operation)  # Create OpsRequest, wait for KubeDB
update_current_state()  # Sync back to database
```

### Workflow

```
┌──────────────────────────────────────────────────────────────┐
│                    OPERATION WORKER                           │
└──────────────────────────────────────────────────────────────┘

Continuous loop:

1. Dequeue operation from Redis
   └─ Blocking wait (1 second timeout)

2. Load operation & database from MongoDB
   └─ Validate both exist

3. Mark operation as IN_PROGRESS
   └─ Update status, timestamps

4. Execute based on type:

   A. SCALE_VERTICAL (CPU/Memory):
      ├─ Create MongoDBOpsRequest in Kubernetes
      ├─ Monitor every 5 seconds
      │  ├─ Phase: Pending → Progressing → Successful
      │  └─ Update progress: 30% → 90% → 100%
      └─ Wait for completion (~30 seconds)

   B. SCALE_HORIZONTAL (Replicas):
      ├─ Patch MongoDB CR directly
      └─ Quick (~5 seconds)

   C. EXPAND_STORAGE:
      ├─ Patch MongoDB CR directly
      └─ Quick (~5 seconds)

5. Update database current_* fields:
   └─ db.current_size = desired_size
   └─ db.current_replicas = desired_replicas

6. Cleanup:
   └─ Delete OpsRequest from Kubernetes

7. Mark operation as COMPLETED
   └─ Remove from queue

8. Repeat (dequeue next operation)
```

### Example (Vertical Scaling)

```python
# Operation details:
operation.type = "SCALE_VERTICAL"
operation.desired_state = {"size": "db.t3.large"}

# Worker executes:
Step 1: Create OpsRequest
  └─ Name: "pankaj123-demo-demo-scale-1764670590"
  └─ Resources: CPU=2, Memory=4Gi

Step 2: Monitor OpsRequest (every 5 seconds)
  00:00 - Phase: Pending (Progress: 30%)
  00:05 - Phase: Progressing (Progress: 50%)
  00:25 - Phase: Successful (Progress: 100%)

Step 3: Update database
  └─ db.current_size = "db.t3.large"

Step 4: Delete OpsRequest
  └─ Cleanup from Kubernetes

Step 5: Done!
  └─ Duration: ~30 seconds
```

### Key Features
- ✅ **Asynchronous** (non-blocking)
- ✅ **Progress tracking** (0% → 100%)
- ✅ **Retry logic** (max 3 retries)
- ✅ **Timeout handling** (10 minutes max)
- ✅ **Auto cleanup** (deletes OpsRequests)
- ✅ **Scalable** (run multiple workers)

---

## How They Work Together

### Full Lifecycle Example

```
USER ACTION:
PATCH /databases/db-123 {"size": "db.t3.large"}
↓

API LAYER:
- Validates request
- Updates: db.size = "db.t3.large" (desired state)
- Returns immediately (< 50ms)
- Does NOT create operations

↓

RECONCILER (Next Cycle - Every 30s):
1. Reads database from MongoDB:
   - Desired: db.size = "db.t3.large"
   - Current: db.current_size = "db.t3.medium"

2. Reads KubeDB CR from Kubernetes:
   - Actual resources: CPU=1, Memory=2Gi

3. Compares:
   - "db.t3.large != db.t3.medium" → DRIFT!

4. Creates operation:
   - Type: SCALE_VERTICAL
   - Desired: {"size": "db.t3.large"}

5. Enqueues to Redis

↓

OPERATION WORKER:
1. Dequeues operation from Redis

2. Marks as IN_PROGRESS

3. Creates MongoDBOpsRequest:
   - Name: "db-123-scale-1764670590"
   - CPU: 2, Memory: 4Gi

4. Monitors every 5 seconds:
   - Pending → Progressing → Successful

5. Updates current state:
   - db.current_size = "db.t3.large"

6. Deletes OpsRequest

7. Marks as COMPLETED

↓

RECONCILER (Next Cycle):
1. Compares:
   - Desired: "db.t3.large"
   - Current: "db.t3.large"

2. No drift! ✅

3. No operation needed

↓

SYSTEM STABLE ✅
```

---

## Key Differences

| Aspect | Reconciler | Worker |
|--------|-----------|--------|
| **Role** | Detect drift | Execute changes |
| **Reads from** | MongoDB + Kubernetes | MongoDB + Redis |
| **Writes to** | MongoDB (current_*) + Redis | MongoDB (current_*) + Kubernetes |
| **Runs** | Every 30 seconds | Continuously (polls queue) |
| **Speed** | Fast (reads only) | Slow (waits for KubeDB) |
| **Concurrency** | 1 instance only | Multiple instances OK |
| **Creates** | Operations | OpsRequests |

---

## Deduplication

### Problem Without Deduplication
```
Reconciler Cycle 1: Creates operation "scale to large"
Reconciler Cycle 2: Creates operation "scale to large" (duplicate!)
Reconciler Cycle 3: Creates operation "scale to large" (duplicate!)
→ 3 operations for same change!
```

### Solution: Dedup Key
```python
dedup_key = f"{database_id}:{operation_type}"
# Example: "db-123:scale_vertical"

# Redis checks:
if dedup_key exists in queue:
    reject_operation()  # Duplicate!
else:
    enqueue_operation()
    store_dedup_key()
```

### Result
```
Reconciler Cycle 1: Creates operation ✅
Reconciler Cycle 2: Duplicate detected, skipped ✅
Reconciler Cycle 3: Duplicate detected, skipped ✅
→ Only 1 operation created!
```

---

## State Management

### Two Types of State

**Desired State** (What user wants):
```python
db.size = "db.t3.large"
db.replicas = 3
db.storage_gb = 50
```

**Current State** (What's actually running):
```python
db.current_size = "db.t3.medium"
db.current_replicas = 3
db.current_storage_gb = 50
```

### Who Updates What

**API Updates:**
- ✅ Desired state (`db.size`, `db.replicas`, `db.storage_gb`)
- ❌ Current state (never touches current_*)

**Reconciler Updates:**
- ❌ Desired state (never touches it)
- ✅ Current state (syncs from KubeDB CR)
- ⚠️ **Only if value is not None** (after fix)

**Worker Updates:**
- ❌ Desired state (never touches it)
- ✅ Current state (after operation completes)

---

## Error Handling

### Reconciler Errors
```python
try:
    reconcile_database()
except Exception as e:
    log_error(e)
    continue  # Move to next database
    # Don't crash - keep running
```

### Worker Errors
```python
try:
    execute_operation()
except Exception as e:
    mark_as_failed()

    if can_retry():  # retry_count < 3
        requeue_with_higher_priority()
    else:
        mark_database_as_failed()
```

---

## Monitoring

### Key Metrics

**Reconciler:**
```prometheus
dbaas_reconciliation_cycle_total
dbaas_drift_detected_total{type="size|replicas|storage"}
dbaas_operations_created_total{source="reconciler"}
```

**Worker:**
```prometheus
dbaas_operations_completed_total{type="scale_vertical",status="completed"}
dbaas_operation_duration_seconds
dbaas_worker_busy{worker_id="1"}
```

### Key Logs

**Reconciler:**
```log
[info] reconciliation_started provider_count=2
[info] size_drift_detected desired=db.t3.large current=db.t3.medium
[info] drift_detected_creating_operations operation_count=1
[info] reconciliation_completed
```

**Worker:**
```log
[info] processing_operation operation_id=op-abc123
[info] creating_ops_request ops_request_name=db-scale-1234
[info] ops_request_successful duration=28.5s
[info] operation_completed
```

---

## Scaling

### Reconciler: DO NOT SCALE
```bash
# WRONG - Creates duplicate operations!
docker-compose up --scale reconciler=3  # ❌

# CORRECT - Only 1 instance
docker-compose up --scale reconciler=1  # ✅
```

### Worker: SCALE FREELY
```bash
# CORRECT - Higher throughput
docker-compose up --scale worker=5  # ✅

# Each worker processes different operations
# Redis queue distributes work
```

---

## Summary

**Reconciler:**
- 🔍 **Detective** - Finds problems (drift)
- 📋 **Planner** - Creates operations to fix them
- 🔄 **Continuous** - Runs every 30 seconds
- 🎯 **Single instance** - Must be only 1

**Worker:**
- 🔨 **Executor** - Does the actual work
- ⏱️ **Patience** - Waits for KubeDB to finish
- 📊 **Reporter** - Updates progress & state
- 🚀 **Scalable** - Can run multiple instances

**Together:**
- ✅ **Eventually consistent** system
- ✅ **Self-healing** (detects & fixes drift)
- ✅ **Kubernetes-native** pattern
- ✅ **Production-ready** architecture

---

**Think of it like:**
- **Reconciler** = Quality Inspector (checks if things match specs)
- **Worker** = Factory Worker (builds/fixes things)
- **API** = Sales Team (takes orders from customers)
- **Redis Queue** = Task Board (tracks what needs to be done)
