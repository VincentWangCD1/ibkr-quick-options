"""Order rows and trade recaps built from the selected account's execution ledger."""
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


def dec(value):
    return Decimal(str(value))


def is_option(row):
    return row.get("sec_type") in ("OPT", "FOP") and bool(row.get("conid"))


def order_key(row, links):
    """Keep unknown orders separate. Similar timestamps and prices are not order IDs."""
    execution = row["exec_id"]
    scope = (row.get("conid"), row.get("currency"), row.get("action"))
    if is_option(row):
        permanent = str(row.get("perm_id") or "")
        if permanent.isdecimal() and int(permanent) > 0:
            return (*scope, "perm", permanent)
        if row.get("source") == "FLEX":
            broker_id = str(row.get("order_id") or "")
            if broker_id.isdecimal() and int(broker_id) > 0:
                return links.get((*scope, "flex", broker_id), (*scope, "flex", broker_id))
        if row.get("source") == "DEMO":
            parts = execution.split(":")
            if len(parts) == 4 and parts[0] == "DEMO" and all(parts[1:]):
                return (*scope, "demo", parts[1], parts[2])
    return ("fill", execution)


def sum_known(rows, field, currency_field=None):
    if any(row.get(field) is None for row in rows):
        return None
    if currency_field and len({r.get(currency_field) or r["currency"] for r in rows}) != 1:
        return None
    return float(sum(dec(r[field]) for r in rows))


def order_rows(rows):
    # The same execution received via Flex and TWS can link their order ID namespaces.
    aliases = defaultdict(set)
    for row in rows:
        if row.get("source") == "FLEX" and row.get("order_id") and row.get("perm_id"):
            scope = (row.get("conid"), row.get("currency"), row.get("action"))
            perm = str(row["perm_id"])
            if perm.isdecimal() and int(perm) > 0:
                aliases[(*scope, "flex", str(row["order_id"]))].add((*scope, "perm", perm))
    links = {key: next(iter(ids)) for key, ids in aliases.items() if len(ids) == 1}
    by_order = defaultdict(list)
    for row in rows:
        by_order[order_key(row, links)].append(row)
    orders = {}
    for key, fills in by_order.items():
        fills.sort(key=lambda r: (r["timestamp"], r["exec_id"]), reverse=True)
        order = dict(fills[0], key=key, fills=fills)
        if len(fills) > 1:
            qty = sum(dec(r["quantity"]) for r in fills)
            order.update(quantity=float(qty), price=float(sum(dec(r["quantity"]) * dec(r["price"]) for r in fills) / qty),
                         gross=sum_known(fills, "gross"), commission=sum_known(fills, "commission", "commission_currency"),
                         realized_pnl=sum_known(fills, "realized_pnl", "pnl_currency"))
        orders[key] = order
    return orders


def unit_fee(row):
    if row.get("commission") is not None and row.get("commission_currency") == row["currency"]:
        return dec(row["commission"]) / dec(row["quantity"])
    return None


def closing_trades(records, displayed, timezone):
    """Allocate each opening fill once, including openings before the visible range."""
    visible = {r["exec_id"] for r in displayed if is_option(r)}
    contracts = {(r["conid"], r["currency"]) for r in displayed if is_option(r)}
    lots_by_contract = defaultdict(deque)
    results = {}
    zone = ZoneInfo(timezone)

    def stamp(row):
        return datetime.fromisoformat(row["timestamp"]).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")

    ordered = sorted((r for r in records if (r["conid"], r["currency"]) in contracts),
                     key=lambda r: (r["timestamp"], r["exec_id"]))
    for row in ordered:
        qty = dec(row["quantity"])
        if qty <= 0:
            continue
        demo_session = row["exec_id"].split(":")[1:2] if row.get("source") == "DEMO" else []
        lots = lots_by_contract[row["conid"], row["currency"], tuple(demo_session)]
        action = row["action"]
        marker = str(row.get("open_close") or "").upper()
        reported = row.get("realized_pnl")
        flags = {flag.strip() for flag in marker.replace(",", ";").split(";")}
        reversal = {"O", "C"}.issubset(flags)
        known_close = "C" in flags or (marker != "O" and reported not in (0, None))
        if marker == "O" and lots and lots[0][0]["action"] != action:
            lots.clear()  # An explicit opening proves earlier inventory is incomplete.
        legs = []
        while qty and lots and lots[0][0]["action"] != action:
            opening, available = lots[0]
            count = min(qty, available)
            legs.append((opening, count))
            qty -= count
            if available == count:
                lots.popleft()
            else:
                lots[0][1] -= count
        if qty and (not known_close or (legs and reversal)):
            lots.append([row, qty])
        if row["exec_id"] not in visible or not (legs or known_close):
            continue
        # Do not quote a guessed entry for a close that extends beyond recorded inventory.
        incomplete = known_close and qty > 0 and not (legs and reversal)
        count = dec(row["quantity"]) if incomplete else sum(n for _, n in legs)
        closing_time = stamp(row)
        result = dict(action=action, quantity=float(count), close_price=row["price"],
                      close_first=closing_time, close_last=closing_time, timezone=timezone,
                      reported=reported if row.get("pnl_currency", row["currency"]) == row["currency"] else None)
        if legs and not incomplete and row.get("multiplier") and all(
                r.get("multiplier") == row["multiplier"] and r["price"] > 0 for r, _ in legs):
            entry_value = sum(dec(r["price"]) * n for r, n in legs)
            basis = entry_value * dec(row["multiplier"])
            gross = (dec(row["price"]) * count - entry_value) * dec(row["multiplier"])
            if action == "BUY":
                gross = -gross
            fees = [(unit_fee(r), n) for r, n in legs]
            exit_fee = unit_fee(row)
            full_fees = exit_fee is not None and all(f is not None for f, _ in fees)
            entry_fees = sum(f * n for f, n in fees) if full_fees else 0
            net = gross - entry_fees - exit_fee * count if full_fees else None
            times = [stamp(r) for r, _ in legs]
            # IBKR may have a different opening lot, or older executions may be missing.
            # Preserve its realized amount and omit the unconfirmed entry in that case.
            if net is None or result["reported"] is None or abs(net - dec(result["reported"])) <= dec(".02"):
                result.update(open_price=float(entry_value / count), open_first=min(times), open_last=max(times),
                              premium=float(basis), basis=float(basis + entry_fees), gross=float(gross),
                              net=float(net) if net is not None else None)
        if result.get("gross") is not None or result["reported"] is not None:
            results[row["exec_id"]] = result
    return results


def trade_recap(fills, results):
    trades = [results.get(row["exec_id"]) for row in fills]
    if not trades or any(t is None for t in trades):
        return None
    qty = sum(dec(t["quantity"]) for t in trades)
    def weighted(field):
        return float(sum(dec(t[field]) * dec(t["quantity"]) for t in trades) / qty)
    recap = dict(action=trades[0]["action"], quantity=float(qty), timezone=trades[0]["timezone"],
                 close_price=weighted("close_price"), close_first=min(t["close_first"] for t in trades),
                 close_last=max(t["close_last"] for t in trades))
    all_open = all(t.get("open_price") is not None for t in trades)
    if all_open:
        recap.update(open_price=weighted("open_price"), open_first=min(t["open_first"] for t in trades),
                     open_last=max(t["open_last"] for t in trades))
    if all(t["reported"] is not None for t in trades):
        pnl = sum(dec(t["reported"]) for t in trades)
        gross = False
    elif all(t.get("net") is not None or t["reported"] is not None for t in trades):
        pnl = sum(dec(t["reported"] if t["reported"] is not None else t["net"]) for t in trades)
        gross = False
    elif all_open:
        pnl = sum(dec(t["gross"]) for t in trades)
        gross = True
    else:
        return None
    basis = sum(dec(t["premium" if gross else "basis"]) for t in trades) if all_open else 0
    recap.update(pnl=float(pnl), percent=float(100 * pnl / basis) if basis else None, before_fees=gross)
    return recap
