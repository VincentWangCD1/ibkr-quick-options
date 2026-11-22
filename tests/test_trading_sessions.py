"""No-network calendar policy tests; no TWS, GUI, or order creation."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from trading_sessions import decide_session


def details(trading, liquid, zone="US/Eastern"):
    return SimpleNamespace(tradingHours=trading, liquidHours=liquid, timeZoneId=zone)


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class TradingSessionTests(unittest.TestCase):
    def setUp(self):
        self.schedule = details("20261111:0730-20261111:1615", "20261111:0930-20261111:1600")

    def test_regular_market_order_from_liquid_hours(self):
        result = decide_session(self.schedule, utc("2026-11-11T15:00:00"))
        self.assertEqual(result.kind, "regular")
        self.assertEqual(result.order_type, "MKT")
        self.assertFalse(result.outside_rth)
        self.assertEqual(result.session_end, utc("2026-11-11T21:00:00"))

    def test_premarket_requires_limit_and_rth_permission_flag(self):
        result = decide_session(self.schedule, utc("2026-11-11T13:00:00"))
        self.assertEqual(result.kind, "extended")
        self.assertEqual(result.order_type, "LMT")
        self.assertTrue(result.outside_rth)
        self.assertEqual(result.session_end, utc("2026-11-11T14:30:00"))

    def test_postmarket_uses_limit_until_contract_close(self):
        result = decide_session(self.schedule, utc("2026-11-11T21:05:00"))
        self.assertEqual(result.kind, "extended")
        self.assertEqual(result.session_end, utc("2026-11-11T21:15:00"))

    def test_start_inclusive_and_end_exclusive(self):
        cases = [("12:29:59", "closed"), ("12:30:00", "extended"),
                 ("14:30:00", "regular"), ("21:00:00", "extended"), ("21:15:00", "closed")]
        for clock, kind in cases:
            with self.subTest(clock=clock):
                result = decide_session(self.schedule, utc("2026-11-11T" + clock))
                self.assertEqual(result.kind, kind)
                if kind == "closed":
                    self.assertIsNone(result.order_type)
                    self.assertFalse(result.outside_rth)

    def test_specific_contract_calendar_can_have_no_extended_session(self):
        regular_only = details("20261111:0930-20261111:1600", "20261111:0930-20261111:1600")
        self.assertEqual(decide_session(regular_only, utc("2026-11-11T13:00:00")).kind, "closed")

    def test_five_minute_break_between_gth_and_regular_is_closed(self):
        schedule = details("20261111:0730-20261111:0925,20261111:0930-20261111:1615",
                           "20261111:0930-20261111:1600")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T14:24:59")).kind, "extended")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T14:25:00")).kind, "closed")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T14:30:00")).kind, "regular")

    def test_overnight_gth_uses_next_days_liquid_calendar(self):
        schedule = details("20261110:2015-20261111:0925;20261111:0930-20261111:1700",
                           "20261111:0930-20261111:1615")
        result = decide_session(schedule, utc("2026-11-11T02:00:00"))
        self.assertEqual(result.kind, "extended")
        self.assertEqual(result.session_end, utc("2026-11-11T14:25:00"))
        self.assertEqual(decide_session(schedule, utc("2026-11-11T21:30:00")).kind, "extended")

    def test_legacy_endpoint_and_implicit_midnight_rollover(self):
        schedule = details("20261110:2200-0200", "20261111:CLOSED", "UTC")
        result = decide_session(schedule, utc("2026-11-11T01:00:00"))
        self.assertEqual(result.kind, "extended")
        self.assertEqual(result.session_end, utc("2026-11-11T02:00:00"))

    def test_cross_year_overnight(self):
        schedule = details("20261231:2200-20270101:0200", "20270101:CLOSED", "UTC")
        result = decide_session(schedule, utc("2027-01-01T01:00:00"))
        self.assertEqual(result.kind, "extended")

    def test_2400_is_next_midnight_when_used_as_end(self):
        schedule = details("20261111:0000-2400", "20261111:CLOSED", "UTC")
        result = decide_session(schedule, utc("2026-11-11T23:59:59"))
        self.assertEqual(result.kind, "extended")
        self.assertEqual(result.session_end, utc("2026-11-12T00:00:00"))
        self.assertEqual(decide_session(schedule, utc("2026-11-12T00:00:00")).kind, "closed")

    def test_holiday_closed_not_hardcoded_weekday_open(self):
        schedule = details("20261126:CLOSED;20261127:0930-1300", "20261126:CLOSED;20261127:0930-1300")
        result = decide_session(schedule, utc("2026-11-26T16:00:00"))
        self.assertEqual(result.kind, "closed")
        self.assertIn("CLOSED", result.reason)

    def test_early_close_from_calendar(self):
        schedule = details("20261127:0930-1300", "20261127:0930-1300")
        self.assertEqual(decide_session(schedule, utc("2026-11-27T17:59:59")).kind, "regular")
        self.assertEqual(decide_session(schedule, utc("2026-11-27T18:00:00")).kind, "closed")

    def test_stale_calendar_cannot_be_reused_next_day(self):
        result = decide_session(self.schedule, utc("2026-11-12T15:00:00"))
        self.assertEqual(result.kind, "closed")

    def test_stale_liquid_calendar_does_not_turn_today_into_extended(self):
        schedule = details("20261111:0730-1615", "20261110:0930-1600")
        result = decide_session(schedule, utc("2026-11-11T15:00:00"))
        self.assertEqual(result.kind, "closed")
        self.assertIn("未覆盖", result.reason)

    def test_yesterdays_liquid_calendar_cannot_authorize_overnight_morning(self):
        schedule = details("20261110:2015-20261111:0925", "20261110:0930-1600")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T13:00:00")).kind, "closed")

    def test_liquid_hours_never_expand_trading_hours(self):
        schedule = details("20261111:0930-1300", "20261111:0930-1600")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T19:00:00")).kind, "closed")

    def test_closed_date_conflicting_with_overnight_window_fails_closed(self):
        schedule = details("20261110:2015-20261111:0925;20261111:CLOSED", "20261111:CLOSED")
        result = decide_session(schedule, utc("2026-11-11T13:00:00"))
        self.assertEqual(result.kind, "closed")
        self.assertIn("冲突", result.reason)

    def test_split_session_break_is_closed(self):
        schedule = details("20261111:0930-1200,1300-1600", "20261111:0930-1600")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T17:30:00")).kind, "closed")

    def test_spring_dst_crossing_uses_three_elapsed_hours(self):
        schedule = details("20260308:0000-20260308:0400", "20260308:0000-20260308:0400")
        result = decide_session(schedule, utc("2026-03-08T07:00:00"))
        self.assertEqual(result.kind, "regular")
        self.assertEqual(result.session_end, utc("2026-03-08T08:00:00"))

    def test_fall_dst_crossing_uses_five_elapsed_hours(self):
        schedule = details("20261101:0000-20261101:0400", "20261101:0000-20261101:0400")
        result = decide_session(schedule, utc("2026-11-01T08:30:00"))
        self.assertEqual(result.kind, "regular")
        self.assertEqual(result.session_end, utc("2026-11-01T09:00:00"))

    def test_dst_nonexistent_or_ambiguous_boundary_fails_closed(self):
        cases = [("20260308:0230-0400", "2026-03-08T07:00:00"),
                 ("20261101:0130-0400", "2026-11-01T08:00:00")]
        for hours, stamp in cases:
            with self.subTest(hours=hours):
                result = decide_session(details(hours, hours), utc(stamp))
                self.assertEqual(result.kind, "closed")
                self.assertIn("夏令时", result.reason)

    def test_summer_and_winter_use_contract_timezone_not_fixed_offset(self):
        summer = details("20260710:0930-1600", "20260710:0930-1600")
        result = decide_session(summer, utc("2026-07-10T13:30:00"))
        self.assertEqual(result.kind, "regular")
        self.assertEqual(result.session_end, utc("2026-07-10T20:00:00"))
        self.assertEqual(decide_session(self.schedule, utc("2026-11-11T13:30:00")).kind, "extended")

    def test_aware_non_utc_input_is_normalized(self):
        stamp = datetime(2026, 11, 11, 23, tzinfo=timezone(timedelta(hours=8)))
        result = decide_session(self.schedule, stamp)
        self.assertEqual(result.kind, "regular")
        self.assertIs(result.session_end.tzinfo, timezone.utc)

    def test_naive_or_invalid_current_time_fails_closed(self):
        for now in (datetime(2026, 11, 11, 15), None, "20261111"):
            with self.subTest(now=now):
                self.assertEqual(decide_session(self.schedule, now).kind, "closed")

    def test_missing_or_unknown_timezone_fails_closed(self):
        for zone in (None, "", "Invalid/TimeZone"):
            with self.subTest(zone=zone):
                self.assertEqual(decide_session(details("20261111:0930-1600", "20261111:0930-1600", zone),
                                                utc("2026-11-11T15:00:00")).kind, "closed")

    def test_missing_or_malformed_calendars_fail_closed(self):
        bad = [None, "", "20261111", "20260230:0930-1600", "20261111:2500-2600",
               "20261111:0930-0930", "20261111:2400-2400", "20261111:0930-20261110:1600",
               "20261111:0930-1600;20261111:CLOSED", "20261111:0930-1600,,1700-1800"]
        for hours in bad:
            with self.subTest(hours=hours):
                self.assertEqual(decide_session(details(hours, "20261111:0930-1600"),
                                                utc("2026-11-11T15:00:00")).kind, "closed")
                self.assertEqual(decide_session(details("20261111:0730-1615", hours),
                                                utc("2026-11-11T15:00:00")).kind, "closed")

    def test_trailing_semicolon_and_mapping_details(self):
        schedule = {"tradingHours": "20261111:0930-1600;", "liquidHours": "20261111:0930-1600;", "timeZoneId": "US/Eastern"}
        self.assertEqual(decide_session(schedule, utc("2026-11-11T15:00:00")).kind, "regular")

    def test_adjacent_full_day_windows_do_not_hide_missing_liquid_dates(self):
        schedule = details("20261110:0000-2400;20261111:0000-2400", "20261110:CLOSED", "UTC")
        self.assertEqual(decide_session(schedule, utc("2026-11-11T12:00:00")).kind, "closed")


if __name__ == "__main__":
    unittest.main()
