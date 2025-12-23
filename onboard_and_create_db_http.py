#!/usr/bin/env python3
"""
Script to onboard a provider and create a database with backup using HTTP API.

Usage:
    python3 onboard_and_create_db_http.py
"""
import base64
import json
import time
import requests

API_BASE = "http://localhost:8000/api/v1"


def read_kubeconfig(path):
    """Read and encode kubeconfig file."""
    with open(path, 'r') as f:
        content = f.read()
    return base64.b64encode(content.encode()).decode()


def onboard_provider():
    """Onboard a provider."""
    print("\n🚀 Step 1: Onboarding Provider...")
    
    kubeconfig_path = "/Users/vickyupadhyay/Downloads/southaz1-21nov-kubeconfig.yaml"
    print(f"📖 Reading kubeconfig from: {kubeconfig_path}")
    
    kubeconfig_base64 = read_kubeconfig(kubeconfig_path)
    print("✅ Kubeconfig read and encoded")
    
    provider_data = {
        "name": "southaz1-cluster",
        "region": "south-az1",
        "availability_zone": "south-az1",
        "cloud_provider": "airtel",
        "cpu_total_cores": 100.0,
        "memory_total_gb": 200.0,
        "storage_total_gb": 1000.0,
        "cpu_reservation_percent": 80,
        "memory_reservation_percent": 80,
        "storage_reservation_percent": 80,
        "kubeconfig_content": kubeconfig_base64,
        "priority": 100,
        "is_active": True,
        "verify_ssl": False,  # Disable SSL verification for Airtel cluster
    }
    
    try:
        response = requests.post(
            f"{API_BASE}/providers/",
            json=provider_data,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        
        if response.status_code == 201:
            provider = response.json()
            print(f"✅ Provider onboarded successfully!")
            print(f"   Provider ID: {provider.get('id')}")
            print(f"   Name: {provider.get('name')}")
            return provider.get('id')
        elif response.status_code == 409 or "already exists" in response.text.lower():
            print("⚠️  Provider already exists, fetching existing...")
            # List providers to find existing one
            list_response = requests.get(f"{API_BASE}/providers/")
            if list_response.status_code == 200:
                providers = list_response.json().get('providers', [])
                for p in providers:
                    if p.get('name') == provider_data['name']:
                        print(f"✅ Using existing provider ID: {p.get('id')}")
                        return p.get('id')
            print("❌ Could not find existing provider")
            return None
        else:
            print(f"❌ Failed to onboard provider: {response.status_code}")
            print(f"   Response: {response.text}")
            return None
    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to API. Is the server running on http://localhost:8000?")
        return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def create_database(provider_id=None):
    """Create a database."""
    print("\n🚀 Step 2: Creating Database...")
    
    domain = "demo"
    project = "demo"
    
    db_data = {
        "name": "test-db-backup",
        "engine": "mongodb",
        "version": "8.0.4",
        "size": "db.t3.small",
        "storage_gb": 10,
        "replicas": 1,
        "backup_enabled": True,
        "backup_schedule": "daily",
        "backup_retention_days": 30,
        "high_availability": False,
        "monitoring_enabled": True,
    }
    
    headers = {
        "Content-Type": "application/json",
        "X-Region": "south-az1",
        "X-Availability-Zone": "south-az1",
    }
    
    try:
        response = requests.post(
            f"{API_BASE}/domain/{domain}/project/{project}/databases/",
            json=db_data,
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 202:
            db = response.json()
            print(f"✅ Database creation initiated!")
            print(f"   Database ID: {db.get('id')}")
            print(f"   Name: {db.get('name')}")
            print(f"   Status: {db.get('status')}")
            return db.get('id')
        else:
            print(f"❌ Failed to create database: {response.status_code}")
            print(f"   Response: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def trigger_backup(database_id):
    """Trigger a backup."""
    print("\n🚀 Step 3: Triggering Backup...")
    
    domain = "demo"
    project = "demo"
    
    try:
        response = requests.post(
            f"{API_BASE}/domain/{domain}/project/{project}/databases/{database_id}/backup",
            timeout=30
        )
        
        if response.status_code == 202:
            result = response.json()
            print(f"✅ Backup triggered successfully!")
            print(f"   Job ID: {result.get('job_id')}")
            print(f"   Status: {result.get('status')}")
            print(f"   Message: {result.get('message')}")
            return result.get('job_id')
        else:
            print(f"❌ Failed to trigger backup: {response.status_code}")
            print(f"   Response: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def list_backups(database_id):
    """List backups."""
    print("\n🚀 Step 4: Listing Backups...")
    
    domain = "demo"
    project = "demo"
    
    try:
        response = requests.get(
            f"{API_BASE}/domain/{domain}/project/{project}/databases/{database_id}/backups",
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            backups = data.get('backups', [])
            print(f"✅ Found {len(backups)} backup(s)")
            for backup in backups[:5]:  # Show first 5
                print(f"   - Backup ID: {backup.get('backup_id')}")
                print(f"     Phase: {backup.get('phase')}")
                print(f"     Created: {backup.get('created_at')}")
            return backups
        else:
            print(f"⚠️  Could not list backups: {response.status_code}")
            print(f"   Response: {response.text}")
            return []
    except Exception as e:
        print(f"⚠️  Error listing backups: {e}")
        return []


def main():
    """Main function."""
    print("=" * 60)
    print("Provider Onboarding & Database Creation Script (HTTP API)")
    print("=" * 60)
    
    # Check if API is running
    try:
        health_response = requests.get(f"{API_BASE.replace('/api/v1', '')}/health", timeout=5)
        if health_response.status_code != 200:
            print("⚠️  API health check failed. Make sure server is running.")
    except:
        print("⚠️  Could not connect to API. Make sure server is running on http://localhost:8000")
        print("   Start server with: uvicorn app.main:app --reload")
        return
    
    # Step 1: Onboard Provider
    provider_id = onboard_provider()
    if not provider_id:
        print("\n❌ Failed to onboard provider. Exiting.")
        return
    
    # Step 2: Create Database
    database_id = create_database(provider_id)
    if not database_id:
        print("\n❌ Failed to create database. Exiting.")
        return
    
    # Step 3: Wait a bit
    print("\n⏳ Waiting 30 seconds for database to provision...")
    time.sleep(30)
    
    # Step 4: Trigger Backup
    backup_job_id = trigger_backup(database_id)
    
    # Step 5: List Backups
    if backup_job_id:
        time.sleep(5)  # Wait a bit for backup to start
    list_backups(database_id)
    
    print("\n✅ All operations completed!")
    print(f"\n📋 Summary:")
    print(f"   Provider ID: {provider_id}")
    print(f"   Database ID: {database_id}")
    print(f"   Domain: demo")
    print(f"   Project: demo")
    if backup_job_id:
        print(f"   Backup Job ID: {backup_job_id}")
    print(f"\n🔍 Check database status:")
    print(f"   GET {API_BASE}/domain/demo/project/demo/databases/{database_id}")
    print(f"\n🔍 List backups:")
    print(f"   GET {API_BASE}/domain/demo/project/demo/databases/{database_id}/backups")


if __name__ == "__main__":
    main()

