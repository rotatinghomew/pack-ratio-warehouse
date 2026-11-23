"""供应商包装版次目录。

同一 SKU 可以存在多版包装关系（每盒/每箱件数），每版有生产商允许的
生效区间。目录只允许追加：新增版次或登记失效，绝不回头改写旧版口径，
旧签收单因此永远可以按当时的包装关系还原（见 ``spec_at``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class ReturnPolicy(Enum):
    """生产商允许的退货形态。"""

    CASE_ONLY = "case-only"   # 只允许整箱退回供货商
    EACH = "each"             # 允许按件退回
    NONE = "none"


class PackagingError(ValueError):
    """包装关系层面的校验错误。"""


@dataclass(frozen=True)
class PackagingSpec:
    sku: str
    revision: str
    units_per_case: int
    return_policy: ReturnPolicy
    valid_from: str | None = None   # ISO 日期，含当日
    valid_to: str | None = None     # ISO 日期，不含当日（生效至 valid_to 的前一天）

    def __post_init__(self) -> None:
        if self.units_per_case <= 0:
            raise PackagingError(f"每箱件数必须为正: {self.units_per_case}")
        if self.valid_from and self.valid_to and self.valid_from >= self.valid_to:
            raise PackagingError(f"版次 {self.revision} 生效区间为空")

    def active_on(self, day: str) -> bool:
        if self.valid_from and day < self.valid_from:
            return False
        if self.valid_to and day >= self.valid_to:
            return False
        return True


class PackagingCatalog:
    """不可变追加式版次目录。"""

    def __init__(self, specs: Iterable[PackagingSpec]):
        self._specs: list[PackagingSpec] = list(specs)
        seen: set[tuple[str, str]] = set()
        for spec in self._specs:
            key = (spec.sku, spec.revision)
            if key in seen:
                raise PackagingError(f"包装版次重复登记: {key}")
            seen.add(key)

    @classmethod
    def from_fixture(cls, rows: Iterable[dict]) -> "PackagingCatalog":
        specs = [
            PackagingSpec(
                sku=row["sku"],
                revision=row["revision"],
                units_per_case=row["units_per_case"],
                return_policy=ReturnPolicy(row.get("return", "none")),
                valid_from=row.get("valid_from"),
                valid_to=row.get("valid_to"),
            )
            for row in rows
        ]
        return cls(specs)

    def get(self, sku: str, revision: str) -> PackagingSpec:
        for spec in self._specs:
            if spec.sku == sku and spec.revision == revision:
                return spec
        raise PackagingError(f"未登记的包装版次: {sku}/{revision}")

    def spec_at(self, sku: str, revision: str, day: str) -> PackagingSpec:
        """取某时点生效的版次口径；旧时点查不到新版、新时点查不到失效版。"""
        spec = self.get(sku, revision)
        if not spec.active_on(day):
            raise PackagingError(f"版次 {sku}/{revision} 在 {day} 不在生效区间内")
        return spec

    def revisions_for(self, sku: str) -> list[PackagingSpec]:
        return [s for s in self._specs if s.sku == sku]

    def known_revisions(self, sku: str) -> set[str]:
        return {s.revision for s in self.revisions_for(sku)}
