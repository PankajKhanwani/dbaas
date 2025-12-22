# KubeDB Scaling Operations - Complete Guide

## Overview

KubeDB provides two primary methods for scaling database operations:

1. **Direct CR Patch** - Directly updating the Custom Resource (CR) spec
2. **OpsRequest** - Using KubeDB's OpsRequest CRD for controlled operations

---

## Scaling Types

### 1. Vertical Scaling (CPU/Memory Changes)

**Recommended Method: OpsRequest**

KubeDB uses OpsRequest resources for vertical scaling because:
- ✅ Halts the database to prevent conflicts
- ✅ Updates StatefulSet/PetSet resources atomically
- ✅ Performs rolling restart gracefully
- ✅ Resumes database when complete
- ✅ Provides status tracking and error handling

**OpsRequest Structure:**
```yaml
apiVersion: ops.kubedb.com/v1alpha1
kind: MongoDBOpsRequest
metadata:
  name: <database-name>-scale-<timestamp>
  namespace: <namespace>
spec:
  type: VerticalScaling
  databaseRef:
    name: <database-name>
  verticalScaling:
    replicaSet:  # For MongoDB replicaSet topology
      resources:
        requests:
          cpu: "2"
          memory: "4Gi"
        limits:
          cpu: "2"
          memory: "4Gi"
```

**Current Implementation:**
- ✅ Uses `MongoDBOpsRequest` for vertical scaling (size changes)
- ✅ Fire-and-forget approach (reconciler monitors status)
- ✅ Supports all database engines (MongoDB, PostgreSQL, MySQL, etc.)

**Topology-Specific Fields:**
- **MongoDB**: `replicaSet`, `shard`, `configServer`, `mongos`
- **PostgreSQL**: `postgres`, `coordinator`, `standby`
- **MySQL**: `mysql`
- **MariaDB**: `mariadb`
- **Redis**: `redis`
- **Elasticsearch**: `node`

---

### 2. Horizontal Scaling (Replica Count Changes)

**Two Methods Available:**

#### Method A: Direct CR Patch (Current Implementation)
```yaml
# Direct patch to MongoDB CR
spec:
  replicas: 5
```

**Pros:**
- ✅ Simple and immediate
- ✅ No additional resource creation
- ✅ Works for all engines

**Cons:**
- ⚠️ Less controlled (no status tracking)
- ⚠️ No built-in error handling
- ⚠️ May cause issues during active operations

#### Method B: OpsRequest (Recommended for Production)
```yaml
apiVersion: ops.kubedb.com/v1alpha1
kind: MongoDBOpsRequest
metadata:
  name: <database-name>-scale-<timestamp>
  namespace: <namespace>
spec:
  type: HorizontalScaling
  databaseRef:
    name: <database-name>
  horizontalScaling:
    replicaSet:  # For MongoDB replicaSet
      replicas: 5
```

**Pros:**
- ✅ Controlled operation with status tracking
- ✅ Built-in error handling
- ✅ Prevents conflicts during active operations
- ✅ Better for production environments

**Cons:**
- ⚠️ Requires additional resource creation
- ⚠️ Slightly more complex

**Current Implementation:**
- ⚠️ Uses direct patch (Method A)
- 💡 **Recommendation**: Consider implementing OpsRequest for horizontal scaling

---

### 3. Storage Expansion

**Method: Direct CR Patch**

Storage expansion is typically done via direct patch because:
- ✅ Storage changes are handled by Kubernetes PVC expansion
- ✅ No need for pod restarts in most cases
- ✅ Simpler operation

**Patch Structure:**
```yaml
spec:
  storage:
    resources:
      requests:
        storage: "100Gi"
```

**Current Implementation:**
- ✅ Uses direct patch
- ✅ Validates storage can only increase (not decrease)
- ✅ Works correctly

---

## OpsRequest Status Lifecycle

OpsRequests go through the following phases:

1. **Pending** - OpsRequest created, waiting to start
2. **Progressing** - Operation in progress
3. **Successful** - Operation completed successfully
4. **Failed** - Operation failed
5. **Skipped** - Operation skipped (e.g., already at desired state)

**Status Checking:**
```bash
kubectl get mongodbopsrequests -n <namespace>
kubectl describe mongodbopsrequest <name> -n <namespace>
```

---

## Current Implementation Summary

### ✅ What's Working Well

1. **Vertical Scaling (Size Changes)**
   - Uses OpsRequest ✅
   - Fire-and-forget approach ✅
   - Reconciler monitors status ✅
   - Supports all engines ✅

2. **Storage Expansion**
   - Direct patch ✅
   - Validation (no decrease) ✅
   - Works correctly ✅

### 💡 Potential Improvements

1. **Horizontal Scaling (Replicas)**
   - Currently uses direct patch
   - Could be improved to use OpsRequest for better control
   - Would provide status tracking and error handling

2. **Combined Operations**
   - Currently handles operations separately
   - Could create combined OpsRequest for multiple changes
   - Example: Scale replicas + change size in one operation

---

## Best Practices

1. **Use OpsRequest for Vertical Scaling** ✅ (Already implemented)
   - Ensures proper coordination
   - Prevents conflicts
   - Provides status tracking

2. **Use OpsRequest for Horizontal Scaling in Production** 💡 (Consider implementing)
   - Better control and monitoring
   - Error handling
   - Status tracking

3. **Direct Patch for Storage** ✅ (Current approach is fine)
   - Simpler for storage changes
   - Kubernetes handles PVC expansion

4. **Monitor OpsRequest Status**
   - Reconciler should check OpsRequest phases
   - Update database status based on OpsRequest result
   - Handle failures gracefully

---

## Example: Complete Scaling Operation

### Vertical Scaling (Current Implementation)
```python
# Creates OpsRequest automatically
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    size=DatabaseSize.DB_T3_MEDIUM,  # Triggers OpsRequest
)
```

### Horizontal Scaling (Current Implementation)
```python
# Direct patch
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    replicas=5,  # Direct patch
)
```

### Storage Expansion (Current Implementation)
```python
# Direct patch
await kubedb_service.patch_database(
    engine=DatabaseEngine.MONGODB,
    name="my-db",
    namespace="default",
    storage_gb=100,  # Direct patch
)
```

---

## References

- [KubeDB Vertical Scaling Guide](https://kubedb.com/docs/v2025.10.17/guides/mysql/scaling/vertical-scaling/cluster/)
- [KubeDB Horizontal Scaling Guide](https://kubedb.com/docs/v2025.10.17/guides/mariadb/scaling/horizontal-scaling/cluster/)
- [KubeDB OpsRequest Documentation](https://kubedb.com/docs/v2025.10.17/concepts/ops-request/)

