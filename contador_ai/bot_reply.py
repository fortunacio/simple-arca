from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BotReply:
    text: str
    keyboard: tuple[tuple[tuple[str, str], ...], ...] = field(default_factory=tuple)
    replace_message: bool = False
    document: bytes | None = None
    filename: str = ""
