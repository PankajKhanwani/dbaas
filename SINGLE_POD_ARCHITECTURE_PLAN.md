# Single Pod Architecture - Implementation Plan

## Current State Analysis

### Current Architecture:
1. **API Server** (`app/main.py`):
   - FastAPI application
   - Already starts `DatabaseReconciler` in lifespan
   - Runs as single process

2. **Operation Worker** (`app/workers/operation_worker.py`):
   - **Separate process/script**
   - Processes operations from Redis queue
   - Has own `main()` function
   - Currently run separately: `python -m app.workers.operation_worker`

3. **Reconciler**:
   - **DatabaseReconciler** (`app/services/status_sync_service.py`):
     - ✅ Already integrated in `app/main.py` lifespan
     - Syncs status from KubeDB
     - Handles operations (update, delete, scale, pause, resume)
   - **ReconciliationWorker** (`app/workers/reconciliation_worker.py`):
     - ❌ **Separate process** (not integrated)
     - Detects drift and creates operations
     - Currently run separately: `python -m app.workers.reconciliation_worker`

### Issues Identified:

1. **Duplicate Reconciler Logic**:
   - `DatabaseReconciler` handles operations directly
   - `ReconciliationWorker` creates operations for drift
   - **Conflict**: Both try to reconcile, but differently

2. **Separate Processes**:
   - Operation worker runs separately
   - Reconciliation worker runs separately
   - **Problem**: Need 3 separate pods/containers

3. **Resource Inefficiency**:
   - 3 separate processes = 3 separate containers/pods
   - More resource overhead
   - More complex deployment

4. **Shutdown Issues** (Already Fixed):
   - Workers don't stop gracefully (fixed in previous changes)
   - Signal handling needs improvement

5. **Missing Integration**:
   - `ReconciliationWorker` not integrated into main app
   - Operation worker not integrated into main app

---

## Proposed Changes

### Goal: Single Pod with 3 Components

All 3 components run in the same process:
1. **API Server** - FastAPI app (already running)
2. **Operation Worker** - Background task processing queue
3. **Reconciler** - Background task syncing status

---

## Implementation Plan

### Phase 1: Backup Current Implementation ✅
- [x] Create backup in `backup_before_single_pod/`
- [x] Document current structure

### Phase 2: Integrate Operation Worker into Main App

**Changes to `app/main.py`:**

1. **Import OperationWorker**:
   ```python
   from app.workers.operation_worker import OperationWorker
   ```

2. **Create global worker instance**:
   ```python
   operation_worker = OperationWorker(worker_id=1)
   ```

3. **Start worker in lifespan startup**:
   ```python
   # Start operation worker
   logger.info("starting_operation_worker")
   await operation_worker.start()
   logger.info("operation_worker_started")
   ```

4. **Stop worker in lifespan shutdown**:
   ```python
   # Stop operation worker
   try:
       await operation_worker.stop()
       logger.info("operation_worker_stopped")
   except Exception as e:
       logger.error("operation_worker_stop_error", error=str(e))
   ```

**Changes to `app/workers/operation_worker.py`:**

1. **Remove standalone `main()` function** (keep for backward compatibility but mark as deprecated)
2. **Ensure `start()` method runs as background task** (already does)
3. **Fix shutdown to work when called from lifespan**

### Phase 3: Integrate ReconciliationWorker (Optional - May Not Be Needed)

**Decision Needed**: 
- `DatabaseReconciler` already handles reconciliation
- `ReconciliationWorker` creates operations for drift detection
- **Question**: Do we need both, or can `DatabaseReconciler` handle drift detection too?

**Option A**: Keep both (integrate ReconciliationWorker)
**Option B**: Enhance DatabaseReconciler to handle drift detection (simpler)

**Recommendation**: Option B - Enhance DatabaseReconciler

### Phase 4: Update Dockerfile

**Current**:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

**New** (no change needed - all runs in same process):
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
# Note: Remove --workers flag since we're running worker tasks in same process
```

### Phase 5: Update Kubernetes Deployment

**Current**: May have separate deployments for workers
**New**: Single deployment with all components

**Changes to `k8s/app/deployment.yaml`**:
- Ensure only one container
- Update resource requests/limits if needed
- Update health checks

---

## Detailed Changes

### File: `app/main.py`

**Additions**:
```python
# In lifespan startup:
from app.workers.operation_worker import OperationWorker

operation_worker = OperationWorker(worker_id=1)
await operation_worker.start()

# In lifespan shutdown:
await operation_worker.stop()
```

### File: `app/workers/operation_worker.py`

**Changes**:
- `start()` method already returns immediately (runs as background task) ✅
- `stop()` method already implemented ✅
- Keep `main()` for backward compatibility but add deprecation warning

### File: `app/services/status_sync_service.py`

**Current**: Already integrated ✅
**No changes needed**

### File: `Dockerfile`

**Changes**:
- Remove `--workers` flag (or keep for API only, workers run as tasks)
- Single CMD for main app

### File: `k8s/app/deployment.yaml`

**Changes**:
- Ensure single container
- Update resource limits if needed
- Single pod runs all 3 components

---

## Issues to Fix

### 1. **Duplicate Reconciler Logic**
- **Problem**: `DatabaseReconciler` and `ReconciliationWorker` both reconcile
- **Solution**: 
  - Option A: Keep `DatabaseReconciler` for status sync, `ReconciliationWorker` for drift detection
  - Option B: Merge drift detection into `DatabaseReconciler` (recommended)

### 2. **Operation Worker Shutdown**
- **Status**: Already fixed in previous changes ✅
- **Verify**: Ensure graceful shutdown works when called from lifespan

### 3. **Reconciler Sleep Cancellation**
- **Status**: Already fixed in previous changes ✅
- **Verify**: Ensure sleep can be cancelled

### 4. **Resource Cleanup**
- **Issue**: OpsRequests may not be cleaned up
- **Status**: User added cleanup code ✅
- **Verify**: `delete_ops_request` method exists in `kubedb_service`

### 5. **Database State Updates**
- **Issue**: User fixed `_update_current_state_after_operation` to use targeted updates
- **Status**: Already fixed by user ✅

---

## Testing Plan

1. **Unit Tests**:
   - Test worker start/stop
   - Test reconciler start/stop
   - Test graceful shutdown

2. **Integration Tests**:
   - Test all 3 components running together
   - Test operation processing
   - Test reconciliation

3. **Deployment Tests**:
   - Deploy single pod
   - Verify all components running
   - Test graceful shutdown

---

## Rollback Plan

If issues occur:
1. Restore from `backup_before_single_pod/`
2. Revert changes to `app/main.py`
3. Keep workers as separate processes

---

## Questions for User

1. **ReconciliationWorker**: Do you want to keep `ReconciliationWorker` (drift detection) or merge into `DatabaseReconciler`?

2. **Worker ID**: Should operation worker use fixed ID=1 or generate unique ID per pod?

3. **Scaling**: With single pod, how to scale? Multiple pods with same components?

4. **Resource Limits**: Should we adjust CPU/memory limits for combined workload?

---

## Summary

**Files to Modify**:
1. `app/main.py` - Add operation worker startup/shutdown
2. `app/workers/operation_worker.py` - Ensure compatibility with lifespan
3. `Dockerfile` - Update CMD if needed
4. `k8s/app/deployment.yaml` - Verify single container setup

**Files Already OK**:
1. `app/services/status_sync_service.py` - Already integrated ✅

**Backup Location**: `backup_before_single_pod/`

