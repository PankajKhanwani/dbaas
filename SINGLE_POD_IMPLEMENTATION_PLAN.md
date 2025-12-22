# Single Pod Architecture - Implementation Plan

## Current State Analysis

### Current Architecture:

1. **API Server** (`app/main.py`):
   - ✅ FastAPI application
   - ✅ Already starts `DatabaseReconciler` in lifespan
   - ✅ Runs as single process

2. **Operation Worker** (`app/workers/operation_worker.py`):
   - ❌ **Separate process/script**
   - Processes operations from Redis queue
   - Has own `main()` function
   - Currently run separately: `python -m app.workers.operation_worker`

3. **Reconciler**:
   - ✅ **DatabaseReconciler** (`app/services/status_sync_service.py`):
     - Already integrated in `app/main.py` lifespan
     - Syncs status from KubeDB
     - Handles operations (update, delete, scale, pause, resume)
   - ❌ **ReconciliationWorker** (`app/workers/reconciliation_worker.py`):
     - **Separate process** (not integrated)
     - Detects drift and creates operations
     - Currently run separately: `python -m app.workers.reconciliation_worker`

---

## Issues Identified

### 1. **Duplicate Reconciler Logic** ⚠️
- **Problem**: Two different reconcilers:
  - `DatabaseReconciler`: Syncs status and handles operations directly
  - `ReconciliationWorker`: Detects drift and creates operations
- **Impact**: Confusion, potential conflicts
- **Solution**: Keep `DatabaseReconciler` (already integrated), optionally integrate `ReconciliationWorker` for drift detection

### 2. **Separate Processes** ❌
- **Problem**: Operation worker and reconciliation worker run as separate processes
- **Impact**: Need 3 separate pods/containers, resource overhead
- **Solution**: Integrate both into `app/main.py` lifespan

### 3. **Resource Inefficiency** ⚠️
- **Problem**: 3 separate processes = 3 containers
- **Impact**: Higher resource usage, more complex deployment
- **Solution**: Single pod with all 3 components

### 4. **Shutdown Issues** ✅ (Already Fixed)
- **Status**: Fixed in previous changes
- Workers now stop gracefully with cancellation support

### 5. **Missing OpsRequest Cleanup** ✅ (User Fixed)
- **Status**: User added cleanup code
- `delete_ops_request` method exists in `kubedb_service`

### 6. **Database State Update Issues** ✅ (User Fixed)
- **Status**: User fixed `_update_current_state_after_operation` to use targeted updates
- Prevents overwriting desired state

### 7. **ReconciliationWorker Not Integrated** ❌
- **Problem**: `ReconciliationWorker` runs separately
- **Impact**: Need separate deployment
- **Solution**: Integrate into main app or merge into `DatabaseReconciler`

---

## Proposed Changes

### Goal: Single Pod with 3 Components

All 3 components run in the same process:
1. **API Server** - FastAPI app (already running) ✅
2. **Operation Worker** - Background task processing queue ❌ (needs integration)
3. **Reconciler** - Background task syncing status ✅ (already integrated)

---

## Implementation Plan

### Phase 1: Backup ✅ COMPLETED
- [x] Created backup in `backup_before_single_pod/`
- [x] Documented current structure

### Phase 2: Integrate Operation Worker into Main App

**File: `app/main.py`**

**Changes**:
1. Import `OperationWorker`:
   ```python
   from app.workers.operation_worker import OperationWorker
   ```

2. Create global worker instance (after imports):
   ```python
   # Global operation worker instance
   operation_worker: Optional[OperationWorker] = None
   ```

3. In `lifespan` startup (after Redis connection):
   ```python
   # Start operation worker
   logger.info("starting_operation_worker")
   global operation_worker
   operation_worker = OperationWorker(worker_id=1)
   await operation_worker.start()
   logger.info("operation_worker_started")
   ```

4. In `lifespan` shutdown (before closing connections):
   ```python
   # Stop operation worker
   global operation_worker
   if operation_worker:
       try:
           await operation_worker.stop()
           logger.info("operation_worker_stopped")
       except Exception as e:
           logger.error("operation_worker_stop_error", error=str(e))
   ```

**File: `app/workers/operation_worker.py`**

**Changes**:
1. Ensure `start()` method runs as background task (already does) ✅
2. Ensure `stop()` method works when called from lifespan (already does) ✅
3. Keep `main()` function for backward compatibility (standalone mode)

**No changes needed** - worker already designed to run as background task

### Phase 3: Decision on ReconciliationWorker

**Options**:

**Option A**: Integrate `ReconciliationWorker` into main app
- Keeps drift detection separate from status sync
- Two reconcilers: one for status sync, one for drift detection
- More modular

**Option B**: Merge drift detection into `DatabaseReconciler`
- Single reconciler handles both status sync and drift detection
- Simpler architecture
- Less duplication

**Recommendation**: **Option A** - Integrate `ReconciliationWorker` as separate background task
- Keeps concerns separated
- Easier to maintain
- Both run in same process

**If Option A**:
- Add `ReconciliationWorker` to `app/main.py` lifespan
- Start/stop alongside operation worker

**If Option B**:
- Enhance `DatabaseReconciler` to detect drift
- Remove `ReconciliationWorker` (or keep for reference)

### Phase 4: Update Dockerfile

**Current**:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--loop", "uvloop", "--http", "httptools"]
```

**New**:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--http", "httptools"]
```

**Note**: Remove `--workers` flag since:
- Workers run as background tasks in same process
- Multiple uvicorn workers would create duplicate background tasks
- Single process with background tasks is more efficient

### Phase 5: Update Kubernetes Deployment

**File: `k8s/app/deployment.yaml`**

**Changes**:
- Verify single container (already is)
- Update resource requests/limits if needed (workers add load)
- Update health checks (already configured)
- Consider increasing resources:
  ```yaml
  resources:
    requests:
      cpu: 1000m  # Increased from 500m
      memory: 1Gi  # Increased from 512Mi
    limits:
      cpu: 2000m  # Increased from 1000m
      memory: 2Gi  # Increased from 1Gi
  ```

---

## Detailed File Changes

### File 1: `app/main.py`

**Additions** (after line 25):
```python
from typing import Optional
from app.workers.operation_worker import OperationWorker

# Global operation worker instance
operation_worker: Optional[OperationWorker] = None
```

**In lifespan startup** (after Redis connection, around line 68):
```python
# Start operation worker
logger.info("starting_operation_worker")
global operation_worker
operation_worker = OperationWorker(worker_id=1)
await operation_worker.start()
logger.info("operation_worker_started")
```

**In lifespan shutdown** (before closing connections, around line 90):
```python
# Stop operation worker
global operation_worker
if operation_worker:
    try:
        await operation_worker.stop()
        logger.info("operation_worker_stopped")
    except Exception as e:
        logger.error("operation_worker_stop_error", error=str(e))
```

### File 2: `app/workers/operation_worker.py`

**No changes needed** - already designed correctly ✅

**Optional**: Add deprecation warning to `main()`:
```python
async def main():
    """Run operation worker (standalone mode - deprecated)."""
    import warnings
    warnings.warn(
        "Standalone operation worker is deprecated. "
        "Operation worker is now integrated into main app.",
        DeprecationWarning,
        stacklevel=2
    )
    # ... rest of function
```

### File 3: `Dockerfile`

**Change line 65**:
```dockerfile
# Before:
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--loop", "uvloop", "--http", "httptools"]

# After:
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--http", "httptools"]
```

### File 4: `k8s/app/deployment.yaml` (Optional)

**Update resources** (lines 114-120):
```yaml
resources:
  requests:
    cpu: 1000m  # Increased for worker tasks
    memory: 1Gi
  limits:
    cpu: 2000m
    memory: 2Gi
```

---

## Testing Checklist

### Before Implementation:
- [x] Backup created in `backup_before_single_pod/`
- [ ] Review all changes
- [ ] Get user approval

### After Implementation:
- [ ] Test API server starts
- [ ] Test operation worker starts
- [ ] Test reconciler starts (already working)
- [ ] Test operation processing
- [ ] Test graceful shutdown
- [ ] Test all 3 components running together
- [ ] Deploy to test environment
- [ ] Monitor logs for errors

---

## Rollback Plan

If issues occur:
1. Restore from `backup_before_single_pod/`
2. Revert changes to `app/main.py`
3. Keep workers as separate processes
4. Update deployment to use separate containers

---

## Questions for User

1. **ReconciliationWorker**: 
   - Integrate into main app? (Option A)
   - Or merge into DatabaseReconciler? (Option B)
   - **Recommendation**: Option A - Keep separate for modularity

2. **Worker ID**: 
   - Use fixed ID=1 for operation worker?
   - Or generate unique ID per pod instance?
   - **Recommendation**: Fixed ID=1 (single pod)

3. **Resource Limits**: 
   - Increase CPU/memory for combined workload?
   - **Recommendation**: Yes, increase to 1000m CPU, 1Gi memory

4. **Uvicorn Workers**: 
   - Remove `--workers` flag?
   - **Recommendation**: Yes, single process with background tasks

---

## Summary

**Files to Modify**:
1. ✅ `app/main.py` - Add operation worker startup/shutdown
2. ⚠️ `app/workers/operation_worker.py` - Optional deprecation warning
3. ✅ `Dockerfile` - Remove `--workers` flag
4. ⚠️ `k8s/app/deployment.yaml` - Optional resource increase

**Files Already OK**:
1. ✅ `app/services/status_sync_service.py` - Already integrated
2. ✅ `app/workers/operation_worker.py` - Already designed correctly

**Backup Location**: `backup_before_single_pod/`

**Estimated Changes**: 
- 1 file major change (`app/main.py`)
- 1 file minor change (`Dockerfile`)
- 2 files optional changes

**Risk Level**: Low (backup created, minimal changes)

