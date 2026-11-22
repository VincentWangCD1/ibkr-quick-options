"""Fresh, request-scoped account funds for ib_async 2.1.

No cached accountValues/accountSummary data is used for order sizing. TWS's
per-currency Currency/BASE row describes a ledger bucket, not the account's
base currency. Aggregate monetary summary currencies supply that information.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import math
import re
import time
from zoneinfo import ZoneInfo


SUMMARY_TAGS = "AccountType,NetLiquidation,TotalCashValue,SettledCash,AvailableFunds,$LEDGER:USD"


def _unprefixed(tag):
    return tag.removeprefix("$LEDGER-")


def _settlement_shapes(rows):
    """Expose wire structure only, never cash values, dates, or identifiers.

    Empty-currency strings cannot be interpreted as USD merely because the
    account's base currency is USD. A masked shape allows a later regression
    fixture to use the actual encoding without printing account contents.
    """
    currencies = {"USD", "EUR", "GBP", "HKD", "JPY", "CAD", "AUD", "CHF", "SGD", "CNH", "CNY",
                  "NZD", "KRW", "INR", "MXN", "SEK", "NOK", "DKK", "ZAR", "ILS", "TRY", "PLN",
                  "HUF", "CZK", "BRL", "AED", "SAR", "TWD", "THB", "BASE"}
    shapes = set()
    for tag, value, currency in rows:
        if _unprefixed(tag) not in ("SettledCashByDate", "SettledCashByDate-S", "SettledCashByDate-C", "SettledCashByDate-P"):
            continue
        raw = str(value)
        def mask(match):
            token = match.group()
            if token in currencies:
                return token
            if re.fullmatch(r"(?:[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2})", token):
                return "<DATE>"
            if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", token):
                return "<N>"
            return "<TEXT>"
        shape = re.sub(r"[A-Za-z0-9_.+-]+", mask, raw[:4096])
        shape = re.sub(r"\s+", " ", shape)
        # Any unrecognised text (including Unicode identifiers) stays hidden.
        shape = re.sub(r"[^A-Za-z<> {}\[\]():;,=|\"'/\\]", "?", shape)
        shape = shape[:240] + ("..." if len(shape) > 240 or len(raw) > 4096 else "")
        unit = currency if currency in currencies else ("<empty>" if not currency else "<other>")
        shapes.add(f"{tag}/{unit}: {shape or '<empty>'}")
    return sorted(shapes)


def _legacy_settlement_rows(summary_rows, cash_rows, complete, request_error):
    """Narrow compatibility for observed older TWS account-level schedules.

    TWS server 178 was observed returning plain SettledCashByDate(-S) with an
    empty unit while the same completed download used $LEDGER- for currency
    entries. With that setting, IBKR documents that plain keys are account
    aggregates and ledger keys carry the prefix:
    https://www.interactivebrokers.com/docs/third-party-integrations/tws-settings/per-currency-account-value-prefix
    Aggregate/segment balances are stated in account base currency:
    https://www.ibkrguides.com/reportingreference/reportguide/marginsummarymarginaccounts.htm

    The inference applies only to fresh aggregate schedules, with independent
    USD base evidence and a complete USD-only cash ledger. It never repairs an
    unknown ledger unit or treats an arbitrary empty currency as USD.
    """
    eligible_tags = ("SettledCashByDate", "SettledCashByDate-S")
    if not any(tag in eligible_tags and currency == "" for tag, _, currency in cash_rows):
        return cash_rows, False, ""

    def valid(value):
        try:
            number = float(value)
            return math.isfinite(number) and abs(number) < 1e15
        except (ValueError, TypeError, OverflowError):
            return False

    reasons = []
    if not complete or request_error:
        reasons.append("资金下载未完整结束")
    for tag in ("NetLiquidation", "AvailableFunds"):
        matches = [(value, currency) for key, value, currency in summary_rows if key == tag]
        if not matches or any(currency != "USD" or not valid(value) for value, currency in matches):
            reasons.append(f"缺少独立有效的 {tag}/USD 基础币种证据")
    readiness = {str(value).strip().lower() for tag, value, _ in cash_rows if tag == "AccountReady"}
    if not readiness or readiness - {"true", "1"}:
        reasons.append("同次资金下载未明确 AccountReady=true")
    prefixed_usd = [value for tag, value, currency in cash_rows
                    if tag == "$LEDGER-CashBalance" and currency == "USD"]
    if not prefixed_usd or not all(valid(value) for value in prefixed_usd):
        reasons.append("未取得同次有效的 $LEDGER-CashBalance/USD，无法确认账本前缀模式")
    cash_units = {currency for tag, _, currency in cash_rows if _unprefixed(tag) == "CashBalance"}
    if cash_units - {"USD", "BASE"}:
        reasons.append("同次现金账本含其他币种或未知单位，不采用旧版空单位结算额")
    for tag, _, currency in cash_rows:
        if _unprefixed(tag) not in eligible_tags:
            continue
        if tag.startswith("$LEDGER-") and currency == "":
            reasons.append("账本结算字段单位为空，不能按账户基础币种解释")
        elif currency not in ("", "USD"):
            reasons.append("结算字段混有其他币种或 BASE 单位，不能确认旧版空单位含义")
    if reasons:
        return cash_rows, False, "旧版结算字段币种未确认：" + "；".join(dict.fromkeys(reasons))
    normalized = [(tag, value, "USD" if tag in eligible_tags and currency == "" else currency)
                  for tag, value, currency in cash_rows]
    return normalized, True, ""


def _settlement_by_date(rows, today):
    """Read cumulative USD settlements, never add ledger/segment balances.

    Future credits cannot fund today's order. Future debits additionally
    lower the spending cap, even when an intervening credit would cover them.
    Each scope is independent: securities and total can legitimately differ.
    """
    schedules = {}
    for tag, value, currency in rows:
        scope = _unprefixed(tag)
        if currency != "USD" or scope not in ("SettledCashByDate", "SettledCashByDate-S"):
            continue
        schedule = schedules.setdefault(scope, {})
        try:
            for item in str(value).split(";"):
                match = re.fullmatch(r"([0-9]{8}):([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)", item.strip())
                if not match:
                    raise ValueError
                raw_day, raw_amount = match.groups()
                day = datetime.strptime(raw_day, "%Y%m%d").date()
                amount = float(raw_amount)
                if not math.isfinite(amount) or abs(amount) >= 1e15:
                    raise ValueError
                if day in schedule and schedule[day] != amount:
                    raise ValueError
                schedule[day] = amount
        except (TypeError, ValueError, OverflowError):
            return None, None, "", "", "SettledCashByDate/USD 包含无效日期、金额或同日冲突值"
    if not schedules:
        return None, None, "", "", ""
    current_by_scope = {}
    caps = []
    for scope, schedule in schedules.items():
        past_days = [day for day in schedule if day <= today]
        if not past_days:
            return None, None, "", "", "SettledCashByDate/USD 只有未来结算日期，当前已结算现金未知"
        current_day = max(past_days)
        current = schedule[current_day]
        cap = previous = current
        for day in sorted(day for day in schedule if day > today):
            amount = schedule[day]
            cap += min(0.0, amount - previous)
            previous = amount
        current_by_scope[scope] = (current, current_day)
        caps.append(cap)
    primary = "SettledCashByDate" if "SettledCashByDate" in current_by_scope else "SettledCashByDate-S"
    current, as_of = current_by_scope[primary]
    return current, min(caps), " + ".join(sorted(schedules)) + "/USD", as_of.isoformat(), ""


def financial_view(summary_rows, cash_rows=(), *, complete=True, request_error="", source="IBKR account summary", today=None):
    """Turn fresh (tag, value, currency) rows into a conservative display view.

    Values from separate ledger buckets are never added. Duplicate monetary
    rows use the lower finite value. Any malformed alias prevents that metric
    from becoming usable, even when another alias happens to be valid.
    """
    summary_rows = list(summary_rows)
    cash_rows = list(cash_rows)
    all_rows = summary_rows + cash_rows
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    if not isinstance(today, date):
        raise TypeError("today must be a date")
    errors = []
    invalid = []

    def amount(rows, tags, currency, *, display_only=False):
        values = [value for tag, value, unit in rows if tag in tags and unit == currency]
        if not values:
            return None
        try:
            parsed = [float(value) for value in values]
            if not all(math.isfinite(value) and abs(value) < 1e15 for value in parsed):
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            if not display_only:
                invalid.append("/".join(tags) + "/" + currency)
            return None
        return min(parsed)

    # Only account-level summary fields establish the base currency. In
    # particular '$LEDGER-Currency', 'Currency', and BASE are not evidence.
    currencies = {currency for tag, _, currency in summary_rows
                  if tag in ("NetLiquidation", "AvailableFunds") and currency and currency != "BASE"}
    base = next(iter(currencies)) if len(currencies) == 1 else None
    net_currency = base
    if net_currency is None:
        net_units = {currency for tag, _, currency in summary_rows
                     if tag == "NetLiquidation" and currency and currency != "BASE"}
        net_currency = next(iter(net_units)) if len(net_units) == 1 else None
    net = amount(summary_rows, ("NetLiquidation",), net_currency) if net_currency else None
    total_cash = amount(summary_rows, ("TotalCashValue",), net_currency, display_only=True) if net_currency else None
    usd_cash = amount(all_rows, ("CashBalance", "$LEDGER-CashBalance"), "USD")
    aggregate_settled = amount(summary_rows, ("SettledCash",), base) if base else None
    aggregate_available = amount(summary_rows, ("AvailableFunds",), base) if base else None
    settled = aggregate_settled if base == "USD" else None
    available = aggregate_available if base == "USD" else None
    ledger_settled = amount(summary_rows, ("$LEDGER-SettledCash",), "USD")
    settlement_rows, legacy_currency_verified, legacy_error = _legacy_settlement_rows(
        summary_rows, cash_rows, complete, request_error)
    date_current, date_cap, date_source, settled_as_of, date_error = _settlement_by_date(summary_rows + settlement_rows, today)
    if legacy_currency_verified and date_source:
        date_source += "（旧版汇总字段；USD 单位经基础币种及美元账本核验）"
    settled_source = "SettledCash/USD (account summary)" if settled is not None else ""
    if date_current is not None and base == "USD" and not date_error:
        settled = date_current
        settled_source = date_source
    settlement_cap = (min(value for value in (settled, date_cap, aggregate_settled if base == "USD" else None)
                          if value is not None) if settled is not None else None)

    def raw_text(tag):
        values = sorted({str(value).strip() for key, value, _ in all_rows
                         if _unprefixed(key) == tag and str(value).strip()})
        return " / ".join(values)

    account_type = raw_text("AccountType")
    trading_type = raw_text("TradingType-S")
    type_parts = []
    if account_type:
        type_parts.append("账户结构 " + account_type)
    if trading_type:
        type_parts.append("证券交易类型 " + trading_type)
        # IBKR's Risk Navigator guide maps STKNOPT to Reg T Margin:
        # https://www.interactivebrokers.com/campus/trading-lessons/risk-navigator-alternative-margin-calculator/
        # This label does not relax the separate settled-cash budget checks.
        if trading_type == "STKNOPT":
            type_parts[-1] += "（Reg T 保证金）"
    account_type_label = "；".join(type_parts) or "TWS 未返回账户类型"
    if not trading_type:
        account_type_label += "；现金/保证金类型未确认"
    readiness = {str(value).strip().lower() for tag, value, _ in all_rows if _unprefixed(tag) == "AccountReady"}
    if readiness - {"true", "1"}:
        errors.append("TWS AccountReady 未就绪，资金可能正在重置，暂不允许开仓")
    if date_error:
        errors.append(date_error)
    if legacy_error:
        errors.append(legacy_error)

    if not complete:
        errors.append(request_error or "资金下载未完整结束")
    elif request_error:
        errors.append(request_error)
    if base is None:
        errors.append("账户汇总币种缺失或冲突，无法确认资金单位")
    elif base != "USD":
        errors.append(f"账户基础币种为 {base}；可查看资金，当前版本仅对 USD 基础币种计算买入张数")
    if usd_cash is None:
        errors.append("未取得有效的 USD CashBalance；不会把 BASE 余额或总资产当作美元现金")
    if base == "USD" and settled is None:
        errors.append("未取得有效的 SettledCash/USD 或当前 USD SettledCashByDate，暂不允许开仓")
    if base == "USD" and available is None:
        errors.append("账户汇总未返回有效的 AvailableFunds/USD，暂不允许开仓")
    if invalid:
        errors.append("资金值无效：" + ", ".join(sorted(set(invalid))))

    cash = None
    if not errors:
        caps = [usd_cash, settlement_cap, available]
        if ledger_settled is not None:
            caps.append(ledger_settled)
        cash = max(0.0, min(caps))
    return {
        "net_liquidation": net,
        "net_liquidation_currency": net_currency,
        "total_cash_value": total_cash,
        "usd_cash": usd_cash,
        "settled_cash": settled,
        "settled_cash_current": date_current if date_current is not None else settled,
        "settlement_cash_cap": settlement_cap,
        "ledger_settled_cash": ledger_settled,
        "settled_source": settled_source,
        "settled_as_of": settled_as_of,
        "account_type_raw": account_type,
        "trading_type": trading_type,
        "account_type_label": account_type_label,
        "settlement_shapes": _settlement_shapes(all_rows),
        "legacy_settlement_currency_verified": legacy_currency_verified,
        "available_funds": available,
        "aggregate_settled_cash": aggregate_settled,
        "aggregate_available_funds": aggregate_available,
        "cash": cash,
        "base_currency": base,
        "ready": not errors,
        "error": "；".join(errors),
        "updated_at": time.time(),
        "source": source,
        "complete": complete,
        "field_names": sorted({f"{tag}/{currency}" for tag, _, currency in all_rows}),
    }


class AccountData:
    """Use only from the broker's single worker thread / event loop.

    Wrapping the instance's callbacks is intentional: ib_async's public
    accountSummaryEvent omits reqId, and its summary cache mixes request IDs.
    The decoder resolves wrapper methods dynamically. Each wrapper delegates
    to the library and is restored in finally, including timeout/error cases.
    """
    def __init__(self, ib, log=lambda message: None, timeout=10.0):
        self.ib = ib
        self.log = log
        self.timeout = timeout
        self._refreshing = False
        self._last_signature = None

    @staticmethod
    def _restore(wrapper, name, previous, was_instance_attribute):
        if was_instance_attribute:
            setattr(wrapper, name, previous)
        else:
            delattr(wrapper, name)

    def _summary(self, account):
        wrapper, client = self.ib.wrapper, self.ib.client
        req_id = client.getReqId()
        future = wrapper.startReq(req_id)
        rows = []
        complete = False
        old_value = wrapper.accountSummary
        old_end = wrapper.accountSummaryEnd
        had_value = "accountSummary" in vars(wrapper)
        had_end = "accountSummaryEnd" in vars(wrapper)

        def value_callback(incoming_id, incoming_account, tag, value, currency):
            if not complete and incoming_id == req_id and incoming_account == account:
                rows.append((tag, value, currency))
            old_value(incoming_id, incoming_account, tag, value, currency)

        def end_callback(incoming_id):
            nonlocal complete
            if incoming_id == req_id:
                complete = True
            old_end(incoming_id)

        wrapper.accountSummary = value_callback
        wrapper.accountSummaryEnd = end_callback
        failure = ""
        try:
            client.reqAccountSummary(req_id, "All", SUMMARY_TAGS)
            self.ib._run(asyncio.wait_for(future, timeout=self.timeout))
            if not complete:
                failure = "未收到匹配请求的 accountSummaryEnd"
        except Exception as exc:
            complete = False
            failure = f"账户汇总下载失败（{type(exc).__name__}）：{exc or '超时或连接已断开'}"
        finally:
            # Cancel even a completed summary: otherwise a stream remains open
            # and eventually consumes the broker's two-subscription allowance.
            try:
                client.cancelAccountSummary(req_id)
            except Exception as exc:
                complete = False
                failure = f"账户汇总订阅取消失败（{type(exc).__name__}），请断开后重连"
            self._restore(wrapper, "accountSummary", old_value, had_value)
            self._restore(wrapper, "accountSummaryEnd", old_end, had_end)
            if not future.done():
                future.cancel()
            wrapper._endReq(req_id)
        return rows, complete, failure

    def _cash_download(self, account):
        """Fresh fallback for USD cash, dated settlement, and account status.

        This reads callbacks received between an explicit resubscription and
        the account-specific end marker, rather than library caches.
        """
        wrapper = self.ib.wrapper
        rows = []
        complete = False
        old_value = wrapper.updateAccountValue
        old_end = wrapper.accountDownloadEnd
        had_value = "updateAccountValue" in vars(wrapper)
        had_end = "accountDownloadEnd" in vars(wrapper)

        def value_callback(tag, value, currency, incoming_account):
            wanted = ("CashBalance", "SettledCashByDate", "SettledCashByDate-S", "SettledCashByDate-C",
                      "SettledCashByDate-P", "TradingType-S", "AccountType", "AccountReady")
            if not complete and incoming_account == account and _unprefixed(tag) in wanted:
                rows.append((tag, value, currency))
            old_value(tag, value, currency, incoming_account)

        def end_callback(incoming_account):
            nonlocal complete
            if incoming_account == account:
                complete = True
            old_end(incoming_account)

        wrapper.updateAccountValue = value_callback
        wrapper.accountDownloadEnd = end_callback
        future = None
        failure = ""
        try:
            self.ib.client.reqAccountUpdates(False, account)
            future = self.ib.reqAccountUpdatesAsync(account)
            self.ib._run(asyncio.wait_for(future, timeout=self.timeout))
            if not complete:
                failure = "未收到匹配账户的 accountDownloadEnd"
        except Exception as exc:
            complete = False
            failure = f"账户资金补充下载失败（{type(exc).__name__}）：{exc or '超时或连接已断开'}"
        finally:
            self._restore(wrapper, "updateAccountValue", old_value, had_value)
            self._restore(wrapper, "accountDownloadEnd", old_end, had_end)
            if future is not None and not future.done():
                future.cancel()
            wrapper._endReq("accountValues")
        return rows, complete, failure

    def refresh(self, account):
        if self._refreshing:
            return financial_view([], complete=False, request_error="资金同步正在进行，请勿重复请求")
        if not account or not self.ib.isConnected():
            return financial_view([], complete=False, request_error="尚未连接到账户，资金未知")
        self._refreshing = True
        try:
            rows, complete, failure = self._summary(account)
            cash_rows = []
            source = "IBKR fresh account summary + USD ledger"
            has_usd_cash = any(tag in ("CashBalance", "$LEDGER-CashBalance") and currency == "USD"
                               for tag, _, currency in rows)
            has_settled = any(tag == "SettledCash" and currency == "USD" for tag, _, currency in rows)
            if complete and not failure and (not has_usd_cash or not has_settled):
                cash_rows, cash_complete, cash_failure = self._cash_download(account)
                complete = complete and cash_complete
                failure = cash_failure
                source += " + fresh account-value settlement"
            result = financial_view(rows, cash_rows, complete=complete, request_error=failure, source=source)
            signature = (tuple(result["field_names"]), result["base_currency"], result["ready"], result["error"],
                         result["account_type_raw"], result["trading_type"], tuple(result["settlement_shapes"]))
            if signature != self._last_signature:
                self._last_signature = signature
                fields = ", ".join(result["field_names"]) or "无"
                self.log(f"本次账户汇总字段（不含金额）：{fields}；汇总币种 {result['base_currency'] or '未知'}")
                self.log("TWS 类型原文：" + result["account_type_label"])
                if result["settlement_shapes"]:
                    self.log("结算字段格式（日期/金额/文本已掩码，仅诊断，不据此推断币种）：" +
                             "；".join(result["settlement_shapes"]))
                if result["error"]:
                    self.log("资金仅供查看，开仓未就绪：" + result["error"])
            return result
        except Exception as exc:
            # Unexpected library incompatibilities must also clear prior cash.
            result = financial_view([], complete=False,
                                    request_error=f"资金读取失败（{type(exc).__name__}）：{exc}")
            self.log(result["error"])
            return result
        finally:
            self._refreshing = False
