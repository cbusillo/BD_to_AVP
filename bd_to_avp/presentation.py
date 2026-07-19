from __future__ import annotations

from bd_to_avp.runtime import RunContext


def cli_message(message: str, *, run_context: RunContext | None = None) -> None:
    if run_context is None:
        print(message)
