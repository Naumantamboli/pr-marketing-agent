import importlib.util
import logging
import sys
import types
from pathlib import Path

# Make `from utils import ...`, `from tools import ...` resolve as they do on a worker.
SRC_PATH = str(Path(__file__).parent.parent / "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)


def _stub(name: str, **attrs) -> None:
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sys.modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


# Stub platform-only libraries when they aren't installed, so tests run anywhere.
if importlib.util.find_spec("common_lib") is None:
    _stub("common_lib.utils.logger", setup_logger=logging.getLogger)
if importlib.util.find_spec("aetherion_sdk") is None:
    _stub("aetherion_sdk", tool=lambda **_: lambda fn: fn)


# --- catalog updater fixtures ----------------------------------------------------------------
import json  # noqa: E402

import pytest  # noqa: E402

SEED_PATH = Path(__file__).parent.parent / "seed" / "catalog_seed.json"


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """Object storage redirected to a temp dir."""
    from utils import storage

    monkeypatch.setenv("LOCAL_OUTPUT", "true")
    monkeypatch.setattr(storage, "_LOCAL_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def seeded(local_store):
    """A temp store holding the reviewed baseline catalog."""
    from utils import catalog

    catalog.apply_seed(json.loads(SEED_PATH.read_text()))
    return local_store
