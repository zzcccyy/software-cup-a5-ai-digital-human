import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

try:
    from test_quality_regression import reset_database_module  # discover 模式
except ModuleNotFoundError:
    from tests.test_quality_regression import reset_database_module  # 包模式


class IndexUnitTests(unittest.TestCase):
    """P1.4: 关键查询必须走索引, 不再有 TEMP B-TREE 全表排序."""

    @classmethod
    def setUpClass(cls):
        import database
        cls.db = database
        reset_database_module(database)

    def test_required_indexes_exist(self):
        conn = self.db.get_conn()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for expected in (
            "idx_knowledge_updated_at",
            "idx_faq_question",
            "idx_admin_logs_action_ts",
            "idx_admin_logs_resource_ts",
        ):
            self.assertIn(expected, names)

    def _plan_text(self, sql, params=()):
        conn = self.db.get_conn()
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return " ".join(r["detail"] for r in rows)

    def test_knowledge_list_no_temp_btree(self):
        plan = self._plan_text("SELECT * FROM knowledge ORDER BY updated_at DESC LIMIT 10")
        self.assertIn("USING INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_faq_by_question_uses_index(self):
        plan = self._plan_text(
            "SELECT keywords FROM faq WHERE question=?", ("门票多少钱？",)
        )
        self.assertNotIn("SCAN", plan)
        self.assertIn("USING INDEX", plan)

    def test_operation_logs_action_uses_composite_index(self):
        plan = self._plan_text(
            "SELECT * FROM admin_operation_logs WHERE action=? ORDER BY timestamp DESC LIMIT 10",
            ("login",),
        )
        self.assertIn("USING INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_operation_logs_resource_uses_composite_index(self):
        plan = self._plan_text(
            "SELECT * FROM admin_operation_logs WHERE resource=? ORDER BY timestamp DESC LIMIT 10",
            ("knowledge",),
        )
        self.assertIn("USING INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plan)


if __name__ == "__main__":
    unittest.main()
