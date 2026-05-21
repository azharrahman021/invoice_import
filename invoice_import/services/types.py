from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OCRResult:
    text: str
    provider: str
    confidence: float
    pages: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SupplierMatch:
    supplier: str | None
    score: float
    method: str
    created: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemMatch:
    item_code: str | None
    score: float
    method: str
    skipped: bool = False
    comment: str = ""
