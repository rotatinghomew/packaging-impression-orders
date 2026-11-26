"""声明撤回后的拼版委印、仓班台账与结算服务。

仅使用 Python 3.11 标准库。核心对象均可直接以字典构造，便于单测调用；
``load_service`` 用于读取仓库 ``inputs/`` 下的交换 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Any, Iterable

CN_TZ = timezone(timedelta(hours=8))
MONEY_Q = Decimal("0.01")

PRINTED_KINDS = {"printed", "print", "off_press", "down_press", "下机", "已印"}
WASTE_KINDS = {"rejected", "reject", "scrapped", "scrap", "waste", "判废", "报废"}
LOCKED_KINDS = {"locked", "lock", "锁版", "已锁版"}
PENDING_STOP_KINDS = {
    "stop_pending",
    "pending_stop",
    "stop_requested",
    "stop_request",
    "cancellation_pending",
    "待停",
    "撤回待确认",
}
CONFIRMED_STOP_KINDS = {
    "stop_confirmed",
    "stopped",
    "stop_accepted",
    "cancelled",
    "cancel_confirmed",
    "confirmed_cancel",
    "已停",
    "确认取消",
}
OVERLAY_KINDS = {"overlaid", "overlay_completed", "relabelled", "relabeled", "已补贴", "已改贴"}
RECEIVED_KINDS = {"received", "receipt", "inbound", "实收", "入库"}
ISSUED_KINDS = {"issued", "issue", "requisitioned", "领用", "已出给生产"}
DELIVERED_KINDS = {"delivered", "delivery", "shipped", "交付", "已交买方"}


class ServiceError(ValueError):
    """业务数据不满足服务口径。"""


class UnknownProofError(ServiceError):
    """印单引用了未批准/未知的版稿修订。"""


class DuplicateEventError(ServiceError):
    """同一来源事件编号携带了互相冲突的内容。"""


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ServiceError(f"非法数值: {value!r}") from exc


def money(value: Decimal | int | str | None) -> Decimal:
    return D(0 if value is None else value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def iso_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ServiceError(f"非法 ISO 8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt


def local_day_end(value: Any) -> datetime:
    """把 ``YYYY-MM-DD`` 的有效期末解释为销售地当天闭市。"""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt
    if isinstance(value, date):
        d = value
    else:
        d = date.fromisoformat(str(value)[:10])
    return datetime.combine(d, time(23, 59, 59, 999999), CN_TZ)


def _sorted_claims(claims: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(str(c) for c in claims))


def _as_list(value: Any, *keys: str) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, list):
                return [x for x in nested if isinstance(x, dict)]
        return [value]
    return []


def _sites_from(row: dict) -> list[str]:
    for key in ("sites", "positions", "position_sites"):
        sites = row.get(key)
        if isinstance(sites, list):
            return [str(x) for x in sites]
    for key in ("site", "position"):
        if row.get(key) is not None:
            return [str(row[key])]
    return []


def _qty_map(row: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    count = row.get("count")
    if not isinstance(count, dict):
        count = row.get("counts") or row.get("quantities")
    if isinstance(count, dict):
        for site, qty in count.items():
            out[str(site)] = int(D(qty))
    site = row.get("site", row.get("position"))
    if site is not None:
        qty_value = row.get("quantity", row.get("qty", row.get("count")))
        if qty_value is not None and not isinstance(qty_value, dict):
            out[str(site)] = int(D(qty_value))
    return out


def _correction_rank(row: dict) -> int:
    rank = 0
    for key in (
        "correction_revision",
        "revision",
        "correction_no",
        "修正序号",
        "correction_version",
        "更正序号",
    ):
        if key in row:
            try:
                rank = max(rank, int(D(row[key])))
            except ServiceError:
                pass
    correction_keys = (
        "correction",
        "correction_id",
        "corrects",
        "correction_of",
        "supersedes",
        "replaces",
        "correction_flag",
        "amendment_id",
        "更正标识",
        "修正标识",
        "更正编号",
    )
    if any(k in row for k in correction_keys):
        rank += 100
    if row.get("is_correction") is True or row.get("更正") is True:
        rank += 100
    return rank


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_exchange(inputs_dir: str | Path | None = None) -> "CommissionPrintingService":
    return load_service(inputs_dir)


def load_service(inputs_dir: str | Path | None = None) -> "CommissionPrintingService":
    root = Path(inputs_dir) if inputs_dir else Path(__file__).resolve().parent / "inputs"
    design = load_json(root / "designs.json")
    press = load_json(root / "press.json")
    stock = load_json(root / "stock.json")
    orders_path = root / "orders.json"
    orders = load_json(orders_path) if orders_path.exists() else None
    return CommissionPrintingService(design=design, press=press, stock=stock, orders=orders)


@dataclass(frozen=True)
class ProofRef:
    plate: str
    revision: Any


class CommissionPrintingService:
    """保存版稿、印厂回执、仓班事件和销售订单，并生成三方口径。"""

    def __init__(
        self,
        design: dict | list | None = None,
        press: dict | list | None = None,
        stock: list | dict | None = None,
        orders: list | dict | None = None,
    ) -> None:
        self.proofs: dict[ProofRef, dict] = {}
        self.proof_order: list[ProofRef] = []
        self.press_orders: list[dict] = []
        self.stock_events: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.issue_records: dict[str, dict] = {}
        self.errors: list[dict] = []
        self._source_ids: dict[str, Any] = {}

        if design is not None:
            designs = design if isinstance(design, list) else [design]
            for item in designs:
                self.receive_design(item)
        if press is not None:
            presses = press if isinstance(press, list) else [press]
            for item in presses:
                self.receive_press_order(item)
        if stock is not None:
            events = stock if isinstance(stock, list) else _as_list(stock, "events", "records", "items")
            for event in events:
                self.receive_stock_event(event)
        if orders is not None:
            order_items = orders if isinstance(orders, list) else _as_list(orders, "orders", "items")
            for order in order_items:
                self.receive_order(order)

    # ------------------------------------------------------------------ intake

    def _idempotent(self, source: str, row: dict) -> bool:
        """返回 True 表示首次接收；False 表示完全相同的重复回执。"""
        store = self._source_ids.setdefault(source, {})  # type: ignore[assignment]
        event_id = row.get("id")
        if event_id is None:
            # 没有编号的事件不做跨调用幂等；交换材料中的正式记录均有 id。
            return True
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if event_id not in store:
            store[event_id] = [payload]
            return True
        if payload in store[event_id]:
            return False
        if _correction_rank(row) > 0:
            store[event_id].append(payload)
            return True
        self.errors.append(
            {
                "type": "duplicate_conflicting_source_id",
                "source": source,
                "id": event_id,
                "row": row,
            }
        )
        # 冲突件仍保留用于审计，但调用方不应把它当作同编号的新版本覆盖事实。
        return True

    def receive_design(self, design: dict) -> ProofRef:
        ref = ProofRef(str(design["plate"]), design["revision"])
        if ref not in self.proofs:
            self.proofs[ref] = design
            self.proof_order.append(ref)
        return ref

    def receive_press_order(self, press: dict) -> str:
        po = str(press.get("order", press.get("id", f"PRESS-{len(self.press_orders)+1}")))
        self.press_orders.append(press)
        return po

    def receive_stock_event(self, event: dict) -> bool:
        if not isinstance(event, dict):
            return False
        is_new = self._idempotent("stock", event)
        # 即便同 id 冲突，也保留原始事件供审计；完全相同的重发不重复入账。
        if is_new:
            self.stock_events.append(event)
        return is_new

    def receive_order(self, order: dict) -> str:
        oid = str(order.get("id") or order.get("order") or order.get("order_id"))
        if not oid or oid == "None":
            raise ServiceError("销售订单必须有 id/order")
        normalized = dict(order)
        normalized["id"] = oid
        self.orders[oid] = normalized
        return oid

    def add_order(self, market: str, sku: str, delivery_date: str, quantity: int | None = None, **extra: Any) -> str:
        oid = str(extra.pop("id", None) or extra.pop("order", None) or f"ORD-{len(self.orders)+1}")
        order = {
            "id": oid,
            "market": market,
            "sku": sku,
            "delivery_date": delivery_date,
            "expected_delivery": delivery_date,
            "quantity": quantity,
            **extra,
        }
        self.receive_order(order)
        return oid

    # ------------------------------------------------------------ proof rules

    def _proof_for(self, press: dict) -> tuple[ProofRef | None, dict | None, dict | None]:
        ref = ProofRef(str(press.get("plate")), press.get("revision"))
        proof = self.proofs.get(ref)
        if proof is None:
            error = {
                "type": "unknown_plate_revision",
                "plate": ref.plate,
                "revision": ref.revision,
                "order": press.get("order"),
            }
            if error not in self.errors:
                self.errors.append(error)
            return ref, None, None
        if str(proof.get("plate")) != str(press.get("plate")) or proof.get("revision") != press.get("revision"):
            return ref, None, None
        withdrawal = self._withdrawal(proof)
        overlay = self._overlays(proof)
        return ref, proof, {"withdrawal": withdrawal, "overlays": overlay}

    @staticmethod
    def _withdrawal(proof: dict) -> dict | None:
        wd = proof.get("withdrawal")
        if isinstance(wd, dict):
            return wd
        for item in _as_list(proof, "withdrawals"):
            return item
        return None

    @staticmethod
    def _withdrawal_sites(wd: dict | None) -> list[str]:
        if not wd:
            return []
        raw = wd.get("sites", wd.get("positions", wd.get("position_sites", [])))
        return [str(x) for x in raw] if isinstance(raw, list) else []

    @staticmethod
    def _overlays(proof: dict | None) -> list[dict]:
        overlays: list[dict] = []
        if not proof:
            return overlays
        if isinstance(proof.get("overlay"), dict):
            overlays.append(proof["overlay"])
        overlays.extend(_as_list(proof, "overlays"))
        return overlays

    def position_map(self, proof: dict) -> dict[str, dict]:
        return {str(p["site"]): p for p in proof.get("positions", [])}

    def permit_for(self, proof: dict, claim: str, market: str, sku: str, at: datetime | None = None,
                  delivery_date: date | None = None) -> dict | None:
        for permit in proof.get("permit", proof.get("permits", [])):
            if str(permit.get("claim")) != claim:
                continue
            issuer = permit.get("issuer")
            markets = permit.get("markets") or ([permit["market"]] if permit.get("market") else [])
            skus = permit.get("skus") or ([permit["sku"]] if permit.get("sku") else [])
            if market not in [str(x) for x in markets]:
                continue
            if sku not in [str(x) for x in skus]:
                continue
            if permit.get("status") in {"revoked", "suspended", "invalid", "撤销", "暂停"}:
                continue
            valid_from = permit.get("valid_from")
            if valid_from and at and iso_dt(valid_from) and at < iso_dt(valid_from):
                continue
            if permit.get("valid_from_date"):
                try:
                    vf = date.fromisoformat(str(permit["valid_from_date"])[:10])
                    if delivery_date and delivery_date < vf:
                        continue
                except ValueError:
                    pass
            valid_to = permit.get("valid_to") or permit.get("valid_until")
            if valid_to:
                if delivery_date is not None and len(str(valid_to)) == 10:
                    if delivery_date > date.fromisoformat(str(valid_to)[:10]):
                        continue
                elif at is not None:
                    if at > local_day_end(valid_to):
                        continue
            return permit
        return None

    def combination_exists(self, proof: dict, market: str, sku: str, claims: tuple[str, ...],
                           at: datetime | None = None) -> dict | None:
        wanted = _sorted_claims(claims)
        for combo in proof.get("combinations", []):
            if str(combo.get("market")) != str(market) or str(combo.get("sku")) != str(sku):
                continue
            if _sorted_claims(combo.get("claims", [])) != wanted:
                continue
            # 允许规范本身带有效时段；样例未带时视为本交换期有效。
            if at is not None:
                if combo.get("valid_from") and at < iso_dt(combo["valid_from"]):
                    continue
                if combo.get("valid_to") and at > local_day_end(combo["valid_to"]):
                    continue
            return combo
        return None

    def withdrawal_active(self, proof: dict, market: str, at: datetime | None = None,
                          delivery_date: date | None = None) -> bool:
        wd = self._withdrawal(proof)
        if not wd or str(wd.get("market")) != str(market):
            return False
        effective = iso_dt(wd.get("effective"))
        if at is not None and effective:
            return at >= effective
        if delivery_date is not None and effective:
            ed = effective.astimezone(CN_TZ).date()
            if delivery_date > ed:
                return True
            if delivery_date < ed:
                return False
            # 预计交付日恰逢撤回生效日而无具体时刻：计划口径从严，生效后不得沿用旧声明。
            return True
        return False

    def allowed_uses(
        self,
        proof: dict,
        claims: Iterable[str],
        at: datetime | None = None,
        delivery_date: date | None = None,
        include_withdrawn: bool = False,
    ) -> list[dict]:
        """返回声明集合当前可用于哪些 market/sku，及每条依据。"""
        claim_tuple = _sorted_claims(claims)
        uses: list[dict] = []
        for combo in proof.get("combinations", []):
            market, sku = str(combo["market"]), str(combo["sku"])
            if _sorted_claims(combo.get("claims", [])) != claim_tuple:
                continue
            valid_from = combo.get("valid_from", combo.get("effective_from", combo.get("from")))
            valid_to = combo.get("valid_to", combo.get("effective_to", combo.get("to")))
            if delivery_date is not None:
                if valid_from and delivery_date < date.fromisoformat(str(valid_from)[:10]):
                    continue
                if valid_to and delivery_date > date.fromisoformat(str(valid_to)[:10]):
                    continue
            elif at is not None:
                if valid_from and at < iso_dt(valid_from):
                    continue
                if valid_to and at > local_day_end(valid_to):
                    continue
            permits = {}
            missing = []
            for claim in claim_tuple:
                permit = self.permit_for(proof, claim, market, sku, at=at, delivery_date=delivery_date)
                if permit is None:
                    missing.append(claim)
                else:
                    permits[claim] = permit
            if missing:
                continue
            wd = self._withdrawal(proof)
            withdrawn_here = bool(
                wd
                and str(wd.get("market")) == market
                and str(wd.get("claim")) in claim_tuple
                and (
                    (at is not None and iso_dt(wd.get("effective")) and at >= iso_dt(wd["effective"]))
                    or (at is None and self.withdrawal_active(proof, market, delivery_date=delivery_date))
                )
            )
            if withdrawn_here and not include_withdrawn:
                continue
            uses.append(
                {
                    "market": market,
                    "sku": sku,
                    "claims": list(claim_tuple),
                    "combination": combo,
                    "permits": permits,
                    "withdrawn_claim_still_present": withdrawn_here,
                }
            )
        return uses

    def _default_as_of(self) -> datetime:
        times: list[datetime] = []
        for press in self.press_orders:
            for row in press.get("reports", []):
                dt = iso_dt(row.get("at"))
                if dt:
                    times.append(dt)
        for event in self.stock_events:
            dt = iso_dt(event.get("at"))
            if dt:
                times.append(dt)
        return max(times) if times else datetime.now(CN_TZ)

    def proof_report(self, proof: dict | None = None, *, allow_default: bool = True, as_of: datetime | str | None = None) -> dict:
        at = iso_dt(as_of) if as_of else self._default_as_of()
        if proof is None:
            if not allow_default or not self.proofs:
                return {
                    "accepted": False,
                    "errors": [e for e in self.errors if e.get("type") == "unknown_plate_revision"],
                }
            proof = self.proofs[self.proof_order[0]]
        wd = self._withdrawal(proof)
        effective = iso_dt(wd.get("effective")) if wd else None
        historical_at = effective - timedelta(seconds=1) if effective else at
        positions = []
        for pos in proof.get("positions", []):
            site = str(pos["site"])
            claims = list(pos.get("claims", []))
            permits = []
            for claim in claims:
                matches = [
                    p
                    for p in proof.get("permit", [])
                    if str(p.get("claim")) == claim
                    and (at is None or at <= local_day_end(p.get("valid_to")))
                ]
                permits.extend(matches)
            historical_uses = self.allowed_uses(proof, claims, at=historical_at)
            current_original_uses = self.allowed_uses(proof, claims, at=at)
            positions.append(
                {
                    "site": site,
                    "text": pos.get("text", ""),
                    "claims": claims,
                    "share": int(pos.get("share", 1)),
                    "source_permits": permits,
                    "credential_complete": len(permits) >= len(claims),
                    "approved_combinations": historical_uses,
                    "current_original_uses": current_original_uses,
                    "overlay_ids": [
                        str(o["id"])
                        for o in self._overlays(proof)
                        if site in [str(x) for x in o.get("positions", [])]
                    ],
                }
            )
        return {
            "plate": proof.get("plate"),
            "revision": proof.get("revision"),
            "accepted": True,
            "rule": "印厂必须按 (plate, revision) 认版；同名不同 revision 不得下发机台",
            "positions": positions,
            "withdrawal": self._withdrawal(proof),
            "overlays": self._overlays(proof),
        }

    def approve_print_order(self, press: dict | str | None = None, at: datetime | str | None = None) -> dict:
        """核对印单引用的版稿修订、位置和声明凭据。

        印刷批准只解决“印厂能否按这版这些位置生产/结算”；客户组合是否支持销售另在
        ``supported_uses`` / ``spec_supported`` 中表达，避免把无客户组合的实物从台账删除。
        """
        if press is None:
            if not self.press_orders:
                raise ServiceError("缺少委印单")
            press_row = self.press_orders[0]
        elif isinstance(press, str):
            press_row = next((p for p in self.press_orders if p.get("order") == press), None)
            if press_row is None:
                raise ServiceError(f"找不到委印单 {press}")
        else:
            press_row = press
        ref, proof, _ = self._proof_for(press_row)
        if proof is None:
            raise UnknownProofError(f"印单引用未知版稿修订：{ref}")
        check_at = iso_dt(at) if at else (iso_dt(press_row.get("approved_at")) or self._default_as_of())
        positions = self.position_map(proof)
        sites = [str(s) for s in press_row.get("sites", list(positions))]
        site_results = []
        for site in sites:
            pos = positions.get(site)
            if pos is None:
                site_results.append({"site": site, "approved": False, "reasons": ["unknown_position"]})
                continue
            claims = [str(c) for c in pos.get("claims", [])]
            permits = []
            missing = []
            for claim in claims:
                match = next(
                    (
                        p
                        for p in proof.get("permit", proof.get("permits", []))
                        if str(p.get("claim")) == claim
                        and (check_at is None or check_at <= local_day_end(p.get("valid_to")))
                        and p.get("status") not in {"revoked", "suspended", "invalid"}
                    ),
                    None,
                )
                if match:
                    permits.append(match)
                else:
                    missing.append(claim)
            combos = self.allowed_uses(proof, claims, at=check_at, include_withdrawn=True)
            approved = not missing
            site_results.append(
                {
                    "site": site,
                    "approved_to_print": approved,
                    "credential_complete": not missing,
                    "missing_claims": missing,
                    "permits": permits,
                    "spec_supported": bool(combos),
                    "supported_combinations": combos,
                    "share": pos.get("share", 1),
                    "reasons": [] if approved else ["claim_source_permit_missing_or_expired"],
                }
            )
        printer_approved = all(x.get("approved_to_print") for x in site_results)
        spec_complete = all(x.get("spec_supported") for x in site_results)
        return {
            "purchase_order": press_row.get("order"),
            "proof_ref": {"plate": ref.plate, "revision": ref.revision},
            "approved": printer_approved,
            "printer_recognized": printer_approved,
            "approved_for_generic_printing": printer_approved,
            "packaging_spec_complete": spec_complete,
            "approved_for_known_customer_uses": spec_complete,
            "sites": site_results,
            "rule": "必须按 (plate, revision) 认版；客户组合缺失只限制用途，不删除已发生印刷事实。",
        }

    # ------------------------------------------------------------- press view

    def _press_index(self, press: dict, proof: dict | None) -> dict:
        positions = self.position_map(proof) if proof else {}
        requested_sheets = int(D(press.get("requested_sheets", 0)))
        order_sites = [str(s) for s in press.get("sites", list(positions))]
        requested_qty: dict[str, int] = {}
        explicit_qty = press.get("quantities") or press.get("requested_quantities")
        if isinstance(explicit_qty, dict):
            requested_qty = {str(k): int(D(v)) for k, v in explicit_qty.items()}
        else:
            for site in order_sites:
                share = int(positions.get(site, {}).get("share", 1))
                requested_qty[site] = requested_sheets * share

        printed: dict[tuple[str, str], dict] = {}
        waste: dict[tuple[str, str], list[dict]] = {}
        timeline: dict[str, list[dict]] = {s: [] for s in order_sites}
        for site in positions:
            timeline.setdefault(site, [])

        for report in sorted(press.get("reports", []), key=lambda r: iso_dt(r.get("at")) or datetime.min.replace(tzinfo=CN_TZ)):
            kind = str(report.get("kind", "")).lower()
            rid = report.get("id")
            at = report.get("at")
            if kind in PRINTED_KINDS:
                run = str(report.get("run"))
                for site, qty in _qty_map(report).items():
                    key = (run, site)
                    entry = printed.get(key)
                    if entry is None:
                        printed[key] = {
                            "run": run,
                            "site": site,
                            "printed_qty": qty,
                            "report_ids": [rid],
                            "report_id": rid,
                            "at": at,
                            "reports": [report],
                            "reconciliation": "unique_run_site_batch",
                        }
                    else:
                        entry["report_ids"].append(rid)
                        entry["reports"].append(report)
                        if qty == entry["printed_qty"]:
                            entry["reconciliation"] = "duplicate_same_run_site_count_ignored"
                        else:
                            old_rank = max((_correction_rank(r) for r in entry["reports"][:-1]), default=0)
                            new_rank = _correction_rank(report)
                            if new_rank > old_rank:
                                entry["printed_qty"] = qty
                                entry["at"] = at
                                entry["report_id"] = rid
                                entry["reconciliation"] = "corrected_count"
                            else:
                                # 同印次同位置的完工回执是同一批实物；取最后时间的最终点数，
                                # 同时保留冲突，绝不把两张回执相加。
                                if iso_dt(at) and iso_dt(entry.get("at")) and iso_dt(at) >= iso_dt(entry.get("at")):
                                    entry["printed_qty"] = qty
                                    entry["at"] = at
                                    entry["report_id"] = rid
                                entry["reconciliation"] = "conflicting_same_batch_reconciled_to_latest"
                                conflict = {
                                    "type": "conflicting_printed_count",
                                    "run": run,
                                    "site": site,
                                    "report_ids": entry["report_ids"],
                                }
                                if not any(
                                    e.get("type") == conflict["type"]
                                    and e.get("run") == run
                                    and e.get("site") == site
                                    for e in self.errors
                                ):
                                    self.errors.append(conflict)
                    timeline.setdefault(site, []).append(
                        {"type": "printed", "run": run, "qty": qty, "at": at, "id": rid}
                    )
                for waste_key in ("rejected", "rejected_count", "reject_count", "waste", "scrapped", "defective"):
                    if waste_key not in report:
                        continue
                    waste_value = report[waste_key]
                    waste_like = {
                        "site": report.get("site"),
                        "count": waste_value if isinstance(waste_value, dict) else None,
                        "quantity": None if isinstance(waste_value, dict) else waste_value,
                    }
                    for site, qty in _qty_map(waste_like).items():
                        waste.setdefault((run, site), []).append(
                            {"qty": qty, "id": f"{rid}:{waste_key}", "at": at, "report": report}
                        )
            elif kind in WASTE_KINDS:
                run = str(report.get("run", ""))
                for site, qty in _qty_map(report).items():
                    waste.setdefault((run, site), []).append(
                        {"qty": qty, "id": rid, "at": at, "report": report}
                    )
                    timeline.setdefault(site, []).append(
                        {"type": "rejected", "run": run, "qty": qty, "at": at, "id": rid}
                    )
            elif kind in LOCKED_KINDS:
                for site in _sites_from(report):
                    timeline.setdefault(site, []).append({"type": "locked", "at": at, "id": rid})
            elif kind in PENDING_STOP_KINDS:
                for site in _sites_from(report):
                    timeline.setdefault(site, []).append({"type": "stop_pending", "at": at, "id": rid})
            elif kind in CONFIRMED_STOP_KINDS:
                for site in _sites_from(report):
                    timeline.setdefault(site, []).append({"type": "stop_confirmed", "at": at, "id": rid})

        # 同一判废事件 id 重发不重复；不同判废记录可累计。
        waste_qty: dict[tuple[str, str], int] = {}
        seen_waste_ids: set[Any] = set()
        for key, rows in waste.items():
            total = 0
            for row in rows:
                wid = row.get("id")
                if wid is not None and wid in seen_waste_ids:
                    continue
                if wid is not None:
                    seen_waste_ids.add(wid)
                total += int(row["qty"])
            waste_qty[key] = total

        confirmed_stop_sites: dict[str, dict] = {}
        pending_stop_sites: dict[str, list[dict]] = {}
        for site, events in timeline.items():
            for ev in events:
                if ev["type"] == "stop_confirmed":
                    # 同一位置确认取消只产生一个结算项；重复回执不重复付费。
                    confirmed_stop_sites.setdefault(site, ev)
                elif ev["type"] == "stop_pending":
                    pending_stop_sites.setdefault(site, []).append(ev)

        return {
            "press": press,
            "proof": proof,
            "positions": positions,
            "order_sites": order_sites,
            "requested_qty": requested_qty,
            "printed": printed,
            "waste_qty": waste_qty,
            "timeline": timeline,
            "confirmed_stop_sites": confirmed_stop_sites,
            "pending_stop_sites": pending_stop_sites,
        }

    def _primary_press_context(self) -> tuple[dict, dict | None, dict | None, dict]:
        if not self.press_orders:
            raise ServiceError("缺少 press.json 委印单")
        press = self.press_orders[0]
        ref, proof, extra = self._proof_for(press)
        ctx = self._press_index(press, proof)
        ctx["ref"] = ref
        ctx["extra"] = extra or {}
        return press, proof, extra, ctx

    # ---------------------------------------------------------- withdrawal view

    def purchasing_report(self, withdrawal_id: str | None = None) -> dict:
        press, proof, extra, ctx = self._primary_press_context()
        if proof is None:
            raise UnknownProofError("印单引用的版稿修订不存在")
        wd = extra.get("withdrawal") if extra else self._withdrawal(proof)
        if wd is None:
            return {"withdrawal": None, "sites": [], "will_receive": []}
        if withdrawal_id and wd.get("id") != withdrawal_id:
            # 支持多撤回时可在此扩展；当前交换只有一条。
            match = next((w for w in _as_list(proof, "withdrawals") if w.get("id") == withdrawal_id), None)
            if match:
                wd = match
        effective = iso_dt(wd.get("effective"))
        claim = str(wd.get("claim"))
        market = str(wd.get("market"))

        stock_index = self._stock_index(ctx)
        site_reports = []
        will_receive = []

        affected = [
            s
            for s, pos in ctx["positions"].items()
            if claim in [str(c) for c in pos.get("claims", [])]
            and (not self._withdrawal_sites(wd) or s in self._withdrawal_sites(wd))
        ]
        if self._withdrawal_sites(wd):
            affected = list(dict.fromkeys(self._withdrawal_sites(wd) + affected))

        for site in affected:
            batches = [b for (run, s), b in ctx["printed"].items() if s == site]
            before, after = [], []
            for batch in sorted(batches, key=lambda b: iso_dt(b.get("at")) or datetime.min.replace(tzinfo=CN_TZ)):
                item = self._delivery_expectation(batch, stock_index, ctx)
                will_receive.append(item)
                if effective and iso_dt(batch.get("at")) and iso_dt(batch["at"]) < effective:
                    before.append(item)
                else:
                    after.append(item)

            pending_events = ctx["pending_stop_sites"].get(site, [])
            confirmed = ctx["confirmed_stop_sites"].get(site)
            pending_at = pending_events[0]["at"] if pending_events else None
            # 若在 pending 之后、confirmed 之前仍有下机回执，该批已不可能停下。
            unstopped_batches = []
            for item in after:
                print_at = iso_dt(item.get("printed_at"))
                if pending_at and iso_dt(pending_at) and print_at and print_at >= iso_dt(pending_at):
                    item["stop_outcome"] = "printed_after_stop_pending"
                    unstopped_batches.append(item)
                elif not confirmed:
                    item["stop_outcome"] = "printed_after_effective_without_stop_confirmation"
                    unstopped_batches.append(item)
            if confirmed:
                stop_state = "confirmed_stopped"
                possible_more = False
            elif unstopped_batches:
                stop_state = "stop_failed_or_timed_out_for_reported_batch"
                possible_more = True
            elif pending_events:
                stop_state = "stop_pending_unknown"
                possible_more = True
            else:
                stop_state = "stop_not_requested"
                possible_more = True

            printed_qty = sum(int(b["printed_qty"]) for b in batches)
            before_qty = sum(int(x["printed_qty"]) for x in before)
            after_qty = sum(int(x["printed_qty"]) for x in after)
            requested_qty = int(ctx["requested_qty"].get(site, 0))
            stop_target_qty = max(0, requested_qty - before_qty)
            site_reports.append(
                {
                    "site": site,
                    "claim": claim,
                    "market": market,
                    "should_stop_future_unprinted": True,
                    "stop_target_qty_at_effective": stop_target_qty,
                    "stop_state": stop_state,
                    "stop_failed_or_timed_out": stop_state == "stop_failed_or_timed_out_for_reported_batch",
                    "stop_pending_at": pending_at,
                    "stop_pending_report_ids": [e.get("id") for e in pending_events],
                    "stop_confirmed_at": confirmed.get("at") if confirmed else None,
                    "stop_confirmed_report_id": confirmed.get("id") if confirmed else None,
                    "printed_before_effective": before,
                    "printed_after_effective": after,
                    "unstopped_reported_batches": unstopped_batches,
                    "requested_qty": requested_qty,
                    "printed_qty": printed_qty,
                    "printed_before_effective_qty": before_qty,
                    "printed_after_effective_qty": after_qty,
                    "unprinted_requested_qty": max(0, requested_qty - printed_qty),
                    "possible_future_delivery_without_final_stop_or_completion": possible_more,
                    "basis": {
                        "withdrawal_id": wd.get("id"),
                        "effective": wd.get("effective"),
                        "position_claims": ctx["positions"].get(site, {}).get("claims", []),
                    },
                }
            )

        unaffected_will_receive = []
        for (run, site), batch in ctx["printed"].items():
            if site not in affected:
                item = self._delivery_expectation(batch, stock_index, ctx)
                unaffected_will_receive.append(item)

        return {
            "withdrawal": wd,
            "affected_sites": affected,
            "sites": site_reports,
            "will_receive": will_receive,
            "unaffected_printed_batches": unaffected_will_receive,
            "answer": {
                "应停位置": affected,
                "已确认停住": [s["site"] for s in site_reports if s["stop_state"] == "confirmed_stopped"],
                "超时或未能停住且已有下机": [
                    {"site": s["site"], "qty": sum(x["printed_qty"] for x in s["unstopped_reported_batches"])}
                    for s in site_reports
                    if s["unstopped_reported_batches"]
                ],
                "印好待收": [
                    {"run": x["run"], "site": x["site"], "outstanding_qty": x["outstanding_receipt_qty"]}
                    for x in will_receive
                    if x["outstanding_receipt_qty"] > 0
                ],
            },
        }

    def _canonical_events(self, events: list[dict]) -> list[dict]:
        """同编号更正件取代原件用于计算；无编号事件原样保留。"""
        by_id: dict[Any, dict] = {}
        no_id: list[dict] = []
        for event in events:
            event_id = event.get("id")
            if event_id is None:
                no_id.append(event)
                continue
            old = by_id.get(event_id)
            if old is None:
                by_id[event_id] = event
                continue
            new_rank, old_rank = _correction_rank(event), _correction_rank(old)
            if new_rank > old_rank:
                by_id[event_id] = event
            elif new_rank == old_rank:
                new_at, old_at = iso_dt(event.get("at")), iso_dt(old.get("at"))
                if new_at and old_at and new_at >= old_at:
                    by_id[event_id] = event
        return no_id + list(by_id.values())

    def _canonical_stock_events(self) -> list[dict]:
        return self._canonical_events(self.stock_events)

    def _stock_index(self, ctx: dict | None = None) -> dict[tuple[str, str], dict]:
        press_ctx = ctx
        if press_ctx is None and self.press_orders:
            _, _, _, press_ctx = self._primary_press_context()
        valid_overlay_ids = {str(o.get("id")) for o in self._overlays(press_ctx["proof"]) if press_ctx and press_ctx.get("proof")}
        index: dict[tuple[str, str], dict] = {}
        for event in self._canonical_stock_events():
            key = (str(event.get("run")), str(event.get("site")))
            agg = index.setdefault(
                key,
                {
                    "run": key[0],
                    "site": key[1],
                    "received_qty": 0,
                    "issued_qty": 0,
                    "overlay_issued_qty": 0,
                    "delivered_qty": 0,
                    "overlaid_qty_raw": 0,
                    "valid_overlaid_qty": 0,
                    "invalid_overlay_qty": 0,
                    "other_out_qty": 0,
                    "events": [],
                    "receipt_ids": [],
                    "issue_ids": [],
                    "delivery_ids": [],
                    "overlay_matches": [],
                    "overlay_rejections": [],
                    "orders": {},
                },
            )
            kind = str(event.get("kind", "")).lower()
            qty = int(D(event.get("quantity", event.get("qty", 0))))
            agg["events"].append(event)
            oid = event.get("order") or event.get("order_id")
            order_agg = None
            if oid:
                order_agg = agg["orders"].setdefault(
                    str(oid), {"issued_qty": 0, "delivered_qty": 0, "events": []}
                )
                order_agg["events"].append(event.get("id"))
            if kind in RECEIVED_KINDS:
                agg["received_qty"] += qty
                agg["receipt_ids"].append(event.get("id"))
            elif kind in ISSUED_KINDS:
                agg["issued_qty"] += qty
                agg["issue_ids"].append(event.get("id"))
                basis = event.get("basis", [])
                overlay_qty = event.get("overlay_issued_qty")
                if overlay_qty is not None:
                    agg["overlay_issued_qty"] += int(D(overlay_qty))
                elif (
                    event.get("layer") == "overlaid"
                    or event.get("overlay_id")
                    or event.get("overlay_ids")
                    or "approved_overlay_and_completion_record" in basis
                ):
                    agg["overlay_issued_qty"] += qty
                if order_agg:
                    order_agg["issued_qty"] += qty
            elif kind in DELIVERED_KINDS:
                agg["delivered_qty"] += qty
                agg["delivery_ids"].append(event.get("id"))
                if order_agg:
                    order_agg["delivered_qty"] += qty
            elif kind in OVERLAY_KINDS:
                agg["overlaid_qty_raw"] += qty
                match = self._validate_overlay_event(event, press_ctx)
                if match.get("valid"):
                    agg["valid_overlaid_qty"] += qty
                    agg["overlay_matches"].append(match)
                else:
                    agg["invalid_overlay_qty"] += qty
                    agg["overlay_rejections"].append(match)
            elif kind in WASTE_KINDS or kind in {"discarded", "adjustment_out"}:
                agg["other_out_qty"] += qty
        return index

    def _validate_overlay_event(self, event: dict, ctx: dict | None) -> dict:
        approval_id = str(event.get("approval") or event.get("approval_id") or "")
        proof = ctx.get("proof") if ctx else None
        overlays = self._overlays(proof) if proof else []
        overlay = next((o for o in overlays if str(o.get("id")) == approval_id), None)
        reasons = []
        if overlay is None:
            reasons.append("approval_not_found")
        site = str(event.get("site"))
        if overlay and overlay.get("positions") and site not in [str(x) for x in overlay["positions"]]:
            reasons.append("site_outside_overlay_scope")
        position = ctx.get("positions", {}).get(site) if ctx else None
        if overlay and position:
            removed = str(overlay.get("claim_removed"))
            if removed not in [str(c) for c in position.get("claims", [])]:
                reasons.append("claim_not_on_position")
            if proof is not None and overlay.get("issuer"):
                issuer_match = any(
                    str(p.get("claim")) == removed
                    and str(p.get("issuer")) == str(overlay.get("issuer"))
                    and (
                        not overlay.get("market")
                        or str(overlay.get("market")) in [str(m) for m in p.get("markets", [])]
                    )
                    for p in proof.get("permit", proof.get("permits", []))
                )
                if not issuer_match:
                    reasons.append("overlay_issuer_not_approved_for_claim")
        if overlay and event.get("market") and str(event["market"]) != str(overlay.get("market")):
            reasons.append("market_mismatch")
        if overlay and event.get("sku") and str(event["sku"]) != str(overlay.get("sku")):
            reasons.append("sku_mismatch")
        if int(D(event.get("quantity", event.get("qty", 0)))) <= 0:
            reasons.append("quantity_not_positive")
        return {
            "valid": not reasons,
            "reasons": reasons,
            "approval": approval_id,
            "overlay": overlay,
            "event_id": event.get("id"),
            "run": event.get("run"),
            "site": site,
            "quantity": int(D(event.get("quantity", event.get("qty", 0)))),
            "use": (
                {"market": overlay.get("market"), "sku": overlay.get("sku")}
                if overlay and not reasons
                else None
            ),
        }

    def _delivery_expectation(self, batch: dict, stock_index: dict, ctx: dict) -> dict:
        agg = stock_index.get((batch["run"], batch["site"]), {})
        received = int(agg.get("received_qty", 0))
        printed = int(batch["printed_qty"])
        return {
            "run": batch["run"],
            "site": batch["site"],
            "printed_qty": printed,
            "printed_at": batch.get("at"),
            "press_report_id": batch.get("report_id"),
            "received_qty": received,
            "receipt_ids": agg.get("receipt_ids", []),
            "outstanding_receipt_qty": max(0, printed - received),
            "receipt_overrun_qty": max(0, received - printed),
            "stock_status": "received" if received >= printed and printed > 0 else ("partial" if received else "not_received"),
        }

    # ------------------------------------------------------------ warehouse

    def warehouse_report(self, as_of: datetime | str | None = None) -> dict:
        report_at = iso_dt(as_of) if as_of else self._default_as_of()
        press, proof, extra, ctx = self._primary_press_context()
        stock_index = self._stock_index(ctx)
        batches = []
        total = {
            "press_printed_qty": 0,
            "received_qty": 0,
            "issued_qty": 0,
            "consumed_qty": 0,
            "delivered_qty": 0,
            "warehouse_on_hand_qty": 0,
            "valid_overlaid_qty": 0,
            "available_qty": 0,
            "pending_overlay_qty": 0,
            "pending_overlay_including_line_side_qty": 0,
            "blocked_qty": 0,
            "line_side_open_qty": 0,
        }

        keys = set(ctx["printed"].keys()) | set(stock_index.keys())
        for key in sorted(keys):
            run, site = key
            batch = ctx["printed"].get(key)
            agg = stock_index.get(key, self._empty_agg(run, site))
            position = ctx["positions"].get(site, {})
            printed_qty = int(batch["printed_qty"]) if batch else 0
            rejected_qty = int(ctx["waste_qty"].get(key, 0))
            qualified_qty = max(0, printed_qty - rejected_qty)
            received = int(agg["received_qty"])
            issued = int(agg["issued_qty"])
            delivered = int(agg["delivered_qty"])
            on_hand = max(0, received - issued - int(agg["other_out_qty"]))
            line_open = 0
            for order_agg in agg["orders"].values():
                line_open += max(0, int(order_agg["issued_qty"]) - int(order_agg["delivered_qty"]))
            # 已单独标记为领用补贴层的数量不再占可用补贴库存；其余按“补贴完成件优先留在在库”
            # 的可承诺口径匹配。若原始记录没有位置/层级，报表同时给出口径说明。
            overlay_issued = min(int(agg.get("overlay_issued_qty", 0)), int(agg["valid_overlaid_qty"]))
            overlay_unissued = max(0, int(agg["valid_overlaid_qty"]) - overlay_issued)
            overlaid = min(overlay_unissued, on_hand)
            overlay_in_line = max(0, overlay_unissued - overlaid)

            eligibility = self._site_usage_status(proof, position, overlaid_qty=overlaid, at=report_at)
            unoverlaid_on_hand = max(0, on_hand - overlaid)
            original_ready_qty = unoverlaid_on_hand if eligibility["base_current_uses"] else 0
            overlay_ready_qty = overlaid if eligibility["overlay_uses"] else 0
            available_qty = original_ready_qty + overlay_ready_qty
            # 对撤回目标用途，原版已不允许；方案存在时，未完成补贴件属于待改贴。
            if eligibility["overlay_scheme"] and not eligibility["base_current_uses"]:
                pending_overlay = unoverlaid_on_hand
            elif eligibility["overlay_scheme"]:
                pending_overlay = 0
            else:
                pending_overlay = 0
            blocked = max(0, on_hand - available_qty - pending_overlay)

            usable_by_use: dict[str, int] = {}
            for use in eligibility["base_current_uses"]:
                key = f"{use['market']}|{use['sku']}|{'+'.join(use['claims'])}"
                usable_by_use[key] = usable_by_use.get(key, 0) + original_ready_qty
            for use in eligibility["overlay_uses"]:
                key = f"{use['market']}|{use['sku']}|{'+'.join(use['claims'])}"
                usable_by_use[key] = usable_by_use.get(key, 0) + overlay_ready_qty

            overlay_issued_open = min(overlay_issued, line_open)
            line_pending_overlay = max(0, line_open - overlay_in_line - overlay_issued_open)
            pending_including_line = pending_overlay + line_pending_overlay
            basis_warnings = []
            if batch is None:
                basis_warnings.append("stock_or_overlay_record_without_matching_printed_run_site")
            if int(agg.get("invalid_overlay_qty", 0)):
                basis_warnings.append("has_invalid_overlay_record")

            row = {
                "run": run,
                "site": site,
                "plate": press.get("plate"),
                "revision": press.get("revision"),
                "printed_qty": printed_qty,
                "rejected_qty": rejected_qty,
                "qualified_qty": qualified_qty,
                "received_qty": received,
                "receipt_ids": agg["receipt_ids"],
                "issued_qty": issued,
                "consumed_qty": issued,
                "requisitioned_qty": issued,
                "consumed_basis_ids": agg["issue_ids"],
                "issue_ids": agg["issue_ids"],
                "delivered_qty": delivered,
                "delivery_ids": agg["delivery_ids"],
                "valid_overlaid_qty": int(agg["valid_overlaid_qty"]),
                "overlay_issued_qty": overlay_issued,
                "overlay_in_warehouse_qty": overlaid,
                "overlay_in_line_side_qty": overlay_in_line,
                "invalid_overlay_qty": int(agg["invalid_overlay_qty"]),
                "warehouse_on_hand_qty": on_hand,
                "physical_available_qty": on_hand,
                "line_side_open_qty": line_open,
                "available_qty": available_qty,
                "legally_available_qty": available_qty,
                "usable_qty_by_use": usable_by_use,
                "original_ready_qty": original_ready_qty,
                "overlay_ready_qty": overlay_ready_qty,
                "pending_overlay_qty": pending_overlay,
                "pending_relabel_qty": pending_overlay,
                "blocked_unusable_qty": blocked,
                "pending_overlay_including_line_side_qty": pending_including_line,
                "usage": eligibility,
                "basis_events": [e.get("id") for e in agg["events"]],
                "basis_warnings": basis_warnings,
                "overlay_matches": agg["overlay_matches"],
                "overlay_rejections": agg["overlay_rejections"],
                "order_flows": agg["orders"],
                "physical_reconciliation": {
                    "received": received,
                    "warehouse_issued": issued,
                    "warehouse_on_hand": on_hand,
                    "customer_delivered_evidence": delivered,
                    "line_side_open_estimate": line_open,
                    "note": "delivered 通常承接同一订单的 issued，不再作为第二次仓库出库扣减；received-issued 是仓班在库。",
                },
            }
            batches.append(row)
            total["press_printed_qty"] += printed_qty
            total["received_qty"] += received
            total["issued_qty"] += issued
            total["consumed_qty"] += issued
            total["delivered_qty"] += delivered
            total["warehouse_on_hand_qty"] += on_hand
            total["valid_overlaid_qty"] += int(agg["valid_overlaid_qty"])
            total["available_qty"] += available_qty
            total["pending_overlay_qty"] += pending_overlay
            total["pending_overlay_including_line_side_qty"] += pending_including_line
            total["blocked_qty"] += blocked
            total["line_side_open_qty"] += line_open

        # 给销售用途汇总可承诺量。
        available_by_use: dict[str, int] = {}
        for row in batches:
            for key, qty in row.get("usable_qty_by_use", {}).items():
                available_by_use[key] = available_by_use.get(key, 0) + qty
        return {
            "batches": batches,
            "totals": total,
            "available_by_use": available_by_use,
            "available_by_use_note": "同一批原版通用袋可能同时符合多个用途；各 use 是可承诺视图，不能跨行相加。",
            "stock_rule": "只有 received 形成库存；相同事件 id 或相同 (run,site) 完工回执不重复计数。",
        }

    def _empty_agg(self, run: str, site: str) -> dict:
        return {
            "run": run,
            "site": site,
            "received_qty": 0,
            "issued_qty": 0,
            "consumed_qty": 0,
            "delivered_qty": 0,
            "valid_overlaid_qty": 0,
            "invalid_overlay_qty": 0,
            "events": [],
            "receipt_ids": [],
            "issue_ids": [],
            "delivery_ids": [],
            "overlay_matches": [],
            "overlay_rejections": [],
            "orders": {},
            "other_out_qty": 0,
        }

    def _site_usage_status(self, proof: dict | None, position: dict, overlaid_qty: int, at: datetime | None) -> dict:
        claims = list(position.get("claims", []))
        if proof is None:
            return {
                "site": str(position.get("site", "")),
                "base_claims": claims,
                "base_current_uses": [],
                "base_historical_uses": [],
                "base_withdrawn": False,
                "overlay_scheme": None,
                "overlay_uses": [],
                "available_uses": [],
                "available_qty": 0,
                "hold_reasons": ["unknown_plate_revision"],
            }
        base_uses = self.allowed_uses(proof, claims, at=at)
        wd = self._withdrawal(proof)
        effective = iso_dt(wd.get("effective")) if wd else None
        historical_at = effective - timedelta(seconds=1) if effective else at
        historical_uses = self.allowed_uses(proof, claims, at=historical_at)
        base_withdrawn = bool(
            wd
            and str(wd.get("claim")) in claims
            and (
                not historical_uses
                or any(str(u.get("market")) == str(wd.get("market")) for u in historical_uses)
            )
        )
        site = str(position.get("site"))
        scheme = next(
            (
                o
                for o in self._overlays(proof)
                if site in [str(x) for x in o.get("positions", [])]
            ),
            None,
        )
        overlay_uses = []
        if scheme:
            new_claims = [c for c in claims if c != str(scheme.get("claim_removed"))]
            uses = self.allowed_uses(proof, new_claims, at=at)
            for use in uses:
                if use["market"] == str(scheme.get("market")) and use["sku"] == str(scheme.get("sku")):
                    use["overlay_id"] = scheme.get("id")
                    overlay_uses.append(use)
        available_uses = list(base_uses)
        if overlay_uses and overlaid_qty > 0:
            available_uses.extend(overlay_uses)
        return {
            "site": site,
            "base_claims": claims,
            "base_current_uses": base_uses,
            "base_historical_uses": historical_uses,
            "base_withdrawn": bool(wd and str(wd.get("claim")) in claims),
            "overlay_scheme": scheme,
            "overlay_uses": overlay_uses,
            "available_uses": available_uses,
            "available_qty": None,
        }

    # ------------------------------------------------------------- allocation

    def order_availability(self, order: dict | str, requested_qty: int | None = None) -> dict:
        if isinstance(order, str):
            order = self.orders[order]
        market = str(order["market"])
        sku = str(order["sku"])
        qty = int(requested_qty if requested_qty is not None else order.get("quantity") or 0)
        delivery_date = order.get("delivery_date") or order.get("expected_delivery")
        ddate = date.fromisoformat(str(delivery_date)[:10]) if delivery_date else None
        at = iso_dt(order.get("requested_at") or order.get("at"))
        if at is None and ddate is None:
            at = self._default_as_of()

        warehouse = self.warehouse_report()
        sources = []
        available_total = 0
        for row in warehouse["batches"]:
            on_hand = int(row["warehouse_on_hand_qty"])
            if on_hand <= 0:
                continue
            proof = self.proofs.get(ProofRef(str(row.get("plate")), row.get("revision")))
            if proof is None and self.proof_order:
                proof = self.proofs[self.proof_order[0]]
            claims = list(row.get("usage", {}).get("base_claims", []))
            can_original = False
            if proof is not None:
                can_original = any(
                    u["market"] == market and u["sku"] == sku
                    for u in self.allowed_uses(proof, claims, at=at, delivery_date=ddate)
                )

            matching_overlay_qty = 0
            overlay_basis_ids: list[Any] = []
            for match in row.get("overlay_matches", []):
                scheme = match.get("overlay") or {}
                scope_markets = scheme.get("markets") or ([scheme.get("market")] if scheme.get("market") else [])
                scope_skus = scheme.get("skus") or ([scheme.get("sku")] if scheme.get("sku") else [])
                if market not in [str(x) for x in scope_markets]:
                    continue
                if sku not in [str(x) for x in scope_skus]:
                    continue
                if proof is not None:
                    new_claims = [c for c in claims if c != str(scheme.get("claim_removed"))]
                    overlay_ok = any(
                        u["market"] == market and u["sku"] == sku
                        for u in self.allowed_uses(proof, new_claims, at=at, delivery_date=ddate)
                    )
                    if not overlay_ok:
                        continue
                matching_overlay_qty += int(match.get("quantity", 0))
                overlay_basis_ids.append(match.get("event_id"))
            matching_overlay_qty = min(matching_overlay_qty, int(row["overlay_in_warehouse_qty"]))

            q = 0
            basis = []
            if can_original:
                original_qty = max(0, on_hand - int(row["overlay_in_warehouse_qty"]))
                q += original_qty
                if original_qty:
                    basis.append("original_allowed_combination")
            if matching_overlay_qty:
                q += matching_overlay_qty
                basis.append("approved_overlay_and_completion_record")
            q = min(q, on_hand)
            if q:
                sources.append(
                    {
                        "run": row["run"],
                        "site": row["site"],
                        "available_qty": q,
                        "original_available_qty": q - matching_overlay_qty if "original_allowed_combination" in basis else 0,
                        "overlay_available_qty": matching_overlay_qty,
                        "warehouse_on_hand_qty": on_hand,
                        "basis": basis,
                        "basis_ids": row["overlay_matches"],
                        "overlay_completion_ids": overlay_basis_ids,
                    }
                )
                available_total += q
        allocation = []
        remaining = qty
        for src in sorted(sources, key=lambda x: (x["run"], x["site"])):
            take = min(src["available_qty"], remaining) if qty else src["available_qty"]
            if take <= 0:
                continue
            allocation.append({**src, "allocate_qty": take})
            if qty:
                remaining -= take
                if remaining <= 0:
                    break
        return {
            "order_id": order.get("id"),
            "market": market,
            "sku": sku,
            "delivery_date": str(ddate) if ddate else None,
            "requested_qty": qty or None,
            "currently_available_qty": available_total,
            "shortfall_qty": max(0, qty - available_total) if qty else None,
            "can_fulfill": available_total >= qty if qty else None,
            "sources": sources,
            "allocation": allocation,
            "rule": "销售订单按 market+sku+预计交付日核对许可、组合与撤回；审批本身不把未补贴实物转为新版。",
        }

    def issue_for_order(
        self,
        order: dict | str,
        quantity: int | None = None,
        *,
        at: str | datetime | None = None,
        allow_partial: bool = False,
        commit: bool = True,
        idempotency_key: str | None = None,
    ) -> dict:
        """按 market/sku/交付日分配并（可选）登记仓班 issued 事件。"""
        if isinstance(order, str):
            order = self.orders[order]
        elif order.get("id"):
            self.receive_order(order)
        issue_at = iso_dt(at).isoformat() if at else self._default_as_of().isoformat()
        idem_key = idempotency_key or f"{order.get('id')}:{quantity}:{issue_at}"
        if idem_key in self.issue_records:
            return {**self.issue_records[idem_key], "idempotent_replay": True}
        availability = self.order_availability(order, quantity)
        qty = quantity if quantity is not None else int(order.get("quantity") or 0)
        if qty and availability["shortfall_qty"] and not allow_partial:
            raise ServiceError(
                f"订单 {order.get('id')} 缺料 {availability['shortfall_qty']}，不能整单领用"
            )
        issued_events = []
        for alloc in availability["allocation"]:
            overlay_qty = int(alloc.get("overlay_available_qty") or 0)
            allocated_overlay_qty = min(int(alloc["allocate_qty"]), overlay_qty)
            event = {
                "id": f"ISS-{order.get('id')}-{alloc['run']}-{alloc['site']}",
                "run": alloc["run"],
                "site": alloc["site"],
                "kind": "issued",
                "quantity": alloc["allocate_qty"],
                "order": order.get("id"),
                "at": issue_at,
                "basis": alloc["basis"],
                "overlay_issued_qty": allocated_overlay_qty,
                "layer": "overlaid" if allocated_overlay_qty == alloc["allocate_qty"] else "mixed",
            }
            issued_events.append(event)
            if commit:
                self.receive_stock_event(event)
        availability["issued_events"] = issued_events
        availability["committed"] = commit
        if commit:
            self.issue_records[idem_key] = {k: v for k, v in availability.items() if k != "idempotent_replay"}
        return availability

    # -------------------------------------------------------------- finance

    def finance_report(self) -> dict:
        press, proof, extra, ctx = self._primary_press_context()
        rate = D(press.get("fee_per_impression", press.get("unit_price", "0")))
        cancel_fee = D(
            press.get(
                "confirmed_cancel_fee_per_position",
                press.get("cancellation_fee_per_position", "0"),
            )
        )
        currency = press.get("currency", "CNY")
        lines = []
        printed_total = Decimal("0.00")
        payable_total = Decimal("0.00")
        qualified_total = 0
        unknown_rev = proof is None
        for key, batch in sorted(ctx["printed"].items()):
            rejected = int(ctx["waste_qty"].get(key, 0))
            qualified = max(0, int(batch["printed_qty"]) - rejected)
            amount = money(D(qualified) * rate)
            printed_total += amount
            qualified_total += qualified
            reconciliation_ok = batch.get("reconciliation") != "conflicting_same_batch_reconciled_to_latest"
            payable = not unknown_rev and reconciliation_ok
            if payable:
                payable_total += amount
            reasons = []
            if unknown_rev:
                reasons.append("unknown_plate_revision")
            if not reconciliation_ok:
                reasons.append("conflicting_duplicate_count_needs_correction")
            lines.append(
                {
                    "kind": "printed_qualified_pieces",
                    "run": batch["run"],
                    "site": batch["site"],
                    "source_report_id": batch.get("report_id"),
                    "source_report_ids": batch.get("report_ids"),
                    "printed_qty": batch["printed_qty"],
                    "rejected_qty": rejected,
                    "qualified_qty": qualified,
                    "unit_price": money(rate),
                    "currency": currency,
                    "amount": amount,
                    "payable": payable,
                    "hold_reasons": reasons,
                    "basis": "same run/site receipts are one physical batch; pay qualified pieces, not received stock and not withdrawal-deleted quantities",
                }
            )

        cancel_total = Decimal("0.00")
        payable_cancel_total = Decimal("0.00")
        for site, ev in sorted(ctx["confirmed_stop_sites"].items()):
            # 如果确认事件自带金额，以合同/回执金额为准；否则按位置单价。
            source_report = next((r for r in press.get("reports", []) if r.get("id") == ev.get("id")), {})
            fee = D(source_report.get("fee", source_report.get("amount", cancel_fee)))
            amount = money(fee)
            cancel_total += amount
            payable = not unknown_rev
            if payable:
                payable_cancel_total += amount
            lines.append(
                {
                    "kind": "confirmed_cancelled_position",
                    "site": site,
                    "source_report_id": ev.get("id"),
                    "confirmed_at": ev.get("at"),
                    "quantity_basis": "position",
                    "unit_fee": money(fee),
                    "currency": currency,
                    "amount": amount,
                    "payable": payable,
                    "hold_reasons": ["unknown_plate_revision"] if unknown_rev else [],
                    "basis": "stop_confirmed per position; pending stop is not payable as cancellation",
                }
            )
        payable_total += payable_cancel_total
        total = money(payable_total)
        return {
            "purchase_order": press.get("order"),
            "currency": currency,
            "fee_per_impression": money(rate),
            "confirmed_cancel_fee_per_position": money(cancel_fee),
            "lines": jsonable(lines),
            "printed_qualified_qty": qualified_total,
            "printed_subtotal": money(printed_total),
            "cancellation_subtotal": money(cancel_total),
            "payable_total": total,
            "payable_items": [
                {
                    "kind": line["kind"],
                    "run": line.get("run"),
                    "site": line.get("site"),
                    "source_report_id": line.get("source_report_id"),
                    "qualified_qty": line.get("qualified_qty"),
                    "amount": line["amount"],
                }
                for line in lines
                if line.get("payable")
            ],
            "rule": "确实印制的合格件与经确认按位置取消项可付；客户撤回不冲销已印实物，重复回执不重复结算。",
        }

    # --------------------------------------------------------- certificates

    def certificates_report(self) -> list[dict]:
        press, proof, extra, ctx = self._primary_press_context()
        if proof is None:
            return []
        stock_index = self._stock_index(ctx)
        certs = []
        cert_no = 0
        for event in sorted(self._canonical_stock_events(), key=lambda e: iso_dt(e.get("at")) or datetime.min.replace(tzinfo=CN_TZ)):
            if str(event.get("kind", "")).lower() not in DELIVERED_KINDS:
                continue
            run, site = str(event.get("run")), str(event.get("site"))
            batch = ctx["printed"].get((run, site))
            if batch is None:
                continue
            at = iso_dt(event.get("at"))
            claims = list(ctx["positions"].get(site, {}).get("claims", []))
            uses_then = self.allowed_uses(proof, claims, at=at)
            wd = self._withdrawal(proof)
            before_withdrawal = bool(wd and iso_dt(wd.get("effective")) and at and at < iso_dt(wd["effective"]))
            cert_no += 1
            certs.append(
                {
                    "certificate_id": f"CERT-{event.get('id', cert_no)}",
                    "delivery_id": event.get("id"),
                    "order_id": event.get("order"),
                    "plate": press.get("plate"),
                    "revision": press.get("revision"),
                    "run": run,
                    "site": site,
                    "quantity": int(D(event.get("quantity", 0))),
                    "delivered_at": event.get("at"),
                    "printed_report_id": batch.get("report_id"),
                    "printed_at": batch.get("at"),
                    "label_claims_at_time": claims,
                    "applicable_uses_at_time": uses_then,
                    "before_withdrawal_effective": before_withdrawal,
                    "certifiable": before_withdrawal and bool(uses_then),
                    "statement": (
                        f"该 {int(D(event.get('quantity', 0)))} 件交付发生在撤回生效前，"
                        f"证明依据为 {press.get('plate')} 第 {press.get('revision')} 版 / {run} / {site}，"
                        "不得用后续 OV9 遮盖记录改写其当时版号。"
                        if before_withdrawal and uses_then
                        else "交付时点的版稿组合或许可已不成立，不能出具旧版有效证明。"
                    ),
                    "basis_record_ids": [event.get("id"), batch.get("report_id")],
                }
            )
        return certs

    # ------------------------------------------------------------- all output

    def report(self) -> dict:
        press, proof, _, _ = self._primary_press_context()
        return {
            "proof": self.proof_report(proof, allow_default=False),
            "print_approval": self.approve_print_order(),
            "purchasing": self.purchasing_report(),
            "warehouse": self.warehouse_report(),
            "finance": self.finance_report(),
            "certificates": self.certificates_report(),
            "order_inquiry_n_p1": self.order_availability(
                {"id": "INQUIRY-N-P1", "market": "N", "sku": "P1", "delivery_date": "2026-12-10"}
            ),
            "errors": self.errors,
        }


# Common concise aliases for callers/tests written against functional wording.
CommissionPrintingService.withdrawal_report = CommissionPrintingService.purchasing_report
CommissionPrintingService.purchaser_report = CommissionPrintingService.purchasing_report
CommissionPrintingService.stock_report = CommissionPrintingService.warehouse_report
CommissionPrintingService.inventory_report = CommissionPrintingService.warehouse_report
CommissionPrintingService.settlement_report = CommissionPrintingService.finance_report
CommissionPrintingService.certificate_report = CommissionPrintingService.certificates_report

CommissionPrintService = CommissionPrintingService
PrintShopService = CommissionPrintingService
PackagingService = CommissionPrintingService


def build_report(inputs_dir: str | Path | None = None) -> dict:
    return load_service(inputs_dir).report()


def purchasing_view(inputs_dir: str | Path | None = None) -> dict:
    return load_service(inputs_dir).purchasing_report()


def warehouse_view(inputs_dir: str | Path | None = None) -> dict:
    return load_service(inputs_dir).warehouse_report()


def finance_view(inputs_dir: str | Path | None = None) -> dict:
    return load_service(inputs_dir).finance_report()


def certificates_view(inputs_dir: str | Path | None = None) -> list[dict]:
    return load_service(inputs_dir).certificates_report()


withdrawal_view = purchasing_view
purchaser_view = purchasing_view
stock_view = warehouse_view
inventory_view = warehouse_view
settlement_view = finance_view
certificate_view = certificates_view


def render_markdown(report: dict) -> str:
    """生成给采购员、仓班和财务阅读的中文核对单。"""
    report = jsonable(report)
    p = report["purchasing"]
    w = report["warehouse"]
    f = report["finance"]
    lines = ["# 委印服务核对结果", ""]

    lines += ["## 采购员：撤回后的应停与应交", ""]
    wd = p.get("withdrawal") or {}
    lines.append(
        f"撤回 `{wd.get('id', '-')}`：{wd.get('market', '-')} / `{wd.get('claim', '-')}`，"
        f"生效时间 {wd.get('effective', '-')}。影响位置：{', '.join(p.get('affected_sites', [])) or '无'}。"
    )
    lines.append("")
    lines.append("| 位置 | 状态 | 生效前已印 | 生效后下机 | 停版确认 | 后续可能入库 |")
    lines.append("|---|---|---:|---:|---|---|")
    for s in p.get("sites", []):
        before = sum(x["printed_qty"] for x in s["printed_before_effective"])
        after = sum(x["printed_qty"] for x in s["printed_after_effective"])
        lines.append(
            f"| {s['site']} | {s['stop_state']} | {before} | {after} | "
            f"{s.get('stop_confirmed_at') or '未确认'} | {'是' if s['possible_future_delivery_without_final_stop_or_completion'] else '否'} |"
        )
    lines.append("")
    lines.append("已印但尚待收货：")
    for x in p.get("answer", {}).get("印好待收", []):
        lines.append(f"- `{x['run']}` / {x['site']}：待收 {x['outstanding_qty']}")
    if not p.get("answer", {}).get("印好待收"):
        lines.append("- 无")
    lines.append("")

    lines += ["## 仓班：按印次/位置的数量与依据", ""]
    lines.append(
        "| 印次 | 位置 | 下机 | 实收 | 已领 | 已交付凭据 | 在库 | 已批准补贴 | 现可用 | 待改贴 | 不可用 |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for b in w.get("batches", []):
        lines.append(
            f"| {b['run']} | {b['site']} | {b['printed_qty']} | {b['received_qty']} | "
            f"{b['issued_qty']} | {b['delivered_qty']} | {b['warehouse_on_hand_qty']} | "
            f"{b['valid_overlaid_qty']} | {b['available_qty']} | {b['pending_overlay_qty']} | "
            f"{b['blocked_unusable_qty']} |"
        )
    t = w.get("totals", {})
    lines.append("")
    lines.append(
        f"合计：实收 {t.get('received_qty', 0)}，仓库在库 {t.get('warehouse_on_hand_qty', 0)}，"
        f"已领未交付的线边估算 {t.get('line_side_open_qty', 0)}，现可用 {t.get('available_qty', 0)}，"
        f"仓内待改贴 {t.get('pending_overlay_qty', 0)}；含线边未转换件的待改贴总量 "
        f"{t.get('pending_overlay_including_line_side_qty', 0)}，仓班已领用/耗用 {t.get('consumed_qty', 0)}，"
        f"客户交付凭据 {t.get('delivered_qty', 0)}。"
    )
    lines.append("")
    lines.append("按用途可承诺：")
    for use, qty in w.get("available_by_use", {}).items():
        lines.append(f"- `{use}`：{qty}")
    lines.append("")

    lines += ["## 财务：本次印单可付项目", ""]
    lines.append(f"币种：{f.get('currency')}；合格印件总量：{f.get('printed_qualified_qty')}。")
    lines.append("")
    lines.append("| 类型 | 印次 | 位置 | 凭据 | 数量基础 | 金额 | 可付 |")
    lines.append("|---|---|---|---|---|---:|---|")
    for line in f.get("lines", []):
        qty_basis = line.get("qualified_qty", line.get("quantity_basis", ""))
        ref = line.get("source_report_id", "")
        lines.append(
            f"| {line['kind']} | {line.get('run') or ''} | {line.get('site') or ''} | "
            f"{ref} | {qty_basis} | {line['amount']} | {'是' if line['payable'] else '暂不付'} |"
        )
    lines.append("")
    lines.append(f"**应付合计：{f.get('payable_total')} {f.get('currency')}**")
    lines.append("")

    lines += ["## 客户交付证明", ""]
    for c in report.get("certificates", []):
        mark = "可证明" if c["certifiable"] else "不可按旧版证明"
        lines.append(
            f"- {c['certificate_id']}（{c['order_id']} / {c['run']} / {c['site']} / {c['quantity']} 件，{mark}）：{c['statement']}"
        )
    lines.append("")
    if report.get("errors"):
        lines += ["## 数据异常", ""]
        for e in report["errors"]:
            lines.append(f"- `{e.get('type')}` {e}")
    return "\n".join(lines)


def write_reports(inputs_dir: str | Path | None = None, out_dir: str | Path = "reports") -> dict:
    service = load_service(inputs_dir)
    report = jsonable(service.report())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "all.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, key in (
        ("proof", "proof"),
        ("purchasing", "purchasing"),
        ("warehouse", "warehouse"),
        ("finance", "finance"),
        ("certificates", "certificates"),
    ):
        (out / f"{name}.json").write_text(
            json.dumps(report[key], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def generate_reports(inputs_dir: str | Path | None = None, out_dir: str | Path = "reports") -> dict:
    return write_reports(inputs_dir, out_dir)


def solve(inputs_dir: str | Path | None = None, out_dir: str | Path = "reports") -> dict:
    return write_reports(inputs_dir, out_dir)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="拼版委印、撤回、仓班领用与结算服务")
    parser.add_argument("--inputs", default=str(Path(__file__).resolve().parent / "inputs"))
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "reports"))
    parser.add_argument("--stdout", action="store_true", help="输出 all.json 到标准输出")
    args = parser.parse_args(argv)
    report = write_reports(args.inputs, args.out)
    if args.stdout:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"已写入报告目录：{Path(args.out).resolve()}")
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
