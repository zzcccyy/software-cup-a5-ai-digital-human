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


class FtsSyncUnitTests(unittest.TestCase):
    """P1.1: FTS5 external-content + 触发器保持知识库/FAQ 索引与真实表同步."""

    @classmethod
    def setUpClass(cls):
        import database
        cls.db = database
        reset_database_module(database)

    def _match_count(self, table, query):
        conn = self.db.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (query,)
        ).fetchone()[0]

    def _fts_rowids(self, table):
        conn = self.db.get_conn()
        return [r[0] for r in conn.execute(f"SELECT rowid FROM {table} ORDER BY rowid")]

    def _source_rowids(self, table):
        conn = self.db.get_conn()
        return [r[0] for r in conn.execute(f"SELECT rowid FROM {table} ORDER BY rowid")]

    def test_add_syncs_knowledge_fts(self):
        item = self.db.add_knowledge(
            title="灵山大佛高度", category="景点讲解", tags=["大佛"],
            content="灵山大佛高88米，是世界上最高的露天青铜立佛。", source="t",
        )
        conn = self.db.get_conn()
        rowid = conn.execute("SELECT rowid FROM knowledge WHERE id=?", (item["id"],)).fetchone()[0]
        self.assertEqual(self._match_count("knowledge_fts", "灵山大佛高度"), 1)
        self.assertEqual(self._fts_rowids("knowledge_fts"), [rowid])

    def test_update_knowledge_resyncs_fts(self):
        item = self.db.add_knowledge(
            title="九龙灌浴表演", category="景点讲解", tags=[], content="九龙灌浴是音乐喷泉表演。", source="t",
        )
        self.assertTrue(self.db.update_knowledge(item["id"], title="梵宫吉祥颂演出", content="梵宫《吉祥颂》是必看演出。"))
        self.assertEqual(self._match_count("knowledge_fts", "九龙灌浴表演"), 0, "旧词不应再命中")
        self.assertEqual(self._match_count("knowledge_fts", "梵宫吉祥颂演出"), 1, "新词应命中")
        self.assertEqual(self._fts_rowids("knowledge_fts"), self._source_rowids("knowledge"))

    def test_delete_knowledge_removes_fts_row(self):
        item = self.db.add_knowledge(
            title="五印坛城文化", category="景点讲解", tags=[], content="五印坛城展示藏传佛教文化。", source="t",
        )
        conn = self.db.get_conn()
        rowid = conn.execute("SELECT rowid FROM knowledge WHERE id=?", (item["id"],)).fetchone()[0]
        self.assertTrue(self.db.delete_knowledge(item["id"]))
        self.assertEqual(self._fts_rowids("knowledge_fts"), self._source_rowids("knowledge"), "删除后无幽灵行")
        self.assertEqual(self._match_count("knowledge_fts", "五印坛城文化"), 0)

    def test_faq_add_update_delete_sync(self):
        item = self.db.add_faq("门票多少钱？", "成人票210元。", keywords=["门票"])
        self.assertEqual(self._match_count("faq_fts", "门票多少钱"), 1)
        self.assertTrue(self.db.update_faq(item["id"], question="门票价格是多少？"))
        self.assertEqual(self._match_count("faq_fts", "门票多少钱"), 0)
        self.assertEqual(self._match_count("faq_fts", "门票价格是多少"), 1)
        self.assertTrue(self.db.delete_faq(item["id"]))
        self.assertEqual(self._fts_rowids("faq_fts"), self._source_rowids("faq"), "FAQ 删除后无幽灵行")

    def test_fts_counts_match_source_after_ops(self):
        conn = self.db.get_conn()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM faq_fts").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM faq").fetchone()[0],
        )

    def test_init_db_repopulates_empty_fts(self):
        """FTS 索引缺失行时 init_db 必须重灌(迁移/中断恢复场景)."""
        self.db.add_knowledge(title="灵山广场导览", category="景点讲解", tags=[], content="灵山广场是开场导览点。", source="t")
        conn = self.db.get_conn()
        conn.execute("DELETE FROM knowledge_fts")
        conn.commit()
        self.db.init_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
        )
        self.assertEqual(self._match_count("knowledge_fts", "灵山广场导览"), 1)

    def test_search_knowledge_fts_contract_and_no_cache_pollution(self):
        item = self.db.add_knowledge(
            title="灵山大佛高度", category="景点讲解", tags=["大佛", "高度"],
            content="灵山大佛高88米，是世界上最高的露天青铜立佛。", source="t",
        )
        results = self.db.search_knowledge_fts("灵山大佛高度", limit=5)
        self.assertTrue(results, "MATCH 路径应返回结果")
        for r in results:
            self.assertIn("score", r)
            self.assertIn("id", r)
        self.assertIn(item["id"], {r["id"] for r in results}, "新入库条目应出现在结果中")
        self.assertTrue(all(r["score"] >= 0.5 for r in results), "FTS 命中评分应通过 0.5 置信度闸门")
        cached = self.db.get_all_knowledge(use_cache=True)
        self.assertTrue(cached)
        self.assertNotIn("score", cached[0], "score 不得污染 TTL 缓存")

    def test_search_knowledge_fts_short_query_fallback(self):
        self.db.add_knowledge(title="门票价格", category="服务信息", tags=[], content="成人票210元。", source="t")
        results = self.db.search_knowledge_fts("门票", limit=5)
        self.assertEqual(results, [], "2字查询低于trigram下限, 回退打分器语义不变")


if __name__ == "__main__":
    unittest.main()
