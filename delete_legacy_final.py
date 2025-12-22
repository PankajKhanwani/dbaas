#!/usr/bin/env python3
from pymongo import MongoClient

client = MongoClient('mongodb://localhost:27017')
db = client['kubedb_dbaas']

print("All databases:")
for doc in db.databases.find({}):
    provider_id = doc.get('provider_id')
    print(f"  - {doc.get('id')}: provider_id={provider_id}, name={doc.get('name')}")

# Find legacy databases
legacy = list(db.databases.find({
    "$or": [
        {"provider_id": {"$exists": False}},
        {"provider_id": None}
    ]
}))

if not legacy:
    print("\n✅ No legacy databases found. All databases have provider_id.")
else:
    print(f"\n⚠️  Found {len(legacy)} legacy database(s):")
    for doc in legacy:
        print(f"  - {doc.get('id')}: {doc.get('name')}")

    # Delete them
    result = db.databases.delete_many({
        "$or": [
            {"provider_id": {"$exists": False}},
            {"provider_id": None}
        ]
    })
    print(f"✅ Deleted {result.deleted_count} database(s) without provider_id")

client.close()
