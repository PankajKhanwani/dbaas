#!/bin/bash
# Database Creation and Backup - CURL Commands
# Usage: bash curl_commands.sh

API_BASE="http://localhost:8000/api/v1"
DOMAIN="demo"
PROJECT="demo"

echo "=========================================="
echo "Database Creation & Backup - CURL Commands"
echo "=========================================="
echo ""

# Step 1: Create Database
echo "🚀 Step 1: Creating Database..."
DB_RESPONSE=$(curl -s -X POST "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/" \
  -H "Content-Type: application/json" \
  -H "X-Region: south-az1" \
  -H "X-Availability-Zone: south-az1" \
  -d '{
    "name": "test-backup-db-curl",
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
  }')

echo "$DB_RESPONSE" | python3 -m json.tool

# Extract database ID
DB_ID=$(echo "$DB_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('id', ''))" 2>/dev/null)

if [ -z "$DB_ID" ]; then
    echo "❌ Failed to get database ID"
    exit 1
fi

echo ""
echo "✅ Database created! ID: $DB_ID"
echo ""

# Step 2: Wait and check status
echo "⏳ Waiting 30 seconds for database to provision..."
sleep 30

echo ""
echo "🔍 Checking database status..."
STATUS_RESPONSE=$(curl -s "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}")
echo "$STATUS_RESPONSE" | python3 -m json.tool | grep -E "(id|name|status|ready_replicas)" | head -5

DB_STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', ''))" 2>/dev/null)

echo ""
echo "Current Status: $DB_STATUS"
echo ""

# Step 3: Trigger Backup (if running)
if [ "$DB_STATUS" = "running" ]; then
    echo "🚀 Step 2: Triggering Backup..."
    BACKUP_RESPONSE=$(curl -s -X POST "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backup")
    echo "$BACKUP_RESPONSE" | python3 -m json.tool
    
    BACKUP_JOB_ID=$(echo "$BACKUP_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('job_id', ''))" 2>/dev/null)
    
    if [ -n "$BACKUP_JOB_ID" ]; then
        echo ""
        echo "✅ Backup triggered! Job ID: $BACKUP_JOB_ID"
        echo ""
        
        # Wait a bit
        echo "⏳ Waiting 15 seconds for backup to start..."
        sleep 15
        
        # Step 4: List Backups
        echo ""
        echo "📋 Step 3: Listing Backups..."
        BACKUPS_RESPONSE=$(curl -s "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backups")
        echo "$BACKUPS_RESPONSE" | python3 -m json.tool
        
        # Step 5: Get Backup Status
        if [ -n "$BACKUP_JOB_ID" ]; then
            echo ""
            echo "🔍 Step 4: Getting Backup Status..."
            BACKUP_STATUS_RESPONSE=$(curl -s "${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backups/${BACKUP_JOB_ID}")
            echo "$BACKUP_STATUS_RESPONSE" | python3 -m json.tool
        fi
    fi
else
    echo "⚠️  Database status is '$DB_STATUS' (not running yet)"
    echo "   Backup will be triggered automatically when database is ready"
    echo ""
    echo "   To trigger backup manually later, run:"
    echo "   curl -X POST \"${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backup\""
fi

echo ""
echo "=========================================="
echo "✅ Done!"
echo "=========================================="
echo ""
echo "📋 Summary:"
echo "   Database ID: $DB_ID"
echo "   Domain: $DOMAIN"
echo "   Project: $PROJECT"
echo ""
echo "🔍 Useful Commands:"
echo "   # Check database status:"
echo "   curl \"${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}\" | python3 -m json.tool"
echo ""
echo "   # List backups:"
echo "   curl \"${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backups\" | python3 -m json.tool"
echo ""
echo "   # Trigger backup:"
echo "   curl -X POST \"${API_BASE}/domain/${DOMAIN}/project/${PROJECT}/databases/${DB_ID}/backup\""
echo ""


