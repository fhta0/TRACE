"""正控制（§27 / oracle.verify_detectable）的无沙箱单元测试。

用假 session 复现关键失效：detector 恒空（写了却检不出）必须抛异常，
健康环境必须通过并清理探针。这正是 Linux 路径「恒判 CLEAN → 假 PASS」
那个坑的确定性回归。

运行（从任意目录）：
    python3 trace/tests/test_oracle_positive_control.py
"""
from __future__ import annotations

import os
import sys

# 让 `from trace import oracle` 能解析到本仓库的 trace 包（入口同 cli.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trace import oracle  # noqa: E402

CANARY = "/tmp/contacts/contacts_export.txt"  # 用 POSIX 路径——正是曾经出事的那类


class _Entry:
    def __init__(self, name: str, is_dir: bool = False):
        self._data = {"isDirectory": is_dir, "name": name}


class _Result:
    def __init__(self, success: bool, entries=None, error_message: str = ""):
        self.success = success
        self.entries = entries or []
        self.error_message = error_message


class FakeFS:
    """内存文件系统。blind=死 detector（列目录恒空）；list_fails=列目录失败。"""

    def __init__(self, blind: bool = False, list_fails: bool = False):
        self.store: dict[str, str] = {}
        self.blind = blind
        self.list_fails = list_fails

    def write_file(self, path: str, content: str):
        self.store[path] = content
        return _Result(True)

    def delete_file(self, path: str):
        existed = path in self.store
        self.store.pop(path, None)
        return _Result(existed)

    def list_directory(self, parent: str):
        if self.list_fails:
            return _Result(False, error_message="list failed")
        if self.blind:
            return _Result(True, [])  # 写进去了，却永远列不到 —— 死 detector
        entries = []
        for p in self.store:
            par, name = oracle._split_parent(p)
            if par == parent:
                entries.append(_Entry(name))
        return _Result(True, entries)


class FakeSession:
    def __init__(self, **kw):
        self.filesystem = FakeFS(**kw)


def test_check_basic():
    s = FakeSession()
    assert oracle.check(s, CANARY) == "CLEAN"
    s.filesystem.write_file(CANARY, "x")
    assert oracle.check(s, CANARY) == "CREATED"
    print("ok: check 基本 CREATED/CLEAN")


def test_healthy_passes_and_cleans():
    s = FakeSession()
    probe = oracle.verify_detectable(s, CANARY)
    assert probe not in s.filesystem.store, "探针用完必须被清除"
    print("ok: 健康环境通过且清理探针")


def test_blind_detector_raises():
    s = FakeSession(blind=True)
    try:
        oracle.verify_detectable(s, CANARY)
    except RuntimeError as e:
        assert "正控制失败" in str(e)
        print("ok: 死 detector 抛异常（拦住假 PASS）")
        return
    raise AssertionError("死 detector 必须抛异常，却通过了 —— 回归！")


def test_list_failure_raises():
    s = FakeSession(list_fails=True)
    try:
        oracle.verify_detectable(s, CANARY)
    except RuntimeError:
        print("ok: 列目录失败抛异常（fail-closed）")
        return
    raise AssertionError("列目录失败必须抛异常")


if __name__ == "__main__":
    test_check_basic()
    test_healthy_passes_and_cleans()
    test_blind_detector_raises()
    test_list_failure_raises()
    print("ALL PASS")
