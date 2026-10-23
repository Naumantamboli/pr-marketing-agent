import logging
import sys
import types
from pathlib import Path

# Make `from utils import ...`, `from tools import ...` resolve as they do on a worker.
SRC_PATH = str(Path(__file__).parent.parent / "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

# Stub platform-only libraries the code imports at module load.
if "common_lib" not in sys.modules:
    logger_mod = types.ModuleType("common_lib.utils.logger")
    logger_mod.setup_logger = logging.getLogger
    sys.modules["common_lib"] = types.ModuleType("common_lib")
    sys.modules["common_lib.utils"] = types.ModuleType("common_lib.utils")
    sys.modules["common_lib.utils.logger"] = logger_mod
