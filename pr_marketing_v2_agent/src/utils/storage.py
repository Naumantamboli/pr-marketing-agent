"""Object storage for reports and run markers; S3 by default, ./output when LOCAL_OUTPUT=true."""

import json
import os
from pathlib import Path
from typing import Any

from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)

# Separate from v1's "pr_marketing" so the two agents never share run markers.
PREFIX = "pr_marketing_v2"
_LOCAL_DIR = Path(__file__).resolve().parents[2] / "output"


def _local() -> bool:
    return os.getenv("LOCAL_OUTPUT", "false").lower() == "true"


def _bucket() -> str:
    return os.getenv("TENANT_ID") or "default_bucket"


def put(key: str, data: bytes, content_type: str) -> str:
    if _local():
        path = _LOCAL_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info("Wrote %s locally.", path)
        return str(path)
    from common_lib.storage.storage_client import storage

    storage.store_object(_bucket(), key, data, content_type=content_type)
    return key


def get(key: str) -> bytes | None:
    if _local():
        path = _LOCAL_DIR / key
        return path.read_bytes() if path.is_file() else None
    from botocore.exceptions import ClientError
    from common_lib.storage.storage_client import RetrievalMode, storage

    try:
        return storage.retrieve(_bucket(), key, RetrievalMode.FULL_OBJECT)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _marker_key(repo_full: str) -> str:
    return f"{PREFIX}/state/{repo_full.replace('/', '__')}.json"


def read_marker(repo_full: str) -> dict[str, Any] | None:
    raw = get(_marker_key(repo_full))
    return json.loads(raw) if raw else None


def write_marker(repo_full: str, marker: dict[str, Any]) -> None:
    put(_marker_key(repo_full), json.dumps(marker).encode(), "application/json")
