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
