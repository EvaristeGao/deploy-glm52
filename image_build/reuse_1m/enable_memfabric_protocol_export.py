"""Repair the missing public protocol enum in the installed MemFabric 1.2 wheel.

This build step edits Python exports only; it imports no native libraries and
does not initialize devices. The enum already exists in _pymf_transfer.
"""

import hashlib
import json
from pathlib import Path

path = Path(
    "/usr/local/python3.12.13/lib/python3.12/site-packages/memfabric_hybrid/__init__.py"
)
original = path.read_text()
needle = (
    "from _pymf_transfer import TransferEngine, TransferOpcode, create_config_store"
)
if original.count(needle) != 1:
    raise RuntimeError(
        "Unexpected MemFabric wheel: refusing an unreviewed export patch"
    )
updated = original.replace(needle, needle + ", TransDataOpType")
updated += '\n__all__.append("TransDataOpType")\n'
path.write_text(updated)
Path("/root/reuse-sdk-compat.json").write_text(
    json.dumps(
        {
            "file": str(path),
            "before_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(updated.encode()).hexdigest(),
            "change": "Re-export existing _pymf_transfer.TransDataOpType",
        },
        indent=2,
    )
    + "\n"
)
