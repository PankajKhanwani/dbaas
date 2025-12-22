# Operation Worker - Detailed Technical Documentation

## Overview

The **Operation Worker** is a background process that executes database operations asynchronously. It's the "executor" component in the declarative architecture that actually performs the work of scaling databases, expanding storage, etc.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        OPERATION WORKER                          │
│                                                                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │ Main Loop    │───▶│ Dequeue from │───▶│  Process     │     │
│  │ (Continuous) │    │ Redis Queue  │    │  Operation   │     │
│  └──────────────┘    └──────────────┘    └──────────────┘     │
│         ▲                                        │               │
│         │                                        ▼               │
│         │                                ┌──────────────┐       │
│         │                                │   Monitor    │       │
│         └────────────────────────────────│ OpsRequest  │       │
│                                          └──────────────┘       │
└─────────────────────────────────────────────────────────────────┘
                           │
                           │ Creates & Monitors
                           ▼
              ┌─────────────────────────┐
              │   KubeDB OpsRequest     │
              │  (in Kubernetes API)    │
              └─────────────────────────┘
                           │
                           │ Executes
                           ▼
              ┌─────────────────────────┐
              │   Database Pods         │
              │  (MongoDB/PostgreSQL)   │
              └─────────────────────────┘
```

---

## Core Components

### 1. Worker Lifecycle

**File:** `app/workers/operation_worker.py`

```python
class OperationWorker:
    def __init__(self, worker_id: int = 1):
        self.worker_id = worker_id              # Unique ID (for multiple workers)
        self.running = False                     # Control flag
        self.current_operation_id = None         # Currently processing
        self._current_task = None                # Async task handle
        self._shutdown_timeout = 5               # Graceful shutdown time
```

**Key Properties:**
- `worker_id`: Identifies this worker instance (for scaling to multiple workers)
- `running`: Boolean flag to control the main loop
- `current_operation_id`: Tracks which operation is being processed
- `_current_task`: Holds the async task for cancellation during shutdown

### 2. Main Processing Loop

**Location:** `operation_worker.py:47-102`

```python
async def start(self):
    """Start worker (runs until stopped)."""
    self.running = True

    while self.running:
        try:
            # STEP 1: Dequeue from Redis (1 second timeout)
            operation_id = await operation_queue.dequeue(timeout=1)

            if not operation_id:
                # No operations in queue, wait and retry
                await asyncio.sleep(1)
                continue

            # STEP 2: Process the operation
            self.current_operation_id = operation_id
            self._current_task = asyncio.create_task(
                self.process_operation(operation_id)
            )

            # STEP 3: Wait for completion
            await self._current_task

        except asyncio.CancelledError:
            # Graceful shutdown requested
            break
        except Exception as e:
            # Log error and continue
            logger.error("worker_error", error=str(e))
            await asyncio.sleep(5)
```

**Flow:**
1. **Continuously polls** Redis queue for operations
2. **Dequeues** with 1-second timeout (allows quick shutdown)
3. **Processes** operation asynchronously
4. **Handles errors** without crashing
5. **Repeats** until `self.running = False`

---

## Operation Processing Flow

### Complete Processing Pipeline

**Location:** `operation_worker.py:139-248`

```python
async def process_operation(self, operation_id: str):
    """
    Process a single operation.

    Flow:
    1. Load operation from DB
    2. Mark as IN_PROGRESS
    3. Execute operation (create OpsRequest, monitor it)
    4. Update status (COMPLETED or FAILED)
    """
```

### Detailed Step-by-Step

#### **STEP 1: Load Operation & Database**

```python
# Mark as processing in Redis queue
await operation_queue.mark_processing(operation_id)

# Set worker as busy (for metrics)
metrics.set_worker_busy(self.worker_id, True)

# Load operation record from MongoDB
operation = await Operation.get(operation_id)
if not operation:
    logger.error("operation_not_found")
    return

# Load associated database
db = await Database.get(operation.database_id)
if not db:
    operation.status = OperationStatus.FAILED
    operation.error_message = "Database not found"
    await operation.save()
    return
```

**What happens:**
- Operation retrieved from MongoDB
- Database record retrieved
- Validation that both exist

#### **STEP 2: Mark as IN_PROGRESS**

```python
# Record operation start in metrics
metrics.record_operation_start(operation.type.value)

# Update operation status
operation.status = OperationStatus.IN_PROGRESS
operation.started_at = datetime.now(timezone.utc)
operation.estimated_completion_at = datetime.now(timezone.utc) + timedelta(minutes=5)
await operation.save()
```

**What happens:**
- Status changes from `QUEUED` → `IN_PROGRESS`
- Timestamps recorded
- User can see operation is being processed

#### **STEP 3: Execute Based on Operation Type**

```python
# Route to specific handler
if operation.type == OperationType.SCALE_VERTICAL:
    await self.process_scale_vertical(operation, db)
elif operation.type == OperationType.SCALE_HORIZONTAL:
    await self.process_scale_horizontal(operation, db)
elif operation.type == OperationType.EXPAND_STORAGE:
    await self.process_expand_storage(operation, db)
else:
    raise ValueError(f"Unsupported operation type: {operation.type}")
```

**Three Operation Types:**

##### **A. Vertical Scaling (CPU/Memory)**

**Location:** `operation_worker.py:274-336`

```python
async def process_scale_vertical(self, operation: Operation, db: Database):
    # 1. Get provider kubeconfig
    provider_id, kubeconfig_content = await db_service._get_provider_kubeconfig(db)

    # 2. Update progress
    operation.progress = 10
    operation.message = "Creating OpsRequest"
    await operation.save()

    # 3. Create KubeDB OpsRequest
    target_size = DatabaseSize(operation.desired_state["size"])
    ops_request = await kubedb_service.create_vertical_scaling_ops_request(
        engine=db.engine,
        name=db.kubedb_resource_name,
        namespace=db.namespace,
        size=target_size,
        provider_id=provider_id,
        kubeconfig_content=kubeconfig_content,
    )

    # 4. Store OpsRequest details
    operation.ops_request_name = ops_request["metadata"]["name"]
    operation.ops_request_uid = ops_request["metadata"]["uid"]
    operation.progress = 20
    operation.message = "OpsRequest created, waiting for KubeDB"
    await operation.save()

    # 5. Monitor OpsRequest until completion
    await self.monitor_ops_request(
        operation=operation,
        db=db,
        provider_id=provider_id,
        kubeconfig_content=kubeconfig_content,
        timeout=600,  # 10 minutes
    )
```

**Detailed Flow:**

1. **Create OpsRequest** (in Kubernetes)
   - Generates name: `{db-name}-scale-{timestamp}`
   - Example: `pankaj123-demo-demo-scale-1764668432`
   - Specifies new CPU/memory resources
   - KubeDB operator picks it up

2. **Monitor Progress** (polling every 5 seconds)
   - Checks OpsRequest phase: `Pending` → `Progressing` → `Successful`
   - Updates operation.progress: 30% → 90% → 100%
   - Updates operation.message: "OpsRequest pending" → "Updating pods"

3. **Wait for Completion**
   - Timeout: 10 minutes (600 seconds)
   - If timeout → raises TimeoutError
   - If phase = "Successful" → continues
   - If phase = "Failed" → raises KubeDBError

##### **B. Horizontal Scaling (Replicas)**

**Location:** `operation_worker.py:338-372`

```python
async def process_scale_horizontal(self, operation: Operation, db: Database):
    # 1. Get provider kubeconfig
    provider_id, kubeconfig_content = await db_service._get_provider_kubeconfig(db)

    # 2. Update progress
    operation.progress = 20
    operation.message = "Updating replicas"
    await operation.save()

    # 3. Patch database directly (no OpsRequest needed)
    await kubedb_service.patch_database(
        engine=db.engine,
        name=db.kubedb_resource_name,
        namespace=db.namespace,
        replicas=operation.desired_state["replicas"],
        provider_id=provider_id,
        kubeconfig_content=kubeconfig_content,
    )

    # 4. Update database record
    db.replicas = operation.desired_state["replicas"]
    await db.save()

    # 5. Mark as complete
    operation.progress = 100
    operation.message = "Replicas updated"
    await operation.save()
```

**Key Difference:**
- **No OpsRequest** created (direct patch)
- **Faster** (no monitoring needed)
- **Simpler** operation

##### **C. Storage Expansion**

**Location:** `operation_worker.py:374-408`

```python
async def process_expand_storage(self, operation: Operation, db: Database):
    # Similar to horizontal scaling
    # Patches storage field directly
    await kubedb_service.patch_database(
        engine=db.engine,
        name=db.kubedb_resource_name,
        namespace=db.namespace,
        storage_gb=operation.desired_state["storage_gb"],
        provider_id=provider_id,
        kubeconfig_content=kubeconfig_content,
    )
```

**Note:** Storage can only be **increased**, never decreased (Kubernetes PVC limitation)

#### **STEP 4: Mark as COMPLETED**

```python
# Mark operation as completed
operation.status = OperationStatus.COMPLETED
operation.progress = 100
operation.completed_at = datetime.now(timezone.utc)
operation.message = "Operation completed successfully"
await operation.save()

# Update current_* fields in database record
await self._update_current_state_after_operation(db, operation)
```

**Updates Current State:**

```python
async def _update_current_state_after_operation(db: Database, operation: Operation):
    """
    Sync current_* fields to match what was just applied.
    """
    if operation.type == OperationType.SCALE_VERTICAL:
        # current_size = desired size
        db.current_size = DatabaseSize(operation.desired_state["size"])

    elif operation.type == OperationType.SCALE_HORIZONTAL:
        # current_replicas = desired replicas
        db.current_replicas = operation.desired_state["replicas"]

    elif operation.type == OperationType.EXPAND_STORAGE:
        # current_storage_gb = desired storage
        db.current_storage_gb = operation.desired_state["storage_gb"]

    await db.save()
```

**Why This Matters:**
- Reconciler compares `desired` vs `current`
- If they match → no drift → no new operation
- This prevents infinite loops!

#### **STEP 5: Cleanup OpsRequest**

```python
# Clean up OpsRequest if it was created (for vertical scaling)
if operation.ops_request_name and operation.type == OperationType.SCALE_VERTICAL:
    try:
        await kubedb_service.delete_ops_request(
            engine=db.engine,
            ops_request_name=operation.ops_request_name,
            namespace=db.namespace,
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )
    except Exception as e:
        # Log error but don't fail the operation
        logger.warning("failed_to_cleanup_ops_request", error=str(e))
```

**Cleanup Logic:**
- Only for vertical scaling (only type that creates OpsRequests)
- Deletes the OpsRequest from Kubernetes
- Prevents cluster clutter
- Failure doesn't fail the operation

#### **STEP 6: Mark Completed in Queue**

```python
# Mark completed in Redis queue
dedup_key = f"{db.id}:{operation.type.value}"
await operation_queue.mark_completed(operation_id, dedup_key=dedup_key)

# Record metrics
duration = operation.get_duration_seconds() or 0
metrics.record_operation_complete(
    operation.type.value,
    OperationStatus.COMPLETED.value,
    duration
)
metrics.record_worker_operation_processed(self.worker_id)
metrics.set_worker_busy(self.worker_id, False)
```

**Queue Cleanup:**
- Removes from Redis queue
- Removes deduplication key (allows new operations)
- Records metrics for monitoring

---

## OpsRequest Monitoring

### Monitoring Loop

**Location:** `operation_worker.py:410-491`

```python
async def monitor_ops_request(
    self,
    operation: Operation,
    db: Database,
    provider_id: str,
    kubeconfig_content: str,
    timeout: int,
):
    """
    Monitor OpsRequest until completion.

    Checks status every 5 seconds and updates progress.
    """
    start_time = datetime.now(timezone.utc)

    while self.running:  # Allow cancellation during shutdown
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

        if elapsed > timeout:
            raise TimeoutError(f"Operation timed out after {timeout}s")

        # Check OpsRequest status from Kubernetes
        status = await kubedb_service.get_ops_request_status(
            engine=db.engine,
            ops_request_name=operation.ops_request_name,
            namespace=db.namespace,
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )

        phase = status["phase"]
        operation.ops_request_phase = phase

        # Update progress based on phase
        if phase == "Pending":
            operation.progress = 30
            operation.message = "OpsRequest pending"

        elif phase == "Progressing":
            # Estimate progress: 30% to 90%
            progress = 30 + int((elapsed / timeout) * 60)
            operation.progress = min(progress, 90)
            operation.message = "Updating pods (rolling restart)"

        elif phase == "Successful":
            operation.progress = 100
            operation.message = "OpsRequest successful"
            await operation.save()

            # Update database status
            db.status = DatabaseStatus.RUNNING
            await db.save()

            return  # Success!

        elif phase == "Failed":
            failure_reason = self.get_failure_reason(status)
            raise KubeDBError(f"OpsRequest failed: {failure_reason}")

        await operation.save()

        # Wait before next check (with cancellation support)
        await asyncio.sleep(5)
```

### OpsRequest Phases

```
┌──────────┐    ┌──────────────┐    ┌────────────┐
│ Pending  │───▶│ Progressing  │───▶│ Successful │
└──────────┘    └──────────────┘    └────────────┘
     │                  │
     │                  │
     └──────────────────┴───────────▶ Failed
```

**Phase Transitions:**

1. **Pending** (30% progress)
   - OpsRequest created
   - Waiting for KubeDB operator to pick it up
   - Duration: ~5-10 seconds

2. **Progressing** (30-90% progress)
   - KubeDB operator executing changes
   - Rolling restart of pods
   - Updating resources
   - Duration: ~20-60 seconds (depends on pod count)

3. **Successful** (100% progress)
   - All pods updated
   - Database healthy
   - Operation complete

4. **Failed**
   - Something went wrong
   - Extract failure reason from conditions
   - Raise error to trigger retry logic

---

## Error Handling & Retries

### Error Flow

```python
except Exception as e:
    logger.error("operation_failed", error=str(e))

    # Mark as failed
    operation = await Operation.get(operation_id)
    if operation:
        operation.status = OperationStatus.FAILED
        operation.error_message = str(e)
        operation.completed_at = datetime.now(timezone.utc)
        await operation.save()

        # Check if can retry
        if operation.can_retry():
            logger.info("operation_retry_scheduled", retry_count=operation.retry_count + 1)

            # Requeue with higher priority
            await operation_queue.requeue_failed(operation_id, priority=10)
        else:
            # Max retries reached
            db.status = DatabaseStatus.FAILED
            await db.save()

            # Mark completed (failed) in queue
            dedup_key = f"{db.id}:{operation.type.value}"
            await operation_queue.mark_completed(operation_id, dedup_key=dedup_key)
```

### Retry Logic

**Conditions:**
- **Max Retries:** 3 (default)
- **Retry Priority:** 10 (higher than normal operations)
- **Retry Delay:** Exponential backoff (handled by queue)

**When Retries Happen:**
- Network errors talking to Kubernetes
- Temporary KubeDB operator issues
- Timeout errors (OpsRequest took too long)

**When Retries Don't Happen:**
- Max retries (3) already attempted
- Operation manually cancelled
- Validation errors (won't succeed even with retry)

---

## Concurrency & Scaling

### Single Worker

```
┌──────────────────┐
│ Operation Worker │
│    (worker_id=1) │
└──────────────────┘
         │
         ▼
   [One operation
    at a time]
```

**Characteristics:**
- Processes operations sequentially
- Simple, predictable
- Good for low-volume workloads

### Multiple Workers

```
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ Operation Worker │    │ Operation Worker │    │ Operation Worker │
│    (worker_id=1) │    │    (worker_id=2) │    │    (worker_id=3) │
└──────────────────┘    └──────────────────┘    └──────────────────┘
         │                       │                       │
         └───────────────────────┴───────────────────────┘
                                 │
                                 ▼
                         ┌───────────────┐
                         │  Redis Queue  │
                         │ (operations)  │
                         └───────────────┘
```

**How to Scale:**

```bash
# Docker Compose
docker-compose up -d --scale worker=5

# Kubernetes
kubectl scale deployment dbaas-worker --replicas=5

# Makefile (manual)
make run-worker  # Terminal 1
make run-worker  # Terminal 2
make run-worker  # Terminal 3
```

**Characteristics:**
- Each worker has unique `worker_id`
- Redis queue distributes operations
- Higher throughput
- Parallel processing

**Deduplication Still Works:**
- Redis dedup keys are global
- Multiple workers won't process same operation
- Safe to scale horizontally

---

## Metrics & Monitoring

### Metrics Collected

**Location:** `operation_worker.py:144, 179-183, 201-203, etc.`

```python
# Operation lifecycle
metrics.record_operation_start(operation.type.value)
metrics.record_operation_complete(operation.type.value, status, duration)
metrics.record_operation_retry(operation.type.value)

# Worker status
metrics.set_worker_busy(worker_id, True/False)
metrics.record_worker_operation_processed(worker_id)
metrics.record_worker_error(worker_id)

# OpsRequest lifecycle
metrics.record_ops_request_created(engine, type)
metrics.record_ops_request_complete(engine, type, phase, duration)
```

### Prometheus Metrics

```prometheus
# Operation metrics
dbaas_operations_started_total{type="scale_vertical"}
dbaas_operations_completed_total{type="scale_vertical",status="completed"}
dbaas_operation_duration_seconds{type="scale_vertical"}

# Worker metrics
dbaas_worker_busy{worker_id="1"} 0
dbaas_worker_operations_processed_total{worker_id="1"} 42
dbaas_worker_errors_total{worker_id="1"} 2

# OpsRequest metrics
dbaas_ops_requests_created_total{engine="mongodb",type="vertical_scaling"}
dbaas_ops_request_duration_seconds{engine="mongodb",type="vertical_scaling"}
```

---

## Graceful Shutdown

### Shutdown Flow

```python
async def stop(self):
    """Stop worker gracefully."""
    logger.info("worker_stopping")
    self.running = False  # Stop main loop

    # Cancel current operation task if running
    if self._current_task and not self._current_task.done():
        logger.info("worker_cancelling_current_operation")
        self._current_task.cancel()

        try:
            # Wait up to 5 seconds for cancellation
            await asyncio.wait_for(self._current_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.warning("worker_operation_cancelled_or_timeout")
```

**Shutdown Sequence:**

1. **Signal Received** (SIGTERM or SIGINT)
   - Signal handler calls `worker.stop()`
   - Sets `self.running = False`

2. **Main Loop Exits**
   - Current iteration finishes
   - No new operations dequeued

3. **Current Operation Handling**
   - If operation in progress:
     - Cancel the async task
     - Wait up to 5 seconds
     - Mark operation as FAILED with error message

4. **Cleanup**
   - Disconnect from Redis queue
   - Close database connections
   - Exit process

**Best Practice:**
```bash
# Send SIGTERM for graceful shutdown
kill -TERM <worker_pid>

# NOT SIGKILL (leaves operations in bad state)
# kill -9 <worker_pid>  # DON'T DO THIS
```

---

## Configuration

### Environment Variables

```bash
# Database connection
MONGODB_URL=mongodb://localhost:27017/kubedb_dbaas

# Redis queue
REDIS_URL=redis://localhost:6379/0

# Kubernetes
KUBECONFIG=/path/to/kubeconfig.yaml

# Worker behavior (future enhancement)
WORKER_TIMEOUT=600           # Operation timeout (seconds)
WORKER_RETRY_MAX=3           # Max retry attempts
WORKER_CONCURRENCY=1         # Operations per worker
```

### Code Configuration

```python
# Timeouts
timeout=600  # 10 minutes for OpsRequest monitoring
self._shutdown_timeout = 5  # Graceful shutdown timeout

# Retry logic
max_retries = 3  # In Operation.can_retry()
retry_priority = 10  # Higher than normal (5)

# Polling intervals
await operation_queue.dequeue(timeout=1)  # Check queue every 1s
await asyncio.sleep(5)  # Check OpsRequest every 5s
```

---

## Debugging & Troubleshooting

### Check Worker Status

```bash
# Is worker running?
ps aux | grep operation_worker

# Check logs
tail -f .logs/worker.log

# Or with Makefile
make logs-worker
```

### Common Issues

#### **Issue 1: Operations Stuck in QUEUED**

**Symptom:**
```bash
$ # Check operations
Operations Status:
  Queued: 26
  In Progress: 0
  Completed: 2
```

**Cause:** Worker not running

**Fix:**
```bash
# Start the worker
make run-worker

# Or check if it crashed
make logs-worker | grep error
```

#### **Issue 2: Operations Timing Out**

**Logs:**
```log
[error] operation_failed error="Operation timed out after 600s"
```

**Causes:**
- KubeDB operator slow/stuck
- Large number of replicas (takes longer)
- Kubernetes API server issues

**Fix:**
- Increase timeout in code
- Check KubeDB operator logs
- Check Kubernetes node resources

#### **Issue 3: OpsRequest Fails**

**Logs:**
```log
[error] operation_failed error="OpsRequest failed: Insufficient resources"
```

**Causes:**
- Not enough CPU/memory in cluster
- PVC expansion not supported
- Invalid resource configuration

**Fix:**
- Check cluster capacity
- Verify storage class supports expansion
- Review resource requests

#### **Issue 4: Worker Crashes**

**Logs:**
```log
[error] worker_error error="Connection refused"
```

**Causes:**
- MongoDB connection lost
- Redis connection lost
- Kubernetes API unreachable

**Fix:**
- Check service availability
- Verify network connectivity
- Check credentials/kubeconfig

---

## Performance Considerations

### Throughput

**Single Worker:**
- ~2-3 operations per minute (30 seconds each)
- Sequential processing

**Multiple Workers:**
- Linear scaling up to queue bottleneck
- 5 workers = ~10-15 operations per minute

### Resource Usage

**Per Worker:**
- **Memory:** ~100-200 MB
- **CPU:** Low (mostly I/O wait)
- **Network:** Moderate (polling Kubernetes API)

**Bottlenecks:**
1. **Kubernetes API Rate Limits**
   - Each check hits API
   - 5 second polling interval
   - 12 requests per minute per operation

2. **KubeDB Operator Capacity**
   - Can handle ~10 concurrent OpsRequests
   - More = queuing on operator side

3. **Redis Queue**
   - Very high throughput
   - Not a bottleneck

---

## Code Organization

```
app/workers/operation_worker.py
├── OperationWorker (Class)
│   ├── __init__()                          # Initialize worker
│   ├── start()                             # Main loop
│   ├── stop()                              # Graceful shutdown
│   ├── process_operation()                 # Core processing logic
│   ├── process_scale_vertical()            # Vertical scaling handler
│   ├── process_scale_horizontal()          # Horizontal scaling handler
│   ├── process_expand_storage()            # Storage expansion handler
│   ├── monitor_ops_request()               # OpsRequest monitoring loop
│   ├── get_failure_reason()                # Extract failure info
│   └── _update_current_state_after_operation()  # Sync current_* fields
│
└── main()                                  # Entry point
    ├── Initialize database connection
    ├── Create OperationWorker instance
    ├── Setup signal handlers (SIGTERM, SIGINT)
    └── Run worker.start()
```

---

## Summary

**The Operation Worker:**

1. **Continuously polls** Redis queue for operations
2. **Dequeues** one operation at a time (per worker)
3. **Executes** based on operation type:
   - Vertical scaling → Create OpsRequest → Monitor → Cleanup
   - Horizontal scaling → Direct patch
   - Storage expansion → Direct patch
4. **Updates state** throughout:
   - Progress percentage (0-100%)
   - Status messages
   - Timestamps
5. **Handles errors** with retry logic (max 3 retries)
6. **Cleans up** OpsRequests after completion
7. **Updates current_*** fields to match desired state
8. **Scales horizontally** with multiple worker instances
9. **Shuts down gracefully** on SIGTERM/SIGINT

**Key Design Decisions:**

- ✅ **Asynchronous**: Non-blocking I/O for better performance
- ✅ **Resilient**: Retries on transient failures
- ✅ **Observable**: Rich logging and metrics
- ✅ **Scalable**: Multiple workers supported
- ✅ **Graceful**: Proper shutdown handling
- ✅ **Clean**: Automatic OpsRequest cleanup

This architecture makes the system production-ready and reliable! 🚀
