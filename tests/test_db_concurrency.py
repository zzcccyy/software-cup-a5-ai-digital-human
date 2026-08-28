import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True


class DbConcurrencyUnitTests(unittest.TestCase):
    """P1.2: 每线程连接复用 + BEGIN IMMEDIATE 重试, 并发读写不泄漏锁异常."""

    N_THREADS = 50

    def setUp(self):
        import database as db
        self.db = db
        # 脱离其他测试类对 get_conn 的 lambda 残留补丁
        db.get_conn = db._get_thread_conn
        db.get_read_conn = db._get_thread_conn
        self._tmp = TemporaryDirectory()
        self._orig_path = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "conc.db"
        self._orig_open = db._open_conn
        self._open_count = 0
        self._count_lock = threading.Lock()

        def _counting_open():
            with self._count_lock:
                self._open_count += 1
            return self._orig_open()

        db._open_conn = _counting_open
        db._reset_thread_conns()
        db.init_db()

    def tearDown(self):
        self.db._open_conn = self._orig_open
        self.db.DB_PATH = self._orig_path
        self.db._reset_thread_conns()
        self._tmp.cleanup()

    def _worker(self, i, barrier, errors, errors_lock):
        try:
            barrier.wait(timeout=30)
            with self.db.get_db() as conn:
                conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
            if i % 2 == 0:
                with self.db.get_db(write=True) as conn:
                    conn.execute(
                        "INSERT INTO conversations (id, session_id, user_id, message, reply, emotion, interest, topics, timestamp, latency_ms) "
                        "VALUES (?, 's', 'guest', 'm', 'r', 'warm', 'history', '[]', ?, 10)",
                        (f"c{i}", self.db.now_str()),
                    )
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(repr(e))
        finally:
            self.db._reset_thread_conns()

    def test_concurrent_reads_and_writes_no_locked_errors(self):
        errors = []
        errors_lock = threading.Lock()
        barrier = threading.Barrier(self.N_THREADS)
        threads = [
            threading.Thread(target=self._worker, args=(i, barrier, errors, errors_lock))
            for i in range(self.N_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [], f"并发下出现异常: {errors}")
        self.assertEqual(self.db.count_conversations(), self.N_THREADS // 2)
        # 连接数有上界: 主线程 1 个 + 每个工作线程至多 1 个
        self.assertLessEqual(self._open_count, self.N_THREADS + 1)

    def test_nested_write_contexts_share_transaction(self):
        with self.db.get_db(write=True) as conn:
            conn.execute(
                "INSERT INTO conversations (id, session_id, user_id, message, reply, emotion, interest, topics, timestamp) "
                "VALUES ('n1', 's', 'guest', 'm', 'r', 'warm', 'history', '[]', ?)",
                (self.db.now_str(),),
            )
            with self.db.get_db(write=True) as inner:
                inner.execute(
                    "INSERT INTO conversations (id, session_id, user_id, message, reply, emotion, interest, topics, timestamp) "
                    "VALUES ('n2', 's', 'guest', 'm', 'r', 'warm', 'history', '[]', ?)",
                    (self.db.now_str(),),
                )
        self.assertEqual(self.db.count_conversations(), 2)

    def test_write_error_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.db.get_db(write=True) as conn:
                conn.execute(
                    "INSERT INTO conversations (id, session_id, user_id, message, reply, emotion, interest, topics, timestamp) "
                    "VALUES ('rb1', 's', 'guest', 'm', 'r', 'warm', 'history', '[]', ?)",
                    (self.db.now_str(),),
                )
                raise RuntimeError("boom")
        self.assertEqual(self.db.count_conversations(), 0, "异常后写入必须回滚")


if __name__ == "__main__":
    unittest.main()
