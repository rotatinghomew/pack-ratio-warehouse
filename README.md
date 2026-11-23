# 工业备件包装事实仓

该项目汇总供应商包装版次、仓库收发、拆装与门店补货请求，提供**按实物包装形态承诺库存**的能力，解决新旧两版包装同时周转时的口径漂移、重复承诺与追溯问题。

需要 Python 3.11 或更新版本，无第三方依赖。

## 业务约束如何落地

| 约束 | 实现位置 |
| --- | --- |
| 同一商品区分生产商允许的包装关系、生效时点 | `stockchain/catalog.py`：追加式版次目录，`spec_at(sku, rev, day)` 按时点取口径 |
| 批次实际收到的版次与每箱件数固化 | `stockchain/ledger.py` `receive()`：批次保存收货时点快照，换算表后改不影响旧批 |
| 旧签收单仍可查原版口径 | `events_of_receipt()` 与只追加事件流，收货事件永久保留原 `units_per_case` |
| 拆箱/换包装/返厂不得凭新版比例增减实物 | `unpack`/`repack`/`initiate_transfer` 内置件数守恒断言；差异只能走显式 `correction` |
| 旧箱 case-only 只能整箱退回 | `return_to_vendor()` 校验版次 `ReturnPolicy` |
| 不认识标签先隔离、后补确认释放 | 未知版次收货进 `QUARANTINED`，`confirm_packaging()` 派生子批次并保留血缘 |
| 锁定的期末数量改变须可追溯 | `lock_period()` 后落账的事件逐笔生成 `Adjustment`（事件号、增量、原因） |
| 按仓/商品/原包装版本对账，解释在途与散件 | `stockchain/reports.py` `reconciliation()` |
| 申请带窗口、可接受版次、拆箱许可 | `stockchain/allocation.py` `Request` + `Allocator` |
| 替代仍须满足完整包装数量与拆装权限 | 不允许拆箱只配整箱；拆箱行校验仓库 `can_unpack`，确认拣货时才真实拆箱 |
| 拆出余量留给后续请求 | 分配优先消化 `LOOSE` 散件池 |
| 未确认拣货可重排，已确认整箱不能再拆 | provisional 占用可释放；picked 占用由账本硬冻结，超量拆箱直接拒绝 |
| 更正旧箱规格时联合重核多张申请、不重复占用 | `replan_affected()` 先统一释放再按优先级重算，预留表防同批货双卖 |
| 门店查可交付量、承诺变更、批次仓间流转 | `deliverable()`、`request_status()`、`trace_lot()` |

## 模块

- `stockchain/catalog.py` — 包装版次目录（生效区间、退货政策）
- `stockchain/models.py` — 批次、仓位、事件、锁定期、调整、占用等值对象
- `stockchain/ledger.py` — 事件溯源库存账本（收/拆/换/退/调拨/更正/锁定）
- `stockchain/allocation.py` — 门店申请分配、拣货确认、联合重核
- `stockchain/reports.py` — 可交付查询、对账、批次血缘追溯
- `stockchain/fixture.py` — 装载 `data/facts.json`
- `scripts/demo_flow.py` — 干净账本上的端到端业务演示

## 运行

```bash
python3 -I -m unittest discover -s tests   # 36 个测试，不依赖网络或第三方包
python3 -I scripts/demo_flow.py           # 端到端场景演示
```

`data/facts.json` 保留原始业务资料键（`packaging`/`receipts`/`requests`/`transfer`/`correction`），
并补充 `warehouses`（拆装权限）与 `events`（按日事件流，含隔离、拆箱、调拨、返厂、期末锁定、待执行更正）。
