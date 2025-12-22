# Production-Grade Scaling Operations Implementation

## Overview

This document describes the production-grade scaling operations implementation for the KubeDB DBaaS platform. All scaling operations now use KubeDB's OpsRequest mechanism for controlled, safe, and monitored database scaling.

---

## Key Features

### ✅ Production-Grade Implementation

1. **OpsRequest-Based Scaling**
   - Vertical scaling (CPU/memory) uses `VerticalScaling` OpsRequest
   - Horizontal scaling (replicas) uses `HorizontalScaling` OpsRequest
   - Combined operations supported when both replicas and size change
   - Storage expansion uses direct patch (handled by Kubernetes PVC expansion)

2. **Comprehensive Error Handling**
   - Input validation (replicas >= 1, storage >= 1GB)
   - Storage decrease prevention
   - Detailed error messages with Kubernetes API responses
   - Graceful fallback for unsupported combined operations

3. **Status Monitoring**
   - Reconciler monitors all OpsRequest phases
   - Automatic status updates based on OpsRequest results
   - Detailed logging for debugging and observability
   - Handles multiple concurrent OpsRequests

4. **Fire-and-Forget Architecture**
   - Operations return immediately with OpsRequest details
   - Reconciler handles status monitoring and updates
   - Non-blocking API responses
   - Supports concurrent operations

---

## Implementation Details

### 1. Vertical Scaling (CPU/Memory)

**Method**: `create_vertical_scaling_ops_request()`

**Process**:
1. Validates size parameter
2. Creates `VerticalScaling` OpsRequest with resource specifications
3. Returns OpsRequest details immediately
4. Reconciler monitors OpsRequest status

**OpsRequest Structure**:
```yaml
apiVersion: ops.kubedb.com/v1alpha1
kind: MongoDBOpsRequest
spec:
  type: VerticalScaling
  databaseRef:
    name: <database-name>
  verticalScaling:
    replicaSet:
      resources:
        requests:
          cpu: "2"
          memory: "4Gi"
        limits:
          cpu: "2"
          memory: "4Gi"
```

**Topology Support**:
- MongoDB: `replicaSet`, `shard`, `configServer`, `mongos`
- PostgreSQL: `postgres`, `coordinator`, `standby`
- MySQL: `mysql`
- MariaDB: `mariadb`
- Redis: `redis`
- Elasticsearch: `node`

---

### 2. Horizontal Scaling (Replicas)

**Method**: `create_horizontal_scaling_ops_request()`

**Process**:
1. Validates replica count (>= 1)
2. Creates `HorizontalScaling` OpsRequest
3. Returns OpsRequest details immediately
4. Reconciler monitors OpsRequest status

**OpsRequest Structure**:
```yaml
apiVersion: ops.kubedb.com/v1alpha1
kind: MongoDBOpsRequest
spec:
  type: HorizontalScaling
  databaseRef:
    name: <database-name>
  horizontalScaling:
    replicaSet:
      replicas: 5
```

**Benefits**:
- Controlled scaling with status tracking
- Prevents conflicts during active operations
- Better error handling than direct patch
- Production-ready approach

---

### 3. Combined Scaling Operations

**Method**: `create_combined_scaling_ops_request()`

**Process**:
1. Validates both replicas and size
2. Attempts to create single OpsRequest with both operations
3. Falls back to separate OpsRequests if combined not supported
4. Returns OpsRequest details

**Fallback Strategy**:
- If combined OpsRequest fails (400 error), creates separate OpsRequests
- Horizontal scaling OpsRequest created first
- Vertical scaling OpsRequest created second
- Both monitored by reconciler

**OpsRequest Structure**:
```yaml
apiVersion: ops.kubedb.com/v1alpha1
kind: MongoDBOpsRequest
spec:
  type: VerticalScaling
  databaseRef:
    name: <database-name>
  verticalScaling:
    replicaSet:
      resources: {...}
  horizontalScaling:
    replicaSet:
      replicas: 5
```

---

### 4. Storage Expansion

**Method**: Direct patch to CR

**Process**:
1. Validates storage size (>= 1GB)
2. Checks current storage to prevent decrease
3. Patches CR with new storage size
4. Kubernetes handles PVC expansion

**Why Direct Patch?**:
- Storage changes handled by Kubernetes PVC expansion
- No pod restarts required in most cases
- Simpler operation, no OpsRequest needed
- Standard Kubernetes practice

---

## API Usage

### Vertical Scaling (Size Change)

```python
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    size=DatabaseSize.DB_T3_MEDIUM,  # Triggers OpsRequest
)
```

**Response**:
```json
{
  "ops_request_name": "my-db-scale-1234567890",
  "ops_request_uid": "abc-123-def",
  "ops_type": "VerticalScaling",
  "message": "OpsRequest created, reconciler will monitor progress",
  "size": "db.t3.medium"
}
```

### Horizontal Scaling (Replicas)

```python
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    replicas=5,  # Triggers OpsRequest
)
```

**Response**:
```json
{
  "ops_request_name": "my-db-scale-1234567890",
  "ops_request_uid": "abc-123-def",
  "ops_type": "HorizontalScaling",
  "message": "OpsRequest created, reconciler will monitor progress",
  "replicas": 5
}
```

### Combined Scaling

```python
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    replicas=5,
    size=DatabaseSize.DB_T3_LARGE,  # Both trigger OpsRequest
)
```

### Storage Expansion

```python
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    storage_gb=100,  # Direct patch
)
```

---

## Status Monitoring

### OpsRequest Phases

1. **Pending** - OpsRequest created, waiting to start
2. **Progressing** - Operation in progress
3. **Successful** - Operation completed successfully
4. **Failed** - Operation failed
5. **Skipped** - Operation skipped (already at desired state)

### Reconciler Monitoring

The `DatabaseReconciler` monitors OpsRequest status:

1. **Finds Active OpsRequests**
   - Lists all OpsRequests for the database
   - Filters by `databaseRef.name`
   - Sorts by creation timestamp

2. **Processes Status**
   - Checks phase of most recent active OpsRequest
   - Updates database status based on phase
   - Extracts detailed error information on failures

3. **Status Updates**
   - `Successful` → Database status updated to `RUNNING` (by main reconciler)
   - `Failed` → Database status set to `FAILED` with error message
   - `Pending/Progressing` → Keeps current status (`UPDATING` or `SCALING`)

4. **Cleanup**
   - Keeps last 3 OpsRequests for history
   - Deletes older completed/failed OpsRequests

---

## Error Handling

### Validation Errors

- **Invalid Replica Count**: `replicas < 1` → `KubeDBError`
- **Invalid Storage Size**: `storage_gb < 1` → `KubeDBError`
- **Storage Decrease**: Attempting to decrease storage → `KubeDBError`

### OpsRequest Errors

- **Creation Failure**: API errors logged with full details
- **Timeout**: OpsRequest monitoring times out after 10 minutes
- **Failure**: Detailed error message extracted from conditions

### Fallback Mechanisms

- **Combined OpsRequest Not Supported**: Falls back to separate OpsRequests
- **OpsRequest Check Failure**: Non-critical, doesn't fail reconciliation
- **Network Errors**: Retry logic with exponential backoff

---

## Logging and Observability

### Key Log Events

1. **Scaling Initiated**
   ```
   scaling_ops_request_created
   - engine, name, namespace
   - ops_request_name, ops_type
   - replicas, size
   ```

2. **OpsRequest Status**
   ```
   ops_request_successful / ops_request_failed
   - database_id, ops_request_name
   - ops_type, phase
   - reason, message (on failure)
   ```

3. **Errors**
   ```
   scaling_operation_failed
   - name, engine
   - replicas, size
   - error, stack trace
   ```

---

## Best Practices

### 1. Use OpsRequest for Scaling ✅
- Always use OpsRequest for CPU/memory and replica changes
- Provides controlled, monitored operations
- Better error handling and status tracking

### 2. Storage Expansion ✅
- Direct patch is fine for storage
- Kubernetes handles PVC expansion
- No OpsRequest needed

### 3. Combined Operations 💡
- Prefer combined OpsRequest when both replicas and size change
- System automatically falls back if not supported
- Both operations still complete successfully

### 4. Monitoring 📊
- Reconciler automatically monitors all OpsRequests
- Status updates happen automatically
- No manual intervention needed

### 5. Error Handling 🛡️
- All errors are logged with full context
- Database status reflects operation state
- Failed operations can be retried

---

## Testing

### Test Scenarios

1. **Vertical Scaling**
   - Change size from `db.t3.small` to `db.t3.medium`
   - Verify OpsRequest created
   - Verify status updates correctly
   - Verify pods get new resources

2. **Horizontal Scaling**
   - Scale replicas from 3 to 5
   - Verify OpsRequest created
   - Verify status updates correctly
   - Verify new pods created

3. **Combined Scaling**
   - Change both replicas and size
   - Verify OpsRequest(s) created
   - Verify both operations complete
   - Verify final state matches desired

4. **Storage Expansion**
   - Increase storage from 20GB to 50GB
   - Verify direct patch succeeds
   - Verify PVC expansion initiated

5. **Error Cases**
   - Invalid replica count (< 1)
   - Invalid storage size (< 1GB)
   - Storage decrease attempt
   - OpsRequest failure

---

## Performance Considerations

1. **Non-Blocking Operations**
   - All scaling operations return immediately
   - No waiting for completion in API layer
   - Better user experience

2. **Concurrent Operations**
   - Multiple databases can scale simultaneously
   - No blocking between operations
   - Efficient resource utilization

3. **Reconciler Efficiency**
   - Batches database reconciliation
   - Groups by provider for client reuse
   - Processes in configurable batch sizes

---

## Future Enhancements

1. **OpsRequest Cleanup Policy**
   - Configurable retention period
   - Automatic cleanup of old OpsRequests
   - Archive completed operations

2. **Scaling History**
   - Track all scaling operations
   - Store OpsRequest details in database
   - Provide scaling history API

3. **Pre-Scaling Validation**
   - Check resource availability before scaling
   - Validate against provider limits
   - Prevent over-allocation

4. **Rollback Support**
   - Automatic rollback on failure
   - Manual rollback capability
   - Restore previous configuration

---

## Summary

The production-grade scaling implementation provides:

✅ **Controlled Operations** - OpsRequest-based scaling with status tracking  
✅ **Comprehensive Error Handling** - Validation, detailed errors, fallback mechanisms  
✅ **Automatic Monitoring** - Reconciler tracks all OpsRequest statuses  
✅ **Non-Blocking API** - Fire-and-forget architecture for better UX  
✅ **Multi-Engine Support** - Works with all KubeDB database engines  
✅ **Production Ready** - Handles edge cases, errors, and concurrent operations  

This implementation follows KubeDB best practices and provides a robust, scalable solution for database operations management.

