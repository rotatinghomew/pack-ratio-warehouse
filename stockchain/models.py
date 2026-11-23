"""库存域的核心值对象：批次、仓位、事件、锁定期与调整记录。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Form(Enum):
    """实物包装形态。"""

    CASE = "case"     # 完整整箱/整盒
    LOOSE = "loose"   # 拆开后剩下的散件


class LotState(Enum):
    NORMAL = "normal"
    QUARANTINED = "quarantined"   # 标签不认识，等待包装确认
    RESOLVED = "resolved"         # 隔离批次已通过包装确认释放到新批次
    RETURNED = "returned"         # 已整箱/散件退回供货商


class CommitState(Enum):
    """门店承诺状态。未确认拣货可重排，已确认拣货不可再动。"""

    PROVISIONAL = "provisional"
    PICKED = "picked"
    SHIPPED = "shipped"
    SHORT = "short"


@dataclass(frozen=True)
class Lot:
    """一批实物。版次与每箱件数在批次上固化（收货时点快照）。

    之后目录再改换算表，也不会改变这批货的原始口径；
    换包装、更正版次都会派生出新批次，绝不就地改写。
    """

    lot_id: str
    sku: str
    revision: str
    units_per_case: int
    warehouse: str
    state: LotState = LotState.NORMAL
    source_event: str | None = None      # 派生来源（拆箱/换包装/更正）
    parent_lot: str | None = None        # 血缘父批次，用于按原单回查流转
    note: str = ""

    @property
    def quarantined(self) -> bool:
        return self.state is LotState.QUARANTINED


@dataclass(frozen=True)
class Position:
    """仓 × 批次 × 形态 的实物数量。case 数与 loose 件数分列。"""

    warehouse: str
    lot_id: str
    form: Form
    qty: int


@dataclass(frozen=True)
class Event:
    """只追加的事实事件，payload 保留原始单据口径。"""

    seq: int
    kind: str
    day: str
    payload: dict


@dataclass
class PeriodLock:
    """期末锁定：记录各仓/批次/形态的账面快照，之后变动必须留调整痕。"""

    period_end: str
    snapshot: dict[tuple[str, str, Form], int]


@dataclass
class Adjustment:
    """针对已锁定期末数量的可追溯调整。"""

    period_end: str
    event_seq: int
    kind: str
    key: tuple[str, str, Form]
    delta: int
    reason: str


@dataclass
class Hold:
    """已承诺/已拣出的占用，按仓 × 批次 × 形态记录。"""

    request_id: str
    warehouse: str
    lot_id: str
    form: Form
    qty: int
    state: CommitState
    event_seq: int | None = None


@dataclass
class Transfer:
    """在途调拨：离开发出仓、尚未到达接收仓。"""

    transfer_id: str
    sku: str
    revision: str
    units_per_case: int
    source: str
    destination: str
    cases: int
    eta: str
    source_lot: str | None = None
    state: str = "in-transit"          # in-transit / arrived
    event_seq: int | None = None


@dataclass
class PendingCorrection:
    """登记但尚未执行的旧箱规格更正。"""

    correction_id: str
    receipt_id: str
    warehouse: str
    lot_id: str
    cases: int
    actual_revision: str
    event_seq: int


@dataclass
class Warehouse:
    code: str
    can_unpack: bool = True            # 该仓是否有拆装权限


@dataclass
class ReconciliationLine:
    warehouse: str
    sku: str
    revision: str
    form: Form
    book_qty: int
    physical_qty: int
    locked_qty: int = 0
    quarantined_qty: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def variance(self) -> int:
        return self.physical_qty - self.book_qty
