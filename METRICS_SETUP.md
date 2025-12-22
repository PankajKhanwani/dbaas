# Metrics Setup Guide

## Issue

The metrics endpoint returns:
```json
{
    "message": "Monitoring data is currently unavailable for this database",
    "reason": "The monitoring service is not accessible"
}
```

This happens because the KubeDB stats service is only accessible from **within the Kubernetes cluster**, but your API is running **locally** (outside the cluster).

## Solution Options

### Option 1: Deploy API Inside Cluster (Recommended for Production)

Deploy your FastAPI application inside the Kubernetes cluster where it can directly access the stats services.

```bash
# Build and deploy
docker build -t dbaas-api:latest .
kubectl apply -f k8s/deployment.yaml
```

**Benefits:**
- Direct access to all database stats services
- Production-ready setup
- No port-forwarding needed
- Full production-grade caching works

### Option 2: Port-Forward Stats Service (Development Only)

For local development, you can port-forward the stats service:

```bash
# Find your database stats service
kubectl get svc -n default | grep stats

# Port-forward (example for pans-demo-demo)
kubectl port-forward -n default svc/pans-demo-demo-stats 56790:56790
```

Then the metrics will work for that specific database while the port-forward is active.

**Limitations:**
- Only works for one database at a time
- Requires manual port-forward for each database
- Not suitable for production

### Option 3: Deploy Prometheus (Best for Scale)

Deploy Prometheus to scrape all database metrics centrally:

```bash
# Install Prometheus using Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/prometheus

# Configure KubeDB to be scraped by Prometheus
# (automatically done via ServiceMonitor)
```

Then update `.env` to use PrometheusService instead:

```env
METRICS_SOURCE=prometheus
PROMETHEUS_URL=http://prometheus-server:9090
```

**Benefits:**
- Centralized metrics for all databases
- Metrics history and time-series data
- Works with production-grade caching
- Scales to lakhs of users

## Quick Test

To verify your setup:

```bash
# 1. Check if stats service exists
kubectl get svc -n default | grep stats

# 2. Try accessing directly (from within cluster)
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \\
  curl -s http://pans-demo-demo-stats.default.svc.cluster.local:56790/metrics | head -20

# 3. Test the API metrics endpoint
curl http://localhost:8000/api/v1/domain/demo/project/demo/databases/{database_id}/metrics
```

## Architecture

### Current (Local Development)
```
[Your Computer]
  └─ FastAPI API (localhost:8000)
       └─ ❌ Cannot reach KubeDB stats service (inside cluster)
```

### Fixed (API in Cluster)
```
[Kubernetes Cluster]
  ├─ FastAPI API Pod
  │    └─ ✅ Can reach stats service directly
  └─ Database Stats Service
       └─ Exposes metrics on port 56790
```

### With Prometheus (Production)
```
[Kubernetes Cluster]
  ├─ FastAPI API Pods (scaled)
  │    └─ Query Prometheus API
  ├─ Prometheus Server
  │    └─ Scrapes all database stats services
  └─ Database Stats Services
       └─ Scraped every 15s by Prometheus
```

## Recommended Path

**For Development:**
1. Deploy API in cluster using Docker Desktop Kubernetes
2. Access via `kubectl port-forward svc/dbaas-api 8000:8000`
3. Metrics will work for all databases

**For Production:**
1. Deploy Prometheus using Helm
2. Deploy API with 3+ replicas for high availability
3. Enable production-grade caching (already implemented)
4. Metrics will scale to lakhs of users

## Production Features Already Implemented

Your current implementation includes:

✅ Multi-layer caching (Memory 5s → Redis 15s)
✅ Rate limiting (100 req/min per user, 1000 req/min per database)
✅ Request coalescing (prevents thundering herd)
✅ Circuit breaker (fail fast when backend is down)
✅ Graceful degradation (returns stale data on failures)

These features will activate automatically once the API can reach the metrics source (either stats service or Prometheus).

## Next Steps

Choose one of the options above based on your environment:

- **Local development**: Use Option 1 (deploy API in cluster)
- **Production deployment**: Use Option 3 (deploy Prometheus)

Need help? Check the logs:
```bash
# API logs
tail -f /tmp/app.log | grep metrics

# Check if stats service is accessible
kubectl get svc -n default | grep stats
kubectl describe svc pans-demo-demo-stats -n default
```
