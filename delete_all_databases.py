#!/usr/bin/env python3
"""
Script to delete all databases and related data from MongoDB.
WARNING: This will delete ALL databases from the system!
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.repositories.models import Database, Provider, AuditLog
from app.config.settings import settings
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from app.config.logging import get_logger

logger = get_logger(__name__)


async def delete_all_databases():
    """Delete all databases from MongoDB."""
    try:
        # Connect to MongoDB
        client = AsyncIOMotorClient(settings.mongodb_url)
        database = client[settings.mongodb_database]
        
        # Initialize Beanie
        await init_beanie(
            database=database,
            document_models=[Database, Provider, AuditLog]
        )
        
        # Count databases before deletion
        db_count = await Database.count()
        logger.info(f"Found {db_count} databases to delete")
        
        if db_count == 0:
            print("✅ No databases found to delete")
            return
        
        # Delete all databases
        result = await Database.delete_all()
        deleted_count = result.deleted_count if hasattr(result, 'deleted_count') else db_count
        
        print(f"✅ Deleted {deleted_count} databases from MongoDB")
        logger.info(f"Deleted {deleted_count} databases")
        
        # Also delete audit logs related to databases (optional)
        audit_count = await AuditLog.find({"resource_type": "database"}).count()
        if audit_count > 0:
            audit_result = await AuditLog.find({"resource_type": "database"}).delete()
            deleted_audits = audit_result.deleted_count if hasattr(audit_result, 'deleted_count') else audit_count
            print(f"✅ Deleted {deleted_audits} database-related audit logs")
            logger.info(f"Deleted {deleted_audits} audit logs")
        
        # Close connection
        client.close()
        
        print("\n✅ All databases deleted successfully!")
        print("⚠️  Note: Backup Jobs in Kubernetes are not deleted by this script.")
        print("   They will be cleaned up automatically after TTL expires (1 hour).")
        
    except Exception as e:
        logger.error(f"Failed to delete databases: {str(e)}", exc_info=True)
        print(f"❌ Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    print("🗑️  Deleting all databases from MongoDB...")
    print("⚠️  WARNING: This will delete ALL databases!")
    
    # Confirm
    confirm = input("Type 'DELETE ALL' to confirm: ")
    if confirm != "DELETE ALL":
        print("❌ Cancelled. Nothing was deleted.")
        sys.exit(0)
    
    asyncio.run(delete_all_databases())

