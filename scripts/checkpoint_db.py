#!/usr/bin/env python3
"""Fold the -wal sidecar back into the .db file so it is self-contained.

CI commits telltale.db. In WAL mode the committed data lives in telltale.db-wal
until a checkpoint runs, so committing the .db alone would push an empty database.

    uv run python scripts/checkpoint_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telltale.storage.session import checkpoint_wal

if __name__ == "__main__":
    ran = checkpoint_wal()
    print("checkpointed" if ran else "no checkpoint needed (non-SQLite or in-memory)")
