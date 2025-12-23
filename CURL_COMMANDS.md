# Database Creation & Backup - CURL Commands

## Quick Commands

### 1. Create Database
```bash
curl -X POST "http://localhost:8000/api/v1/domain/demo/project/demo/databases/" \
  -H "Content-Type: application/json" \
  -H "X-Region: south-az1" \
  -H "X-Availability-Zone: south-az1" \
  -d '{
    "name": "test-backup-db",
    "engine": "mongodb",
    "version": "8.0.4",
    "size": "db.t3.micro",
    "storage_gb": 10,
    "replicas": 1,
    "backup_enabled": true,
    "backup_schedule": "daily",
    "backup_retention_days": 30,
    "high_availability": false,
    "monitoring_enabled": true
  }'
```

**Response mein Database ID milega:**
```json
{
  "id": "db-xxxxx",
  "name": "test-backup-db",
  "status": "pending"
}
```

### 2. Check Database Status
```bash
# Replace DB_ID with actual database ID from step 1
DB_ID="db-xxxxx"

curl "http://localhost:8000/api/v1/domain/demo/project/demo/databases/${DB_ID}"
```

### 3. Trigger Backup (Jab database running ho)
```bash
DB_ID="db-xxxxx"

curl -X POST "http://localhost:8000/api/v1/domain/demo/project/demo/databases/${DB_ID}/backup"
```

**Response:**
```json
{
  "message": "Backup initiated",
  "job_id": "test-backup-db-demo-demo-backup-20241222-071500",
  "status": "initiated"
}
```

### 4. List All Backups
```bash
DB_ID="db-xxxxx"

curl "http://localhost:8000/api/v1/domain/demo/project/demo/databases/${DB_ID}/backups"
```

### 5. Get Backup Status
```bash
DB_ID="db-xxxxx"
BACKUP_JOB_ID="test-backup-db-demo-demo-backup-20241222-071500"

curl "http://localhost:8000/api/v1/domain/demo/project/demo/databases/${DB_ID}/backups/${BACKUP_JOB_ID}"
```

### 6. Restore from Backup
```bash
DB_ID="db-xxxxx"
BACKUP_ID="test-backup-db-demo-demo-backup-20241222-071500"

curl -X POST "http://localhost:8000/api/v1/domain/demo/project/demo/databases/${DB_ID}/restore?backup_id=${BACKUP_ID}"
```

---

## Complete Workflow (One-liner Script)

```bash
#!/bin/bash
API_BASE="http://localhost:8000/api/v1"
DOMAIN="demo"
PROJECT="demo"

# 1. Create Database
echo "Creating database..."
DB_RESPONSE=$(curl -s -X POST "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/" \
  -H "Content-Type: application/json" \
  -H "X-Region: south-az1" \
  -H "X-Availability-Zone: south-az1" \
  -d '{
    "name": "test-backup-db",
    "engine": "mongodb",
    "version": "8.0.4",
    "size": "db.t3.micro",
    "storage_gb": 10,
    "replicas": 1,
    "backup_enabled": true,
    "backup_schedule": "daily"
  }')

DB_ID=$(echo $DB_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
echo "Database ID: $DB_ID"

# 2. Wait 30 seconds
echo "Waiting 30 seconds..."
sleep 30

# 3. Check status
echo "Checking status..."
curl -s "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}" | python3 -m json.tool

# 4. Trigger backup (if running)
echo "Triggering backup..."
curl -X POST "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backup"

# 5. List backups
echo "Listing backups..."
curl -s "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backups" | python3 -m json.tool
```

---

## Alternative: Use the Script

```bash
bash curl_commands.sh
```


