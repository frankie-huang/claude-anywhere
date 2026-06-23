"""atomic_json 工具单元测试

覆盖：正常往返、文件不存在/损坏 JSON/顶层非 dict 的降级、写入异常时 tmp 清理。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

from utils.atomic_json import atomic_load_json, atomic_write_json  # noqa: E402


class TestAtomicJson(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._file = os.path.join(self._tmpdir, 'data.json')

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _list_tmp(self):
        return [n for n in os.listdir(self._tmpdir) if n.endswith('.tmp')]

    def test_write_then_load_roundtrip(self):
        data = {'a': 1, 'b': {'c': '中文'}}
        self.assertTrue(atomic_write_json(self._file, data, self._tmpdir, tag='test'))
        self.assertEqual(atomic_load_json(self._file, tag='test'), data)

    def test_load_missing_file_returns_default(self):
        self.assertEqual(atomic_load_json(self._file, tag='test'), {})
        self.assertEqual(atomic_load_json(self._file, default={'x': 1}, tag='test'), {'x': 1})

    def test_load_corrupt_json_returns_default(self):
        with open(self._file, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        self.assertEqual(atomic_load_json(self._file, tag='test'), {})

    def test_load_non_dict_returns_default(self):
        """缺陷 1：顶层为数组/字符串时返回 default 而非崩溃"""
        for bad in (['a', 'b'], '"just a string"', '123'):
            with open(self._file, 'w', encoding='utf-8') as f:
                f.write(bad if isinstance(bad, str) else json.dumps(bad))
            self.assertEqual(atomic_load_json(self._file, tag='test'), {})

    def test_default_is_fresh_dict_each_call(self):
        """default=None 每次返回新 dict，避免共享可变默认值"""
        a = atomic_load_json(self._file, tag='test')
        a['mutated'] = True
        b = atomic_load_json(self._file, tag='test')
        self.assertEqual(b, {})

    def test_write_failure_cleans_tmp(self):
        """缺陷 2：os.replace 失败时清理临时文件，不留 .tmp 残留"""
        with mock.patch('utils.atomic_json.os.replace', side_effect=OSError('boom')):
            ok = atomic_write_json(self._file, {'a': 1}, self._tmpdir, tag='test')
        self.assertFalse(ok)
        self.assertEqual(self._list_tmp(), [])
        self.assertFalse(os.path.exists(self._file))


if __name__ == '__main__':
    unittest.main()
