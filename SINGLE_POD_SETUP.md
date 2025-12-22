# Single-Pod Architecture Setup Guide

## 🎯 Overview

Your KubeDB DBaaS platform now runs in **single-pod mode** with all components consolidated:

- ✅ **API Server** (FastAPI)
- ✅ **Reconciliation Worker** (with Redis leader election)
- ✅ **3x Operation Workers** (concurrent queue processing)
- ✅ **Status Sync Worker** (KubeDB status updates)

**All in ONE process!**

---

## 🚀 Quick Start

### **Local Development**

```bash
# 1. Start everything (single command!)
make run

# 2. Check status
make status

# 3. View logs
make logs

# 4. Stop
make stop
```

### **Docker Compose**

```bash
# Start with MongoDB + Redis
docker-compose up -d

# View logs
docker-compose logs -f app

# Stop
docker-compose down
```

### **Production (Kubernetes)**

```bash
# Build image
docker build -t kubedb-dbaas:latest .

# Deploy (single deployment, 2 replicas for HA)
kubectl apply -f k8s/app/deployment.yaml

# Check leader election
kubectl logs -f deployment/kubedb-dbaas | grep leader
```

---

## 📋 What Changed

### **1. Makefile - Simplified**

**Before:**
```bash
make run-api       # Start API
make run-reconciler # Start reconciler
make run-worker     # Start worker
```

**After:**
```bash
make run  # Start everything!
```

### **2. docker-compose.yml - Consolidated**

**Before:**
- 3 services: `app`, `worker`, `reconciler`
- 3 containers running

**After:**
- 1 service: `app` (with optional `app-replica`)
- 1-2 containers running

### **3. Dockerfile - Single Process**

**Before:**
```dockerfile
CMD ["uvicorn", "app.main:app", "--workers", "4", ...]
```

**After:**
```dockerfile
CMD ["uvicorn", "app.main:app", "--workers", "1", ...]
# Background workers run inside the same process
```

---

## 🔧 Configuration

### **Environment Variables**

```bash
# Worker Configuration
RECONCILE_INTERVAL=300           # Reconciliation every 5 minutes
NUM_OPERATION_WORKERS=3          # 3 concurrent operation workers
STATUS_SYNC_INTERVAL=30          # Status sync every 30 seconds
```

### **Scaling Recommendations**

| Databases | Replicas | Workers | Reconcile Interval |
|-----------|----------|---------|-------------------|
| < 100     | 1-2      | 3       | 300s (5 min)     |
| 100-500   | 2        | 5       | 300s (5 min)     |
| > 500     | 2-3      | 7       | 600s (10 min)    |

---

## 🔍 Verification

### **1. Check Application is Running**

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "components": {
    "api": "running",
    "workers": "running"
  }
}
```

### **2. Check Leader Election**

```bash
# In logs, you should see ONE pod as leader:
docker-compose logs app | grep leader
```

Expected output:
```
leadership_acquired instance_id=kubedb-dbaas-abc123
```

### **3. Check Workers are Running**

```bash
# In logs:
docker-compose logs app | grep worker
```

Expected output:
```
operation_workers_started count=3
```

---

## 🆘 Troubleshooting

### **Issue: No pod becomes leader**

**Solution:**
```bash
# Check Redis is working
docker-compose exec redis redis-cli PING
# Should return: PONG

# Clear stuck leader manually
docker-compose exec redis redis-cli DEL kubedb:leader:reconciler

# Restart
docker-compose restart app
```

### **Issue: Workers not starting**

**Check logs:**
```bash
docker-compose logs app | grep -i error
```

**Common causes:**
- MongoDB not connected
- Redis not connected
- Missing environment variables

**Fix:**
```bash
# Restart dependencies
docker-compose restart mongodb redis
docker-compose restart app
```

### **Issue: Multiple pods think they're leader**

This shouldn't happen, but if it does:

```bash
# Check Redis is working correctly
docker-compose exec redis redis-cli INFO replication

# Force restart all pods
docker-compose down
docker-compose up -d
```

---

## 📊 Resource Usage

### **Before (Separate Pods)**
```
API:           3 pods × 500MB = 1.5GB
Worker:        2 pods × 500MB = 1.0GB
Reconciler:    1 pod  × 250MB = 0.25GB
────────────────────────────────────
Total:         6 pods           2.75GB
```

### **After (Single Pod)**
```
App:           2 pods × 1GB   = 2GB
────────────────────────────────────
Total:         2 pods          2GB
```

**Savings:** 4 fewer pods, 0.75GB less memory!

---

## 🎓 How It Works

### **Leader Election Flow**

```
Time: 0s
┌─────────┐         ┌─────────┐
│ Pod A   │         │ Pod B   │
│ Try to  │         │ Try to  │
│ acquire │         │ acquire │
│ lock    │         │ lock    │
└────┬────┘         └────┬────┘
     │                   │
     ▼                   ▼
┌────────────────────────────┐
│ Redis: kubedb:leader:      │
│ reconciler                 │
│ SET NX EX 30               │
└────────────────────────────┘
     │                   │
     ▼                   ▼
✅ Pod A           ❌ Pod B
Becomes leader    Becomes standby

Pod A: Runs reconciliation
Pod B: Waits and checks every 5s
```

### **Component Responsibilities**

| Component | What It Does | Runs On |
|-----------|-------------|---------|
| **API Server** | Handle HTTP requests, enqueue operations | All pods |
| **Reconciliation Worker** | Detect drift, create fix operations | Leader only |
| **Operation Workers** | Process queued operations (scale, update, etc.) | All pods |
| **Status Sync** | Update database health_status from KubeDB | All pods |

---

## 🚢 Deployment

### **Production Kubernetes**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubedb-dbaas
spec:
  replicas: 2  # Run 2 pods for HA
  template:
    spec:
      containers:
      - name: app
        image: kubedb-dbaas:latest
        env:
        - name: RECONCILE_INTERVAL
          value: "300"
        - name: NUM_OPERATION_WORKERS
          value: "3"
        - name: STATUS_SYNC_INTERVAL
          value: "30"
        resources:
          requests:
            cpu: 500m
            memory: 512Mi
          limits:
            cpu: 1000m
            memory: 1Gi
```

---

## 📝 Commands Reference

| Command | Description |
|---------|-------------|
| `make run` | Start application (all components) |
| `make stop` | Stop application |
| `make restart` | Restart application |
| `make status` | Check if running |
| `make logs` | View logs |
| `docker-compose up -d` | Start with Docker |
| `docker-compose logs -f app` | View Docker logs |
| `docker-compose down` | Stop Docker |

---

## ✅ Benefits

1. **Fewer Pods** - 6 pods → 2 pods (67% reduction!)
2. **Lower Memory** - 2.75GB → 2GB
3. **Simpler Operations** - One deployment, one set of logs
4. **Faster Startup** - No coordination between services
5. **Better Resource Sharing** - Shared connections, caches
6. **Still Highly Available** - Leader election ensures only one reconciler

---

## 🔗 Related Files

- **Main Application:** `app/main.py`
- **Leader Election:** `app/workers/leader_election.py`
- **Reconciliation Worker:** `app/workers/reconciliation_worker.py`
- **Operation Worker:** `app/workers/operation_worker.py`
- **Settings:** `app/config/settings.py`
- **Environment:** `.env.example`
- **Makefile:** `Makefile`
- **Docker:** `Dockerfile`, `docker-compose.yml`

---

**Questions?** Check the logs first: `make logs` or `docker-compose logs -f app`
