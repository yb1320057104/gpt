from __future__ import annotations

import os
import sys
from typing import Any

from pymongo import MongoClient


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def copy_collection(source: Any, target: Any, name: str) -> tuple[int, int]:
    destination = target[name]
    destination.drop()
    batch: list[dict[str, Any]] = []
    copied = 0
    for document in source[name].find({}):
        batch.append(document)
        if len(batch) >= 500:
            destination.insert_many(batch, ordered=False)
            copied += len(batch)
            batch.clear()
    if batch:
        destination.insert_many(batch, ordered=False)
        copied += len(batch)

    for index in source[name].list_indexes():
        spec = dict(index)
        if spec.get("name") == "_id_":
            continue
        keys = list(spec.pop("key").items())
        spec.pop("v", None)
        spec.pop("ns", None)
        destination.create_index(keys, **spec)
    return copied, destination.count_documents({})


def main() -> int:
    source_uri = required_env("MIGRATION_SOURCE_URI")
    target_uri = required_env("MIGRATION_TARGET_URI")
    database_name = os.environ.get("MIGRATION_DATABASE", "autoregister").strip()
    source_client = MongoClient(source_uri, serverSelectionTimeoutMS=10_000)
    target_client = MongoClient(target_uri, serverSelectionTimeoutMS=10_000)
    try:
        source_client.admin.command("ping")
        target_client.admin.command("ping")
        source = source_client[database_name]
        target = target_client[database_name]
        names = sorted(source.list_collection_names(filter={"type": "collection"}))
        total = 0
        for name in names:
            copied, verified = copy_collection(source, target, name)
            if copied != verified:
                raise RuntimeError(f"count mismatch for {name}: {copied} != {verified}")
            total += copied
            print(f"{name}: {verified}")
        print(f"MIGRATION_OK collections={len(names)} documents={total}")
        return 0
    finally:
        source_client.close()
        target_client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MIGRATION_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
