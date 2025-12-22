# Makefile Usage Guide

This guide explains how to use the Makefile to run the KubeDB DBaaS platform locally.

## Quick Start

```bash
# Start all services (API + Reconciler + Operation Worker)
make run-all

# Check status
make status

# View logs
make logs

# Stop all services
make stop-all
```

## Configuration

You can customize the configuration using environment variables:

```bash
# Set custom reconciliation interval (default: 30 seconds)
export RECONCILE_INTERVAL=60  # Run every 60 seconds

# Set custom kubeconfig path
export KUBECONFIG=/path/to/your/kubeconfig.yaml

# Then start services
make run-all
```

Or override directly in the make command:

```bash
make run-all RECONCILE_INTERVAL=10 KUBECONFIG=/path/to/kubeconfig.yaml
```

## Individual Services

Run services individually:

```bash
# Run only the API
make run-api

# Run only the reconciliation worker
make run-reconciler

# Run only the operation worker
make run-worker
```

## Monitoring

### Check Service Status

```bash
make status
```

Output example:
```
Service Status:

✓ API                Running (PID: 12345)
✓ Reconciler         Running (PID: 12346)
✓ Operation Worker   Running (PID: 12347)
```

### View Logs

```bash
# Tail all logs
make logs

# Tail specific service logs
make logs-api
make logs-reconciler
make logs-worker
```

### Log Files

Logs are stored in `.logs/` directory:
- `.logs/api.log` - API server logs
- `.logs/reconciler.log` - Reconciliation worker logs
- `.logs/worker.log` - Operation worker logs

## Process Management

### Restart Services

```bash
# Restart all services
make restart-all

# Or restart individually
make stop-all
make run-api
make run-reconciler
make run-worker
```

### Stop Services

```bash
# Stop all services
make stop-all
```

PID files are stored in `.pids/` directory:
- `.pids/api.pid`
- `.pids/reconciler.pid`
- `.pids/worker.pid`

## Cleanup

```bash
# Clean log files
make clean-logs

# Clean PID files
make clean-pids

# Clean everything (code + logs + PIDs)
make clean-all
```

## Architecture Overview

The platform runs three main components:

### 1. API Server
- **Port:** 8000 (default)
- **Role:** Handles HTTP requests, validates input, updates desired state
- **Does NOT:** Create operations or patch KubeDB resources directly

### 2. Reconciliation Worker
- **Interval:** 30 seconds (default, configurable via `RECONCILE_INTERVAL`)
- **Role:**
  - Fetches current state from KubeDB CRs
  - Compares desired state vs current state
  - Creates operations when drift detected
  - Prevents duplicate operations

### 3. Operation Worker
- **Role:**
  - Processes operations from Redis queue
  - Creates KubeDB OpsRequests
  - Monitors OpsRequest progress
  - Updates current state after completion

## Workflow Example

```bash
# 1. Start all services
make run-all

# 2. Check that everything is running
make status

# 3. (In another terminal) Make a change via API
curl -X PATCH http://localhost:8000/api/v1/databases/db-123 \
  -H "Content-Type: application/json" \
  -d '{"size": "db.t3.large"}'

# 4. Watch the reconciler detect drift and create operation
make logs-reconciler

# 5. Watch the worker process the operation
make logs-worker

# 6. Stop when done
make stop-all
```

## Troubleshooting

### Services Not Starting

Check if ports are already in use:
```bash
# Check if port 8000 is in use
lsof -i :8000

# Kill the process if needed
kill -9 <PID>
```

### Reconciler Not Detecting Drift

1. Check reconciler is running:
   ```bash
   make status
   ```

2. Check reconciliation interval:
   ```bash
   # Default is 30 seconds
   # Reduce for faster testing
   make stop-all
   make run-all RECONCILE_INTERVAL=10
   ```

3. Check logs for errors:
   ```bash
   make logs-reconciler
   ```

### Operations Not Processing

1. Check operation worker is running:
   ```bash
   make status
   ```

2. Check worker logs:
   ```bash
   make logs-worker
   ```

3. Check Redis is running:
   ```bash
   redis-cli ping
   # Should return: PONG
   ```

### Duplicate Operations Warning

This is **expected behavior**! The reconciler uses deduplication to prevent creating multiple operations for the same database/operation type combination. You'll see:

```
[warning] duplicate_operation_rejected dedup_key=db-123:scale_vertical
```

This means there's already a pending operation for this database.

## Environment Variables

All available configuration options:

| Variable | Default | Description |
|----------|---------|-------------|
| `KUBECONFIG` | `/Users/coredge.io/Downloads/airtel-k8s-do-not-delete-kubeconfig.yaml` | Path to kubeconfig file |
| `RECONCILE_INTERVAL` | `30` | Reconciliation interval in seconds (10-300) |
| `MONGODB_URL` | `mongodb://localhost:27017/kubedb_dbaas` | MongoDB connection URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |

See `.env.example` for all available environment variables.

## Development vs Production

### Development (Local)

```bash
# Use Makefile for local development
make run-all

# Fast reconciliation for testing
make run-all RECONCILE_INTERVAL=10
```

### Production (Docker/Kubernetes)

```bash
# Use docker-compose for production-like setup
make docker-up

# Or deploy to Kubernetes
make k8s-deploy
```

## Additional Commands

See all available commands:
```bash
make help
```

Common commands:
- `make test` - Run tests
- `make lint` - Run linters
- `make format` - Format code
- `make docker-build` - Build Docker image
- `make docker-up` - Start with Docker Compose
- `make k8s-deploy` - Deploy to Kubernetes
