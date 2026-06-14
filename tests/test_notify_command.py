"""_parse_notify_args 命令解析测试"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'shared'))

from handlers.feishu import _parse_notify_args  # noqa: E402


class TestParseNotifyArgs(unittest.TestCase):

    # === query ===

    def test_empty(self):
        self.assertEqual(_parse_notify_args(''), ('query',))

    def test_status(self):
        self.assertEqual(_parse_notify_args('status'), ('query',))

    # === at: set ===

    def test_at_off(self):
        self.assertEqual(_parse_notify_args('at off'), ('set_at', 'off'))

    def test_at_self(self):
        self.assertEqual(_parse_notify_args('at self'), ('set_at', 'self'))

    def test_at_all(self):
        self.assertEqual(_parse_notify_args('at all'), ('set_at', 'all'))

    def test_at_user_id(self):
        self.assertEqual(_parse_notify_args('at ou_xxx123'), ('set_at', 'ou_xxx123'))

    # === at: set_at_time ===

    def test_at_time_range(self):
        self.assertEqual(_parse_notify_args('at 08:00-22:00'),
                         ('set_at_time', '08:00', '22:00'))

    def test_at_time_range_cross_midnight(self):
        self.assertEqual(_parse_notify_args('at 22:00-08:00'),
                         ('set_at_time', '22:00', '08:00'))

    def test_at_time_range_full_day(self):
        self.assertEqual(_parse_notify_args('at 00:00-24:00'),
                         ('set_at_time', '00:00', '24:00'))

    # === at: clear_at_time ===

    def test_at_always(self):
        self.assertEqual(_parse_notify_args('at always'), ('clear_at_time',))

    # === delay: set_permission_delay ===

    def test_delay_zero(self):
        self.assertEqual(_parse_notify_args('delay 0'), ('set_permission_delay', 0))

    def test_delay_positive(self):
        self.assertEqual(_parse_notify_args('delay 30'), ('set_permission_delay', 30))

    # === delay: clear_permission_delay ===

    def test_delay_default(self):
        self.assertEqual(_parse_notify_args('delay default'), ('clear_permission_delay',))

    # === 错误类 ===

    def test_unknown_subcommand_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('mute off')

    def test_at_no_arg_raises(self):
        """at 无参数应报错，不再当 query"""
        with self.assertRaises(ValueError):
            _parse_notify_args('at')

    def test_at_too_many_args_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('at off extra')

    def test_invalid_time_range_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('at 99:99-99:99')

    def test_invalid_time_range_hour_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('at 25:00-08:00')

    def test_delay_no_arg_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('delay')

    def test_delay_negative_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('delay -1')

    def test_delay_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('delay abc')

    def test_delay_too_many_args_raises(self):
        with self.assertRaises(ValueError):
            _parse_notify_args('delay 30 extra')


if __name__ == '__main__':
    unittest.main()
