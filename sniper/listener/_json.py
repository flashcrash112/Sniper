"""JSON codec selection.

`orjson` decodes several times faster than the stdlib, which is worth having on
a feed that delivers every pump.fun transaction on the cluster. It is optional:
without it the bot works identically, just with more CPU spent per frame.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - trivial import shim
    import orjson

    def loads(data: str | bytes) -> Any:
        return orjson.loads(data)

    def dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")

    HAVE_ORJSON = True
except ImportError:  # pragma: no cover
    import json

    def loads(data: str | bytes) -> Any:
        return json.loads(data)

    def dumps(obj: Any) -> str:
        return json.dumps(obj, separators=(",", ":"))

    HAVE_ORJSON = False
