import importlib
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

try:
    from test_quality_regression import reset_database_module  # discover 模式: tests/ 在 sys.path
except ModuleNotFoundError:
    from tests.test_quality_regression import reset_database_module  # 包模式


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class SessionMemoryTtlTests(unittest.TestCase):
    """P3.3: SESSION_MEMORY TTL 回收 + 读写加锁 + 后台线程在测试环境不启动."""

    @classmethod
    def setUpClass(cls):
        import database

        reset_database_module(database)
        sys.modules.pop("main", None)
        # 屏蔽 main 导入期全部后台线程 (startup-init / session-memory-sweep),
        # 与 test_sse_cleanup 保持一致, 避免后台线程并发访问共享测试连接
        started_thread_names = []

        def record_thread_start(thread):
            started_thread_names.append(thread.name)

        with mock.patch.dict(os.environ, {"APP_ENV": "test"}, clear=False):
            with mock.patch("threading.Thread.start", record_thread_start):
                cls.main = importlib.import_module("main")
        if "startup-init" in started_thread_names or "session-memory-sweep" in started_thread_names:
            raise AssertionError(f"test import started background threads: {started_thread_names}")
        cls.main.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls.main, "db", None) is not None and getattr(cls.main.db, "_test_conn", None) is not None:
            cls.main.db._test_conn.real_close()
            cls.main.db._test_conn = None

    def setUp(self):
        self.main.SESSION_MEMORY.clear()

    def tearDown(self):
        self.main.SESSION_MEMORY.clear()

    def _seed(self, sid: str, last_active: datetime):
        mem = self.main.get_session_memory(sid)
        mem["lastActive"] = _fmt(last_active)
        return mem

    def test_stale_entries_swept(self):
        """超过 TTL 的会话被回收, 活跃会话保留."""
        now = datetime.now()
        self._seed("stale-1", now - timedelta(minutes=31))
        self._seed("stale-2", now - timedelta(hours=5))
        self._seed("fresh", now - timedelta(minutes=1))

        removed = self.main._sweep_session_memory_once(now=now)

        self.assertEqual(removed, 2)
        self.assertNotIn("stale-1", self.main.SESSION_MEMORY)
        self.assertNotIn("stale-2", self.main.SESSION_MEMORY)
        self.assertIn("fresh", self.main.SESSION_MEMORY)

    def test_update_refreshes_last_active(self):
        """update_session_memory 刷新 lastActive, 重新进入 TTL 窗口."""
        now = datetime.now()
        sid = "s-refresh"
        self._seed(sid, now - timedelta(minutes=29))
        self.main.update_session_memory(sid, {"lastSpot": "大佛广场"})

        # 29 分钟后仍活跃 (lastActive 已被刷新)
        self.assertEqual(self.main._sweep_session_memory_once(now=now + timedelta(minutes=29)), 0)
        self.assertIn(sid, self.main.SESSION_MEMORY)
        # 再过 31 分钟不活跃 → 回收
        self.assertEqual(self.main._sweep_session_memory_once(now=now + timedelta(minutes=60)), 1)
        self.assertNotIn(sid, self.main.SESSION_MEMORY)

    def test_get_session_memory_creates_with_defaults(self):
        mem = self.main.get_session_memory("brand-new")
        self.assertEqual(mem["interest"], "history")
        self.assertIsNotNone(mem["createdAt"])
        self.assertIsNotNone(mem["lastActive"])
        # 再次获取返回同一对象
        self.assertIs(mem, self.main.get_session_memory("brand-new"))

    def test_guard_thread_never_starts_in_tests(self):
        """测试基建屏蔽 Thread.start → 后台回收线程未启动, 不会污染测试."""
        self.assertFalse(self.main._session_memory_sweep_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
