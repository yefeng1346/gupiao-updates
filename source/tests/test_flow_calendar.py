from datetime import date, datetime
import unittest
from app.flow_calendar import confirmed_close_date, previous_trading_day, trading_window


class FlowCalendarTests(unittest.TestCase):
    def test_national_holiday_and_weekend_roll_back_without_network(self):
        self.assertEqual(previous_trading_day(date(2026, 10, 6)), (date(2026, 9, 30), True))
        self.assertEqual(previous_trading_day(date(2026, 9, 25))[0], date(2026, 9, 24))
        self.assertEqual(previous_trading_day(date(2026, 10, 10))[0], date(2026, 10, 9))
        window = trading_window(date(2026, 10, 6))
        self.assertEqual(len(window), 10)
        self.assertEqual(window[-1], '2026-09-30')
        self.assertNotIn('2026-09-25', window)

    def test_only_dated_post_close_records_are_archived(self):
        now = datetime.fromisoformat('2026-10-06T12:00:00+08:00')
        self.assertEqual(confirmed_close_date('2026-09-30T07:39:30+00:00', now), '2026-09-30')
        for stamp in (None, 'bad', '2026-09-30T14:30:00+08:00',
                      '2026-09-30T15:01:00+08:00', '2026-10-06T15:39:30+08:00',
                      '2026-10-08T15:39:30+08:00', '2026-09-30T15:39:30'):
            self.assertIsNone(confirmed_close_date(stamp, now), stamp)

    def test_unknown_calendar_year_never_claims_complete_window(self):
        self.assertEqual(previous_trading_day(date(2027, 1, 4))[1], False)
        self.assertIsNone(trading_window(date(2027, 1, 4)))


if __name__ == '__main__':
    unittest.main()
