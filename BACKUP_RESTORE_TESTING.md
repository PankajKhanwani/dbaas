# Backup & Restore Testing Guide

Complete step-by-step guide to test backup and restore functionality.

## 📋 Prerequisites

1. **Kubernetes Cluster** with KubeDB operator installed
2. **Stash/KubeStash Operator** installed in cluster
3. **S3 Bucket** created and accessible
4. **Application running** (local or in cluster)

## 🔧 Step 1: Environment Setup

### 1.1 Set S3 Bucket Name

Create or update `.env` file:

```bash
# Backup Configuration
BACKUP_ENABLED=true
BACKUP_S3_BUCKET=your-bucket-name  # ⚠️ IMPORTANT: Set your bucket name
BACKUP_S3_REGION=us-east-1
BACKUP_S3_ENDPOINT=https://south-az2-s3.cloud.airtel.in:10444
BACKUP_S3_ACCESS_KEY_ID=PIY82HBAY3KLGL60SE7A
BACKUP_S3_SECRET_ACCESS_KEY=XhjRuatClbAXoGw+yDb60Gl5bPNakE49qoP6N9kI
BACKUP_RETENTION_DAYS=30
STASH_ENABLED=true
```

### 1.2 Verify Stash Operator

```bash
# Check if Stash operator is installed
kubectl get pods -n stash-system

# Expected output:
# NAME                              READY   STATUS    RESTARTS   AGE
# stash-operator-xxxxx-xxxxx         1/1     Running   0          5m
```

If not installed, install it:

```bash
# Install Stash operator
helm repo add appscode https://charts.appscode.com/stable/
helm repo update
helm install stash appscode/stash \
  --namespace stash-system \
  --create-namespace \
  --version v2023.10.9
```

## 🚀 Step 2: Start Application

### Option A: Local Development

```bash
# Start MongoDB and Redis
docker-compose up -d mongodb redis

# Start application
make dev

# Or directly:
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Option B: Docker Compose

```bash
docker-compose up -d
```

### Option C: Kubernetes

```bash
kubectl apply -f k8s/app/deployment.yaml
```

## 🧪 Step 3: Test Backup & Restore Flow

### 3.1 Create a Test Database

```bash
# Set your API token (get from auth endpoint)
export API_TOKEN="your-jwt-token-here"
export DOMAIN="test-domain"
export PROJECT="test-project"

# Create a MongoDB database with backup enabled
curl -X POST "http://localhost:8000/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-backup-db",
    "engine": "mongodb",
    "version": "7.0.8",
    "size": "db.t3.small",
    "storage_gb": 10,
    "replicas": 1,
    "backup_enabled": true,
    "backup_schedule": "daily",
    "backup_retention_days": 7
  }'
```

**Expected Response:**
```json
{
  "id": "db-abc123",
  "name": "test-backup-db",
  "status": "provisioning",
  "backup_enabled": true,
  ...
}
```

**Note:** Wait for database to be `running` (check status endpoint)

### 3.2 Wait for Database to be Ready

```bash
# Check database status
curl -X GET "http://localhost:8000/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}"

# Wait until status is "running"
# This may take 2-5 minutes
```

### 3.3 Verify Backup Configuration Created

After database is ready, check if BackupConfiguration was automatically created:

```bash
# Get database resource name from response
export DB_RESOURCE_NAME="test-backup-db-test-domain-test-project"

# Check BackupConfiguration in Kubernetes
kubectl get backupconfiguration -n default | grep ${DB_RESOURCE_NAME}

# Check details
kubectl describe backupconfiguration ${DB_RESOURCE_NAME}-backup-config -n default
```

**Expected Output:**
```
Name:         test-backup-db-test-domain-test-project-backup-config
Schedule:     0 2 * * *
Retention:    keepDaily: 7
Backend:      s3://your-bucket-name/backups/mongodb/...
```

### 3.4 Trigger On-Demand Backup

```bash
# Trigger immediate backup
curl -X POST "http://localhost:8000/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/backup" \
  -H "Authorization: Bearer ${API_TOKEN}"

# Expected Response:
# {
#   "message": "Backup initiated",
#   "database_id": "db-abc123",
#   "job_id": "test-backup-db-test-domain-test-project-backup-20240115-143022",
#   "status": "initiated"
# }
```

**Save the `job_id` for later steps!**

### 3.5 Check Backup Status

```bash
# Replace BACKUP_JOB_ID with the job_id from previous step
export BACKUP_JOB_ID="test-backup-db-test-domain-test-project-backup-20240115-143022"

# Check backup status
curl -X GET "http://localhost:8000/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/backups/${BACKUP_JOB_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}"
```

**Expected Response:**
```json
{
  "backup_session_name": "test-backup-db-test-domain-test-project-backup-20240115-143022",
  "phase": "Succeeded",
  "status": {
    "phase": "Succeeded",
    "sessionDuration": "2m30s",
    "snapshot": "test-backup-db-test-domain-test-project-backup-20240115-143022"
  }
}
```

**Possible Phases:**
- `Pending` - Backup job created, waiting to start
- `Running` - Backup in progress
- `Succeeded` - Backup completed successfully
- `Failed` - Backup failed (check logs)

### 3.6 List All Backups

```bash
# List all backups for the database
curl -X GET "http://localhost:8000/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/backups" \
  -H "Authorization: Bearer ${API_TOKEN}"
```

**Expected Response:**
```json
{
  "database_id": "db-abc123",
  "backups": [
    {
      "backup_id": "test-backup-db-test-domain-test-project-backup-20240115-143022",
      "phase": "Succeeded",
      "created_at": "2024-01-15T14:30:22Z"
    }
  ],
  "count": 1
}
```

### 3.7 Verify Backup in S3

```bash
# Using AWS CLI (if configured)
aws s3 ls s3://your-bucket-name/backups/mongodb/test-backup-db-test-domain-test-project/ \
  --endpoint-url=https://south-az2-s3.cloud.airtel.in:10444

# Or using curl with S3 API
curl -X GET "https://south-az2-s3.cloud.airtel.in:10444/your-bucket-name/backups/mongodb/test-backup-db-test-domain-test-project/" \
  -H "Authorization: AWS PIY82HBAY3KLGL60SE7A:..."
```

### 3.8 Test Restore

**⚠️ WARNING:** Restore will overwrite existing data!

```bash
# Restore from backup
curl -X POST "http://localhost:8000/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/restore?backup_id=${BACKUP_JOB_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}"

# Expected Response:
# {
#   "message": "Restore initiated",
#   "database_id": "db-abc123",
#   "backup_id": "test-backup-db-test-domain-test-project-backup-20240115-143022",
#   "job_id": "test-backup-db-test-domain-test-project-restore-20240115-150000",
#   "status": "initiated"
# }
```

### 3.9 Check Restore Status

```bash
# Check restore session in Kubernetes
kubectl get restoresession -n default | grep ${DB_RESOURCE_NAME}

# Get details
kubectl describe restoresession ${DB_RESOURCE_NAME}-restore-* -n default
```

**Expected Phases:**
- `Pending` - Restore job created
- `Running` - Restore in progress
- `Succeeded` - Restore completed
- `Failed` - Restore failed

## 🔍 Step 4: Verify in Kubernetes

### 4.1 Check BackupSession

```bash
# List all backup sessions
kubectl get backupsession -n default

# Get details of specific backup
kubectl describe backupsession ${BACKUP_JOB_ID} -n default

# Check logs
kubectl logs -n default -l app=stash --tail=100
```

### 4.2 Check RestoreSession

```bash
# List all restore sessions
kubectl get restoresession -n default

# Get details
kubectl describe restoresession ${RESTORE_JOB_ID} -n default
```

### 4.3 Check Backup Storage Secret

```bash
# Verify secret was created
kubectl get secret backup-storage-secret -n default

# Check secret contents (base64 encoded)
kubectl get secret backup-storage-secret -n default -o yaml
```

## 🐛 Troubleshooting

### Issue 1: Backup Fails with "Bucket not configured"

**Solution:**
```bash
# Set bucket name in environment
export BACKUP_S3_BUCKET=your-bucket-name

# Or in .env file
echo "BACKUP_S3_BUCKET=your-bucket-name" >> .env
```

### Issue 2: Backup Fails with "Stash operator not found"

**Solution:**
```bash
# Install Stash operator
helm install stash appscode/stash \
  --namespace stash-system \
  --create-namespace
```

### Issue 3: Backup Stuck in "Pending" Phase

**Check:**
```bash
# Check if Stash operator is running
kubectl get pods -n stash-system

# Check BackupSession events
kubectl describe backupsession ${BACKUP_JOB_ID} -n default

# Check Stash operator logs
kubectl logs -n stash-system -l app=stash-operator --tail=100
```

### Issue 4: S3 Connection Failed

**Verify:**
```bash
# Test S3 connection manually
aws s3 ls s3://your-bucket-name/ \
  --endpoint-url=https://south-az2-s3.cloud.airtel.in:10444 \
  --region us-east-1

# Check credentials in secret
kubectl get secret backup-storage-secret -n default -o jsonpath='{.data}' | base64 -d
```

### Issue 5: Restore Fails

**Check:**
```bash
# Verify backup exists
kubectl get backupsession ${BACKUP_JOB_ID} -n default

# Check restore session logs
kubectl logs -n default -l app=stash --tail=200 | grep restore

# Verify database is in correct state for restore
kubectl get mongodb ${DB_RESOURCE_NAME} -n default
```

## 📊 Step 5: Automated Testing Script

Create a test script `test_backup_restore.sh`:

```bash
#!/bin/bash

set -e

API_URL="http://localhost:8000"
DOMAIN="test-domain"
PROJECT="test-project"
API_TOKEN="${API_TOKEN:-your-token-here}"

echo "🧪 Testing Backup & Restore Flow..."

# 1. Create database
echo "1. Creating database..."
DB_RESPONSE=$(curl -s -X POST "${API_URL}/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-backup-db",
    "engine": "mongodb",
    "version": "7.0.8",
    "size": "db.t3.small",
    "storage_gb": 10,
    "replicas": 1,
    "backup_enabled": true
  }')

DATABASE_ID=$(echo $DB_RESPONSE | jq -r '.id')
echo "✅ Database created: ${DATABASE_ID}"

# 2. Wait for database to be ready
echo "2. Waiting for database to be ready..."
for i in {1..30}; do
  STATUS=$(curl -s -X GET "${API_URL}/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}" \
    -H "Authorization: Bearer ${API_TOKEN}" | jq -r '.status')
  
  if [ "$STATUS" == "running" ]; then
    echo "✅ Database is ready"
    break
  fi
  
  echo "   Waiting... (${i}/30) Status: ${STATUS}"
  sleep 10
done

# 3. Trigger backup
echo "3. Triggering backup..."
BACKUP_RESPONSE=$(curl -s -X POST "${API_URL}/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/backup" \
  -H "Authorization: Bearer ${API_TOKEN}")

BACKUP_JOB_ID=$(echo $BACKUP_RESPONSE | jq -r '.job_id')
echo "✅ Backup triggered: ${BACKUP_JOB_ID}"

# 4. Wait for backup to complete
echo "4. Waiting for backup to complete..."
for i in {1..20}; do
  BACKUP_STATUS=$(curl -s -X GET "${API_URL}/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/backups/${BACKUP_JOB_ID}" \
    -H "Authorization: Bearer ${API_TOKEN}" | jq -r '.phase')
  
  if [ "$BACKUP_STATUS" == "Succeeded" ]; then
    echo "✅ Backup completed successfully"
    break
  elif [ "$BACKUP_STATUS" == "Failed" ]; then
    echo "❌ Backup failed"
    exit 1
  fi
  
  echo "   Waiting... (${i}/20) Status: ${BACKUP_STATUS}"
  sleep 15
done

# 5. List backups
echo "5. Listing backups..."
BACKUPS=$(curl -s -X GET "${API_URL}/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/backups" \
  -H "Authorization: Bearer ${API_TOKEN}")

BACKUP_COUNT=$(echo $BACKUPS | jq -r '.count')
echo "✅ Found ${BACKUP_COUNT} backup(s)"

# 6. Test restore (optional - uncomment to test)
# echo "6. Testing restore..."
# RESTORE_RESPONSE=$(curl -s -X POST "${API_URL}/api/v1/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/restore?backup_id=${BACKUP_JOB_ID}" \
#   -H "Authorization: Bearer ${API_TOKEN}")
# 
# RESTORE_JOB_ID=$(echo $RESTORE_RESPONSE | jq -r '.job_id')
# echo "✅ Restore triggered: ${RESTORE_JOB_ID}"

echo "🎉 All tests completed!"
```

**Run the script:**
```bash
chmod +x test_backup_restore.sh
export API_TOKEN="your-token"
./test_backup_restore.sh
```

## ✅ Success Criteria

Your backup/restore flow is working if:

1. ✅ Database creates successfully with `backup_enabled=true`
2. ✅ BackupConfiguration is automatically created in Kubernetes
3. ✅ On-demand backup completes with status "Succeeded"
4. ✅ Backup files appear in S3 bucket
5. ✅ List backups endpoint returns backup sessions
6. ✅ Restore completes successfully (optional test)

## 📝 Notes

- **Backup Time:** Depends on database size (typically 1-5 minutes for small DBs)
- **Restore Time:** Usually 2-3x backup time
- **Scheduled Backups:** Run automatically based on `backup_schedule` (daily at 2 AM by default)
- **Retention:** Old backups are automatically deleted based on `backup_retention_days`

## 🔗 Related Documentation

- [KubeDB Documentation](https://kubedb.com/docs)
- [Stash Documentation](https://stash.run/docs)
- [S3 API Documentation](https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html)

