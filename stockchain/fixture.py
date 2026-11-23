"""把 data/facts.json 的合成资料装载成账本与分配器。

文件中：
* receipts/packaging/requests/transfer/correction 为原始业务资料（保持原口径）；
* warehouses 提供拆装权限；
* events 为按日发生的收拆装/调拨/隔离/锁定事件流；
* 请求补充交付窗口、可接受版次、是否允许拆箱与优先级。
"""
from __future__ import annotations

import json
from pathlib import Path

from .allocation import Allocator, Request
from .catalog import PackagingCatalog
from .ledger import InventoryLedger
from .models import Form, Warehouse

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "data" / "facts.json"


def load_fixture(path: str | Path = DEFAULT_FIXTURE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_ledger(data: dict) -> InventoryLedger:
    catalog = PackagingCatalog.from_fixture(data["packaging"])
    warehouses = [
        Warehouse(code=row["code"], can_unpack=row.get("can_unpack", True))
        for row in data.get("warehouses", [])
    ]
    ledger = InventoryLedger(catalog, warehouses)

    lot_index: dict[str, str] = {}  # 业务单号/批次引用 -> 内部 lot_id
    for event in data.get("events", []):
        kind, day = event["kind"], event["day"]
        if kind == "receive":
            lot = ledger.receive(
                receipt_id=event["receipt"],
                day=day,
                warehouse=event["warehouse"],
                sku=event["sku"],
                revision=event.get("revision"),  # 缺省 => 标签不认识，隔离
                cases=event["cases"],
                label=event.get("label"),
            )
            lot_index[event["receipt"]] = lot.lot_id
        elif kind == "lock":
            ledger.lock_period(day)
        elif kind == "unpack":
            ledger.unpack(day, lot_index[event["receipt"]], event["cases"],
                          reason=event.get("reason", "夹具：门店拆箱替代"))
        elif kind == "return":
            ledger.return_to_vendor(
                day, lot_index[event["receipt"]],
                Form.CASE if event["form"] == "case" else Form.LOOSE,
                event["qty"],
            )
        elif kind == "transfer_out":
            tr = ledger.initiate_transfer(
                transfer_id=event["transfer"],
                day=day,
                source=event["from"],
                destination=event["to"],
                lot_id=lot_index[event["receipt"]],
                cases=event["cases"],
                eta=event["eta"],
            )
            lot_index[event["transfer"]] = tr.source_lot
        elif kind == "transfer_arrive":
            new_lot = ledger.arrive_transfer(day, event["transfer"])
            lot_index[event["transfer"]] = new_lot.lot_id
        elif kind == "correction_register":
            ledger.register_correction(
                day=day,
                correction_id=event["correction"],
                receipt_id=event["receipt"],
                lot_id=lot_index[event["receipt"]],
                cases=event["cases"],
                actual_revision=event["actual_revision"],
            )
        else:
            raise ValueError(f"夹具中存在未知事件类型: {kind}")
    return ledger


def build_allocator(ledger: InventoryLedger, data: dict) -> Allocator:
    allocator = Allocator(ledger)
    requests = [
        Request(
            request_id=row["id"],
            store=row["store"],
            sku=row["sku"],
            units=row["units"],
            window=row["window"],
            allowed_revisions=frozenset(row.get("allowed_revisions", ())),
            allow_split=row.get("allow_split", False),
            priority=row.get("priority", 0),
            created_day=row.get("created_day"),
        )
        for row in data["requests"]
    ]
    requests.sort(key=lambda r: (-r.priority, r.request_id))
    for req in requests:
        allocator.add_request(req)
    return allocator


def build(path: str | Path = DEFAULT_FIXTURE):
    data = load_fixture(path)
    ledger = build_ledger(data)
    allocator = build_allocator(ledger, data)
    return data, ledger, allocator
