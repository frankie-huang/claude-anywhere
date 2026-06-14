"""DirectoryStore 递归静音功能测试

覆盖全部 24 条状态转移规则和 walk-up 匹配算法。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

from services.directory_store import DirectoryStore  # noqa: E402


class _BaseTest(unittest.TestCase):
    """为每个测试创建临时目录和独立的 DirectoryStore 实例。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.store = DirectoryStore(self._tmpdir)
        # 创建测试目录结构
        self.root = os.path.join(self._tmpdir, 'projects')
        self.child = os.path.join(self.root, 'child')
        self.grandchild = os.path.join(self.child, 'grandchild')
        self.sibling = os.path.join(self._tmpdir, 'other')
        for d in [self.root, self.child, self.grandchild, self.sibling]:
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def assert_changed(self, result, msg_contains=None):
        """断言操作发生了变更，可选检查 message 包含特定文本。"""
        self.assertIsNotNone(result)
        self.assertTrue(result['changed'])
        self.assertTrue(len(result['message']) > 0)
        if msg_contains:
            self.assertIn(msg_contains, result['message'])

    def assert_idempotent(self, result):
        """断言操作幂等（无变更）。"""
        self.assertIsNotNone(result)
        self.assertFalse(result['changed'])
        self.assertTrue(len(result['message']) > 0)


# =========================================================================
# 状态转移测试：/mute /p
# =========================================================================

class TestMuteExact(_BaseTest):

    def test_01_empty_to_M(self):
        """{} + /mute /p → {M}，无覆盖提示"""
        result = self.store.mute_dir(self.root)
        self.assert_changed(result)
        self.assertNotIn('加白规则已覆盖', result['message'])

    def test_02_M_idempotent(self):
        """{M} + /mute /p → {M} 幂等"""
        self.store.mute_dir(self.root)
        self.assert_idempotent(self.store.mute_dir(self.root))

    def test_03_R_add_M(self):
        """{R} + /mute /p → {M,R}，提示含所有子目录"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.store.unmute_dir(self.root)                  # {R} (清 M 保留 R)
        result = self.store.mute_dir(self.root)           # 补 M → {M,R}
        self.assert_changed(result, '含所有子目录')
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_04_MR_idempotent(self):
        """{M,R} + /mute /p → {M,R} 幂等"""
        self.store.mute_dir(self.root, recursive=True)
        self.assert_idempotent(self.store.mute_dir(self.root))

    def test_05_U_to_M(self):
        """{U} + /mute /p → {M}，带覆盖提示"""
        self.store.unmute_dir(self.root)    # {U}
        result = self.store.mute_dir(self.root)
        self.assert_changed(result, '加白规则已覆盖')
        self.assertIn('/unmute <path>', result['message'])
        self.assertTrue(self.store.is_dir_muted(self.root))

    def test_06_UR_to_M(self):
        """{U,R} + /mute /p → {M} 翻转保护清 U+R，带覆盖提示"""
        self.store.unmute_dir(self.root, recursive=True)   # {U,R}
        result = self.store.mute_dir(self.root)  # → {M}
        self.assert_changed(result, '加白规则已覆盖')
        self.assertIn('/unmute <path>', result['message'])
        self.assertTrue(self.store.is_dir_muted(self.root))
        # 子孙不应被静音（R 已清除）
        self.assertFalse(self.store.is_dir_muted(self.child))


# =========================================================================
# 状态转移测试：/mute /p/**
# =========================================================================

class TestMuteRecursive(_BaseTest):

    def test_07_empty_to_MR(self):
        """{} + /mute /p/** → {M,R}，无覆盖提示"""
        result = self.store.mute_dir(self.root, recursive=True)
        self.assert_changed(result)
        self.assertNotIn('加白规则已覆盖', result['message'])
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_08_M_upgrade(self):
        """{M} + /mute /p/** → {M,R} 升级"""
        self.store.mute_dir(self.root)
        self.assert_changed(self.store.mute_dir(self.root, recursive=True))
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_09_R_upgrade(self):
        """{R} + /mute /p/** → {M,R} 补 M"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.store.unmute_dir(self.root)                  # {R}
        self.assert_changed(self.store.mute_dir(self.root, recursive=True))
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_10_MR_idempotent(self):
        """{M,R} + /mute /p/** → {M,R} 幂等"""
        self.store.mute_dir(self.root, recursive=True)
        self.assert_idempotent(self.store.mute_dir(self.root, recursive=True))

    def test_11_U_to_MR(self):
        """{U} + /mute /p/** → {M,R} 更甚写入，带覆盖提示"""
        self.store.unmute_dir(self.root)                      # {U}
        result = self.store.mute_dir(self.root, recursive=True)
        self.assert_changed(result, '加白规则已覆盖')
        self.assertIn('/unmute <path>/**', result['message'])
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_12_UR_to_MR(self):
        """{U,R} + /mute /p/** → {M,R} 写入意图，带覆盖提示"""
        self.store.unmute_dir(self.root, recursive=True)      # {U,R}
        result = self.store.mute_dir(self.root, recursive=True)
        self.assert_changed(result, '加白规则已覆盖')
        self.assertIn('/unmute <path>/**', result['message'])
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))


# =========================================================================
# 状态转移测试：/unmute /p
# =========================================================================

class TestUnmuteExact(_BaseTest):

    def test_13_empty_to_U(self):
        """{} + /unmute /p → {U}"""
        self.assert_changed(self.store.unmute_dir(self.root), '不静音')

    def test_14_M_to_empty(self):
        """{M} + /unmute /p → {}"""
        self.store.mute_dir(self.root)
        self.assert_changed(self.store.unmute_dir(self.root), '清除')
        self.assertFalse(self.store.is_dir_muted(self.root))

    def test_15_R_idempotent(self):
        """{R} + /unmute /p → {R} 幂等（R 蕴含自身不静音）"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.store.unmute_dir(self.root)                  # {R}
        # 再次 unmute /p 应该幂等
        self.assert_idempotent(self.store.unmute_dir(self.root))
        # 子孙仍然静音
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_16_MR_to_R(self):
        """{M,R} + /unmute /p → {R} 清 M 保留 R"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.assert_changed(self.store.unmute_dir(self.root), '子目录递归静音保留')
        # 自身不再静音
        self.assertFalse(self.store.is_dir_muted(self.root))
        # 子孙仍然静音
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_17_U_idempotent(self):
        """{U} + /unmute /p → {U} 幂等"""
        self.store.unmute_dir(self.root)
        self.assert_idempotent(self.store.unmute_dir(self.root))

    def test_18_UR_idempotent(self):
        """{U,R} + /unmute /p → {U,R} 幂等"""
        self.store.unmute_dir(self.root, recursive=True)
        self.assert_idempotent(self.store.unmute_dir(self.root))


# =========================================================================
# 状态转移测试：/unmute /p/**
# =========================================================================

class TestUnmuteRecursive(_BaseTest):

    def test_19_empty_to_UR(self):
        """{} + /unmute /p/** → {U,R}"""
        self.assert_changed(self.store.unmute_dir(self.root, recursive=True), '不静音')
        self.assertFalse(self.store.is_dir_muted(self.root))

    def test_20_M_to_UR(self):
        """{M} + /unmute /p/** → {U,R} 更甚写入"""
        self.store.mute_dir(self.root)
        self.assert_changed(self.store.unmute_dir(self.root, recursive=True), '不静音')
        self.assertFalse(self.store.is_dir_muted(self.root))
        self.assertFalse(self.store.is_dir_muted(self.child))

    def test_21_R_to_UR(self):
        """{R} + /unmute /p/** → {U,R} 更甚写入"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.store.unmute_dir(self.root)                  # {R}
        self.assert_changed(self.store.unmute_dir(self.root, recursive=True), '不静音')
        self.assertFalse(self.store.is_dir_muted(self.root))
        self.assertFalse(self.store.is_dir_muted(self.child))

    def test_22_MR_to_empty(self):
        """{M,R} + /unmute /p/** → {} 全匹配清除"""
        self.store.mute_dir(self.root, recursive=True)
        self.assert_changed(self.store.unmute_dir(self.root, recursive=True), '清除')
        self.assertFalse(self.store.is_dir_muted(self.root))
        self.assertFalse(self.store.is_dir_muted(self.child))

    def test_23_U_upgrade(self):
        """{U} + /unmute /p/** → {U,R} 升级"""
        self.store.unmute_dir(self.root)
        self.assert_changed(self.store.unmute_dir(self.root, recursive=True))

    def test_24_UR_idempotent(self):
        """{U,R} + /unmute /p/** → {U,R} 幂等"""
        self.store.unmute_dir(self.root, recursive=True)
        self.assert_idempotent(self.store.unmute_dir(self.root, recursive=True))


# =========================================================================
# walk-up 匹配算法测试
# =========================================================================

class TestWalkUpMatching(_BaseTest):

    def test_recursive_mute_covers_descendants(self):
        """递归静音覆盖所有子孙"""
        self.store.mute_dir(self.root, recursive=True)
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))
        self.assertTrue(self.store.is_dir_muted(self.grandchild))
        self.assertFalse(self.store.is_dir_muted(self.sibling))

    def test_exact_mute_does_not_cover_children(self):
        """精确静音不覆盖子目录"""
        self.store.mute_dir(self.root)
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertFalse(self.store.is_dir_muted(self.child))

    def test_recursive_mute_with_exact_unmute(self):
        """递归静音 + 精确加白：加白只保护自身"""
        self.store.mute_dir(self.root, recursive=True)
        self.store.unmute_dir(self.child)                  # {U} 精确加白
        self.assertFalse(self.store.is_dir_muted(self.child))
        # 精确加白不保护子孙
        self.assertTrue(self.store.is_dir_muted(self.grandchild))

    def test_recursive_mute_with_recursive_unmute(self):
        """递归静音 + 递归加白：加白保护自身和子孙"""
        self.store.mute_dir(self.root, recursive=True)
        self.store.unmute_dir(self.child, recursive=True)  # {U,R} 递归加白
        self.assertFalse(self.store.is_dir_muted(self.child))
        self.assertFalse(self.store.is_dir_muted(self.grandchild))
        # 其他子目录仍静音
        other_child = os.path.join(self.root, 'other')
        os.makedirs(other_child, exist_ok=True)
        self.assertTrue(self.store.is_dir_muted(other_child))

    def test_nested_override(self):
        """多层嵌套：递归静音 → 递归加白 → 精确静音"""
        self.store.mute_dir(self.root, recursive=True)         # /root {M,R}
        self.store.unmute_dir(self.child, recursive=True)      # /child {U,R}
        self.store.mute_dir(self.grandchild)                   # /grandchild {M}
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertFalse(self.store.is_dir_muted(self.child))
        self.assertTrue(self.store.is_dir_muted(self.grandchild))
        # grandchild 下的子目录穿透到 child 的递归加白
        deep = os.path.join(self.grandchild, 'deep')
        os.makedirs(deep, exist_ok=True)
        self.assertFalse(self.store.is_dir_muted(deep))

    def test_R_only_state(self):
        """{R} 状态：自身不静音，子孙静音"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.store.unmute_dir(self.root)                  # {R}
        self.assertFalse(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))

    def test_preemptive_unmute(self):
        """预防性加白在后续递归静音中生效"""
        self.store.unmute_dir(self.child)                       # 预防性加白
        self.store.mute_dir(self.root, recursive=True)          # 递归静音
        self.assertFalse(self.store.is_dir_muted(self.child))   # 加白生效
        self.assertTrue(self.store.is_dir_muted(self.grandchild))  # 子孙仍静音（精确加白不保护）


# =========================================================================
# 回到 {} 的路径测试
# =========================================================================

class TestReturnToEmpty(_BaseTest):

    def test_M_to_empty(self):
        """{M} → /unmute /p → {} (1步)"""
        self.store.mute_dir(self.root)
        self.store.unmute_dir(self.root)
        self.assertEqual(self.store.list_muted_dirs(), [])

    def test_MR_to_empty(self):
        """{M,R} → /unmute /p/** → {} (1步)"""
        self.store.mute_dir(self.root, recursive=True)
        self.store.unmute_dir(self.root, recursive=True)
        self.assertEqual(self.store.list_muted_dirs(), [])

    def test_U_to_empty(self):
        """{U} → /mute /p → /unmute /p → {} (2步)"""
        self.store.unmute_dir(self.root)
        self.store.mute_dir(self.root)
        self.store.unmute_dir(self.root)
        self.assertEqual(self.store.list_muted_dirs(), [])

    def test_R_to_empty(self):
        """{R} → /mute /p → /unmute /p/** → {} (2步)"""
        self.store.mute_dir(self.root, recursive=True)   # {M,R}
        self.store.unmute_dir(self.root)                  # {R}
        self.store.mute_dir(self.root)                    # {M,R}
        self.store.unmute_dir(self.root, recursive=True)  # {}
        self.assertEqual(self.store.list_muted_dirs(), [])

    def test_UR_to_empty(self):
        """{U,R} → /mute /p → /unmute /p → {} (2步)"""
        self.store.unmute_dir(self.root, recursive=True)  # {U,R}
        self.store.mute_dir(self.root)                    # {M} (翻转保护清 R)
        self.store.unmute_dir(self.root)                  # {}
        self.assertEqual(self.store.list_muted_dirs(), [])


# =========================================================================
# 翻转保护测试
# =========================================================================

class TestFlipProtection(_BaseTest):

    def test_mute_exact_on_UR_does_not_mute_children(self):
        """{U,R} + /mute /p → {M}，子孙不被意外静音"""
        self.store.unmute_dir(self.root, recursive=True)   # {U,R}
        self.store.mute_dir(self.root)                     # {M}
        self.assertTrue(self.store.is_dir_muted(self.root))
        self.assertFalse(self.store.is_dir_muted(self.child))

    def test_unmute_exact_on_MR_keeps_children_muted(self):
        """{M,R} + /unmute /p → {R}，子孙仍然静音"""
        self.store.mute_dir(self.root, recursive=True)     # {M,R}
        self.store.unmute_dir(self.root)                   # {R}
        self.assertFalse(self.store.is_dir_muted(self.root))
        self.assertTrue(self.store.is_dir_muted(self.child))


if __name__ == '__main__':
    unittest.main()
