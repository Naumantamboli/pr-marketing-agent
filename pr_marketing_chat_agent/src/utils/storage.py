"""Conversation state and generated files: the tenant's bucket by default, ./output locally.

Chat download links resolve against the bucket named after the conversation's customer, so
every write takes that bucket explicitly; TENANT_ID is only the fallback for local runs.
"""

import json
import os
from pathlib import Path
from typing import Any

from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)

PREFIX = "pr_marketing_chat"
_LOCAL_DIR = Path(__file__).resolve().parents[2] / "output"


def _local() -> bool:
    return os.getenv("LOCAL_OUTPUT", "false").lower() == "true"


def bucket_for(customer_id: str | None) -> str:
    return customer_id or os.getenv("TENANT_ID") or "default_bucket"


def put(bucket: str, key: str, data: bytes, content_type: str) -> str:
    if _local():
        path = _LOCAL_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info("Wrote %s locally.", path)
        return key
    from common_lib.storage.storage_client import storage

    storage.store_object(bucket, key, data, content_type=content_type)
    return key


def get(bucket: str, key: str) -> bytes | None:
    if _local():
        path = _LOCAL_DIR / key
        return path.read_bytes() if path.is_file() else None
    from botocore.exceptions import ClientError
    from common_lib.storage.storage_client import RetrievalMode, storage

    try:
        return storage.retrieve(bucket, key, RetrievalMode.FULL_OBJECT)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def get_json(bucket: str, key: str) -> Any | None:
    raw = get(bucket, key)
    return json.loads(raw) if raw else None


def put_json(bucket: str, key: str, value: Any) -> str:
    return put(bucket, key, json.dumps(value, ensure_ascii=False).encode(), "application/json")
