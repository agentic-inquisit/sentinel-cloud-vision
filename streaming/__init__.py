"""gRPC streaming ingest and inference services. See streaming/README.md."""

import sys
from pathlib import Path

# Generated stubs import each other as `sentinel.v1.*`.
_GEN = str(Path(__file__).resolve().parent / "gen")
if _GEN not in sys.path:
    sys.path.insert(0, _GEN)
