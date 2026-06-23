"""NotifyConfigStore 单元测试

测试运行时通知配置的 set/get/clear 操作及字段隔离性。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

from stores.notify_config_store import NotifyConfigStore  # noqa: E402


class TestNotifyConfigStore(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.store = NotifyConfigStore(self._tmpdir)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_empty_config(self):
        """初始状态不含用户配置字段（迁移写入的 permission_delay 除外）"""
        config = self.store.get_config()
        self.assertNotIn('at_user', config)
        self.assertNotIn('at_time_range', config)

    def test_set_at_user(self):
        """设置 at_user 并读取"""
        self.assertTrue(self.store.set_at_user('off'))
        config = self.store.get_config()
        self.assertEqual(config['at_user'], 'off')
        self.assertIn('updated_at', config)

    def test_set_at_user_rejects_empty(self):
        """空值被拒绝，不写入 at_user 字段"""
        self.assertFalse(self.store.set_at_user(''))
        self.assertFalse(self.store.set_at_user('   '))
        self.assertNotIn('at_user', self.store.get_config())

    def test_set_at_user_overwrite(self):
        """覆盖 at_user"""
        self.store.set_at_user('off')
        self.store.set_at_user('all')
        self.assertEqual(self.store.get_config()['at_user'], 'all')

    def test_set_time_range(self):
        """设置时段"""
        self.assertTrue(self.store.set_time_range('08:00', '22:00'))
        config = self.store.get_config()
        self.assertEqual(config['at_start'], '08:00')
        self.assertEqual(config['at_end'], '22:00')

    def test_set_time_range_rejects_empty(self):
        """空时段被拒绝"""
        self.assertFalse(self.store.set_time_range('', '22:00'))
        self.assertFalse(self.store.set_time_range('08:00', ''))

    def test_clear_time_range(self):
        """清除时段保留 at_user"""
        self.store.set_at_user('all')
        self.store.set_time_range('08:00', '22:00')
        self.assertTrue(self.store.clear_time_range())
        config = self.store.get_config()
        self.assertEqual(config['at_user'], 'all')
        self.assertNotIn('at_start', config)
        self.assertNotIn('at_end', config)

    def test_set_at_user_preserves_time_range(self):
        """设置 at_user 不影响已有时段"""
        self.store.set_time_range('08:00', '22:00')
        self.store.set_at_user('off')
        config = self.store.get_config()
        self.assertEqual(config['at_user'], 'off')
        self.assertEqual(config['at_start'], '08:00')
        self.assertEqual(config['at_end'], '22:00')

    def test_set_time_range_preserves_at_user(self):
        """设置时段不影响已有 at_user"""
        self.store.set_at_user('all')
        self.store.set_time_range('22:00', '08:00')
        config = self.store.get_config()
        self.assertEqual(config['at_user'], 'all')
        self.assertEqual(config['at_start'], '22:00')
        self.assertEqual(config['at_end'], '08:00')

    def test_clear_time_range_on_empty(self):
        """空配置下清除时段不报错"""
        self.assertTrue(self.store.clear_time_range())
        config = self.store.get_config()
        self.assertNotIn('at_start', config)
        self.assertNotIn('at_end', config)

    # === permission_delay ===

    def test_set_permission_delay(self):
        """设置延迟"""
        self.assertTrue(self.store.set_permission_delay(0))
        self.assertEqual(self.store.get_config()['permission_delay'], 0)

    def test_set_permission_delay_rejects_negative(self):
        """负值被拒绝，不覆盖已有的 permission_delay"""
        existing = self.store.get_config().get('permission_delay')
        self.assertFalse(self.store.set_permission_delay(-1))
        self.assertEqual(self.store.get_config().get('permission_delay'), existing)

    def test_clear_permission_delay(self):
        """清除延迟保留其他字段"""
        self.store.set_at_user('all')
        self.store.set_permission_delay(30)
        self.assertTrue(self.store.clear_permission_delay())
        config = self.store.get_config()
        self.assertEqual(config['at_user'], 'all')
        self.assertNotIn('permission_delay', config)

    def test_set_permission_delay_preserves_at_user(self):
        """设置延迟不影响 at_user"""
        self.store.set_at_user('off')
        self.store.set_permission_delay(10)
        config = self.store.get_config()
        self.assertEqual(config['at_user'], 'off')
        self.assertEqual(config['permission_delay'], 10)


if __name__ == '__main__':
    unittest.main()
