"""把夹具中的事件信封序列回放入状态。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .ledger import apply_event
from .models import State


def replay(path: Optional[Path] = None) -> State:
    source = path or Path(__file__).resolve().parents[1] / "fixtures" / "scenario.json"
    envelopes = json.loads(source.read_text(encoding="utf-8"))
    state = State()
    for env in envelopes:
        apply_event(state, env["type"], env["payload"],
                    source=env.get("source", "fixture"))
    return state
