#!/usr/bin/env python3
"""
Script to onboard a provider and create a database with backup.

Usage:
    python3 onboard_and_create_db.py
"""
import asyncio
import base64
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.provider_service import provider_service
from app.services.database_service import database_service
from app.schemas.provider import ProviderCreate
from app.models.database import DatabaseCreateRequest, DatabaseEngine, DatabaseSize, BackupSchedule
from app.config.settings import settings


async def main():
    """Main function to onboard provider and create database."""
    
    # Step 1: Read kubeconfig file
    kubeconfig_path = "/Users/vickyupadhyay/Downloads/southaz1-21nov-kubeconfig.yaml"
    print(f"📖 Reading kubeconfig from: {kubeconfig_path}")
    
    with open(kubeconfig_path, 'r') as f:
        kubeconfig_content = f.read()
    
    # Encode to base64
    kubeconfig_base64 = base64.b64encode(kubeconfig_content.encode()).decode()
    print("✅ Kubeconfig read and encoded")
    
    # Step 2: Onboard Provider
    print("\n🚀 Step 1: Onboarding Provider...")
    
    provider_data = ProviderCreate(
        name="southaz1-cluster",
        region="south-az1",
        availability_zone="south-az1",
        cloud_provider="airtel",
        cpu_total_cores=100.0,
        memory_total_gb=200.0,
        storage_total_gb=1000.0,
        cpu_reservation_percent=80,
        memory_reservation_percent=80,
        storage_reservation_percent=80,
        kubeconfig_content=kubeconfig_base64,
        kubeconfig_path=None,
        priority=100,
        is_active=True,
        verify_ssl=True,
    )
    
    try:
        provider = await provider_service.create_provider(provider_data)
        print(f"✅ Provider onboarded successfully!")
        print(f"   Provider ID: {provider.id}")
        print(f"   Name: {provider.name}")
        print(f"   Region: {provider.region}")
        provider_id = provider.id
    except Exception as e:
        print(f"❌ Failed to onboard provider: {e}")
        # Check if provider already exists
        if "already exists" in str(e).lower():
            print("   Provider already exists, using existing provider...")
            # Try to find existing provider
            from app.repositories.models import Provider
            existing = await Provider.find_one(Provider.name == provider_data.name)
            if existing:
                provider_id = existing.id
                print(f"   Using existing provider ID: {provider_id}")
            else:
                print("   Could not find existing provider. Exiting.")
                return
        else:
            return
    
    # Step 3: Create Database
    print("\n🚀 Step 2: Creating Database...")
    
    db_request = DatabaseCreateRequest(
        name="test-db-backup",
        engine=DatabaseEngine.MONGODB,
        version="8.0.4",
        size=DatabaseSize.SMALL,  # 1 CPU, 2GB RAM
        storage_gb=10,
        replicas=1,
        backup_enabled=True,
        backup_schedule=BackupSchedule.DAILY,
        backup_retention_days=30,
        high_availability=False,
        monitoring_enabled=True,
    )
    
    domain = "demo"
    project = "demo"
    
    # Headers for provider selection (will select our provider)
    headers = {
        "x-region": "south-az1",
        "x-availability-zone": "south-az1",
    }
    
    try:
        db_response = await database_service.create_database(
            db_request=db_request,
            domain=domain,
            project=project,
            headers=headers,
        )
        print(f"✅ Database creation initiated!")
        print(f"   Database ID: {db_response.id}")
        print(f"   Name: {db_response.name}")
        print(f"   Status: {db_response.status}")
        print(f"   Provider ID: {db_response.provider_id}")
        database_id = db_response.id
    except Exception as e:
        print(f"❌ Failed to create database: {e}")
        return
    
    # Step 4: Wait a bit for database to be ready (optional)
    print("\n⏳ Waiting 30 seconds for database to provision...")
    await asyncio.sleep(30)
    
    # Step 5: Trigger Backup
    print("\n🚀 Step 3: Triggering Backup...")
    
    try:
        backup_result = await database_service.trigger_backup(
            database_id=database_id,
            domain=domain,
            project=project,
        )
        print(f"✅ Backup triggered successfully!")
        print(f"   Job ID: {backup_result.get('job_id')}")
        print(f"   Status: {backup_result.get('status')}")
        print(f"   Message: {backup_result.get('message')}")
    except Exception as e:
        print(f"❌ Failed to trigger backup: {e}")
        print(f"   Note: Backup will be automatically configured when database is ready")
        return
    
    # Step 6: List Backups
    print("\n🚀 Step 4: Listing Backups...")
    
    try:
        backups = await database_service.list_backups(
            database_id=database_id,
            domain=domain,
            project=project,
        )
        print(f"✅ Found {len(backups)} backup(s)")
        for backup in backups[:5]:  # Show first 5
            print(f"   - Backup ID: {backup.get('backup_id')}")
            print(f"     Phase: {backup.get('phase')}")
            print(f"     Created: {backup.get('created_at')}")
    except Exception as e:
        print(f"⚠️  Could not list backups yet: {e}")
        print(f"   Backups may still be in progress")
    
    print("\n✅ All operations completed!")
    print(f"\n📋 Summary:")
    print(f"   Provider ID: {provider_id}")
    print(f"   Database ID: {database_id}")
    print(f"   Domain: {domain}")
    print(f"   Project: {project}")
    print(f"\n🔍 Check database status:")
    print(f"   GET /api/v1/domain/{domain}/project/{project}/databases/{database_id}")
    print(f"\n🔍 List backups:")
    print(f"   GET /api/v1/domain/{domain}/project/{project}/databases/{database_id}/backups")


if __name__ == "__main__":
    print("=" * 60)
    print("Provider Onboarding & Database Creation Script")
    print("=" * 60)
    asyncio.run(main())


