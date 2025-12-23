#!/bin/bash

# Restore Database from Backup
# Usage: ./restore_example.sh <database_id> <backup_id>

API_BASE="http://localhost:8000/api/v1"
DOMAIN="demo"
PROJECT="demo"

# Example: Restore south-az3 database from backup 20251222-182047
DATABASE_ID="db-xxx"  # Replace with actual database ID
BACKUP_ID="20251222-182047"  # Or full job name: south-az3-demo-demo-backup-20251222-182047

echo "🔄 Restoring database $DATABASE_ID from backup $BACKUP_ID..."

curl -X POST \
  "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DATABASE_ID}/restore?backup_id=${BACKUP_ID}" \
  -H "Content-Type: application/json" \
  -v

echo ""
echo "✅ Restore job initiated! Check status with:"
echo "kubectl get jobs -l database=south-az3-demo-demo,restore-type=direct"

