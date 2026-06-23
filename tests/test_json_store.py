"""JsonStore 基类单元测试

重点：per-subclass 单例隔离（不同子类的 _instance 互不串台）、load/save 往返、
_post_init 钩子调用、STORE_NAME 缺失校验。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

from stores.json_store import JsonStore  # noqa: E402


class _StoreA(JsonStore):
    STORE_NAME = 'a'
    LOG_TAG = 'store-a'


class _StoreB(JsonStore):
    STORE_NAME = 'b'
    LOG_TAG = 'store-b'


class _StoreNoName(JsonStore):
    pass


class _StoreWithPostInit(JsonStore):
    STORE_NAME = 'p'
    LOG_TAG = 'store-p'

    def _post_init(self):
        # 加载后构建一个内存索引，验证钩子在 _file_path 就绪后被调用
        self._keys = sorted(self._load().keys())


class TestJsonStore(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        # 每个测试重置单例，避免跨用例污染
        _StoreA._instance = None
        _StoreB._instance = None

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        _StoreA._instance = None
        _StoreB._instance = None

    def test_per_subclass_singleton_isolation(self):
        """A.initialize 后，B.get_instance 仍为 None（不串台）"""
        a = _StoreA.initialize(self._tmpdir)
        self.assertIsNotNone(a)
        self.assertIsInstance(a, _StoreA)
        self.assertIsNone(_StoreB.get_instance())

        b = _StoreB.initialize(self._tmpdir)
        self.assertIsInstance(b, _StoreB)
        # 两个子类各自独立
        self.assertIsNot(_StoreA.get_instance(), _StoreB.get_instance())

    def test_initialize_idempotent(self):
        a1 = _StoreA.initialize(self._tmpdir)
        a2 = _StoreA.initialize(self._tmpdir)
        self.assertIs(a1, a2)

    def test_get_instance_before_init(self):
        self.assertIsNone(_StoreA.get_instance())

    def test_load_save_roundtrip(self):
        store = _StoreA(self._tmpdir)
        self.assertTrue(store._save({'k': 'v'}))
        self.assertEqual(store._load(), {'k': 'v'})
        self.assertTrue(os.path.exists(os.path.join(self._tmpdir, 'a.json')))

    def test_missing_store_name_raises(self):
        with self.assertRaises(ValueError):
            _StoreNoName(self._tmpdir)

    def test_post_init_called_after_paths_ready(self):
        store = _StoreWithPostInit(self._tmpdir)
        self.assertEqual(store._keys, [])
        store._save({'x': 1, 'y': 2})
        store2 = _StoreWithPostInit(self._tmpdir)
        self.assertEqual(store2._keys, ['x', 'y'])


if __name__ == '__main__':
    unittest.main()
