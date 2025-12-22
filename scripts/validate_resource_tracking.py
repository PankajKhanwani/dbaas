"""
Validate Resource Tracking Accuracy

This script checks if provider resource tracking is accurate by comparing
tracked allocations with actual database resource usage.

Usage:
    python scripts/validate_resource_tracking.py
"""
import asyncio
import sys
from typing import List, Dict, Any

# Add parent directory to path
sys.path.insert(0, ".")

from app.config.database import Database as DatabaseConnection
from app.repositories.models import Database, Provider
from app.config.logging import get_logger

logger = get_logger(__name__)


async def validate_resources() -> Dict[str, Any]:
    """
    Check if resource tracking is accurate across all providers.

    Returns:
        Dictionary with validation results
    """
    # Initialize database connection
    document_models = [Database, Provider]
    await DatabaseConnection.connect_db(document_models)

    providers = await Provider.find_all().to_list()

    if not providers:
        print("⚠️  No providers found in database")
        return {"status": "warning", "providers": []}

    print(f"\n{'='*80}")
    print(f"Resource Tracking Validation Report")
    print(f"{'='*80}\n")

    results = {
        "status": "ok",
        "providers": [],
        "total_mismatches": 0
    }

    for provider in providers:
        # Get all databases for this provider
        databases = await Database.find({"provider_id": provider.id}).to_list()

        # Calculate actual allocation from databases
        actual_cpu = sum(db.allocated_cpu_cores or 0 for db in databases)
        actual_memory = sum(db.allocated_memory_gb or 0 for db in databases)
        actual_storage = sum(db.allocated_storage_gb or 0 for db in databases)

        # Calculate differences
        cpu_diff = abs(provider.cpu_allocated_cores - actual_cpu)
        memory_diff = abs(provider.memory_allocated_gb - actual_memory)
        storage_diff = abs(provider.storage_allocated_gb - actual_storage)

        # Determine status
        has_mismatch = cpu_diff > 0.1 or memory_diff > 0.1 or storage_diff > 0.1

        provider_result = {
            "id": provider.id,
            "name": provider.name,
            "database_count": len(databases),
            "cpu": {
                "tracked": round(provider.cpu_allocated_cores, 2),
                "actual": round(actual_cpu, 2),
                "diff": round(cpu_diff, 2),
                "mismatch": cpu_diff > 0.1
            },
            "memory": {
                "tracked": round(provider.memory_allocated_gb, 2),
                "actual": round(actual_memory, 2),
                "diff": round(memory_diff, 2),
                "mismatch": memory_diff > 0.1
            },
            "storage": {
                "tracked": round(provider.storage_allocated_gb, 2),
                "actual": round(actual_storage, 2),
                "diff": round(storage_diff, 2),
                "mismatch": storage_diff > 0.1
            },
            "status": "mismatch" if has_mismatch else "ok"
        }

        results["providers"].append(provider_result)

        # Print provider summary
        print(f"Provider: {provider.name} ({provider.id})")
        print(f"  Status: {'✅' if not has_mismatch else '❌'} {provider_result['status'].upper()}")
        print(f"  Databases: {len(databases)}")
        print(f"  Active: {provider.is_active}, Maintenance: {provider.is_maintenance}")
        print(f"\n  Resources:")
        print(f"    CPU:     tracked={provider.cpu_allocated_cores:.2f} cores, "
              f"actual={actual_cpu:.2f} cores, diff={cpu_diff:.2f} {'⚠️' if cpu_diff > 0.1 else '✅'}")
        print(f"    Memory:  tracked={provider.memory_allocated_gb:.2f} GB, "
              f"actual={actual_memory:.2f} GB, diff={memory_diff:.2f} {'⚠️' if memory_diff > 0.1 else '✅'}")
        print(f"    Storage: tracked={provider.storage_allocated_gb:.2f} GB, "
              f"actual={actual_storage:.2f} GB, diff={storage_diff:.2f} {'⚠️' if storage_diff > 0.1 else '✅'}")

        # List databases if there's a mismatch
        if has_mismatch and databases:
            print(f"\n  Databases on this provider:")
            for db in databases:
                print(f"    - {db.name} ({db.id}): "
                      f"CPU={db.allocated_cpu_cores or 0:.2f}, "
                      f"Memory={db.allocated_memory_gb or 0:.2f} GB, "
                      f"Storage={db.allocated_storage_gb or 0:.2f} GB")

        print()

        if has_mismatch:
            results["total_mismatches"] += 1
            results["status"] = "error"

    # Print summary
    print(f"{'='*80}")
    print(f"Summary:")
    print(f"  Total Providers: {len(providers)}")
    print(f"  Providers with Mismatches: {results['total_mismatches']}")
    print(f"  Overall Status: {results['status'].upper()}")
    print(f"{'='*80}\n")

    return results


async def fix_mismatches():
    """
    Automatically fix any resource tracking mismatches found.

    CAUTION: This will update provider allocation counters to match actual usage.
    """
    document_models = [Database, Provider]
    await DatabaseConnection.connect_db(document_models)

    print("\n⚠️  FIX MODE: This will update provider allocations to match actual usage")
    print("Validating resources first...\n")

    results = await validate_resources()

    if results["status"] != "error":
        print("✅ No mismatches found. Nothing to fix.")
        return

    print(f"\n❗ Found {results['total_mismatches']} provider(s) with mismatches")
    response = input("Do you want to fix these mismatches? (yes/no): ")

    if response.lower() != "yes":
        print("Fix cancelled.")
        return

    fixed_count = 0
    for provider_result in results["providers"]:
        if provider_result["status"] == "mismatch":
            provider_id = provider_result["id"]
            provider = await Provider.get(provider_id)

            if provider:
                print(f"\nFixing provider: {provider.name}")

                # Update to actual values
                actual_cpu = provider_result["cpu"]["actual"]
                actual_memory = provider_result["memory"]["actual"]
                actual_storage = provider_result["storage"]["actual"]

                await Provider.find_one({"_id": provider.id}).update({
                    "$set": {
                        "cpu_allocated_cores": actual_cpu,
                        "memory_allocated_gb": actual_memory,
                        "storage_allocated_gb": actual_storage,
                    }
                })

                print(f"  ✅ Updated allocations:")
                print(f"     CPU: {provider_result['cpu']['tracked']} → {actual_cpu}")
                print(f"     Memory: {provider_result['memory']['tracked']} → {actual_memory}")
                print(f"     Storage: {provider_result['storage']['tracked']} → {actual_storage}")

                fixed_count += 1

    print(f"\n✅ Fixed {fixed_count} provider(s)")
    print("\nRunning validation again to confirm...")
    await validate_resources()


async def main():
    """Main entry point"""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--fix":
        await fix_mismatches()
    else:
        results = await validate_resources()

        # Exit with error code if mismatches found
        if results["status"] == "error":
            sys.exit(1)
        else:
            sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nValidation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error("validation_error", error=str(e), exc_info=True)
        print(f"\n❌ Validation failed with error: {e}")
        sys.exit(1)
