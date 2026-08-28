import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

import rag_vector  # noqa: E402

SAMPLE = [
    {"id": "1", "title": "ticket", "category": "service", "tags": ["price"], "content": "adult ticket 210 yuan", "source": "test"},
    {"id": "2", "title": "route", "category": "tour", "tags": ["family"], "content": "family route", "source": "test"},
]

INITIAL_NAME = "scenic_knowledge"


def _query_result(item_id: str, title: str) -> dict:
    return {
        "ids": [[item_id]],
        "documents": [["doc"]],
        "metadatas": [[{"item_id": item_id, "title": title, "category": "c", "tags": "t", "source": "s"}]],
        "distances": [[0.2]],
    }


class ChromaAtomicRebuildTests(unittest.TestCase):
    """P3.4: 原子重建 — 临时集合 add 完再切换, 重建窗口查询永不落空.
    修复后: mock 模拟真实 chroma (集合名字注册表 + 删除不存在的名字抛异常),
    连续重建时必须删除上一代集合本身 (可能已是 tmp_*), 不得硬编码初始名."""

    def setUp(self):
        # 名字注册表: 模拟真实 chroma 的集合存在性语义
        self.names = {INITIAL_NAME}
        self.collections: dict[str, mock.Mock] = {}
        self.client = mock.Mock()

        def _create(name, **kwargs):
            coll = mock.Mock(name=f"coll:{name}")
            coll.name = name
            self.collections[name] = coll
            self.names.add(name)
            return coll

        def _delete(name, **kwargs):
            if name not in self.names:
                raise RuntimeError(f"collection not found: {name}")
            self.names.discard(name)

        self.client.create_collection.side_effect = _create
        self.client.delete_collection.side_effect = _delete

        self.old_client = rag_vector._client
        self.old_using = rag_vector._using_vector
        self.old_collection = rag_vector._chroma_collection
        self.marker_tmp = tempfile.TemporaryDirectory()
        self.marker_path = Path(self.marker_tmp.name) / "active_collection.json"
        self.patch_client = mock.patch.object(rag_vector, "_client", self.client)
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)
        self.patch_marker = mock.patch.object(rag_vector, "_ACTIVE_COLLECTION_MARKER", self.marker_path)
        self.patch_marker.start()
        self.addCleanup(self.patch_marker.stop)
        self.addCleanup(self.marker_tmp.cleanup)
        rag_vector._using_vector = True
        rag_vector._chroma_collection = self.client.create_collection(INITIAL_NAME, embedding_function=None)

    def tearDown(self):
        rag_vector._using_vector = self.old_using
        rag_vector._chroma_collection = self.old_collection

    def test_atomic_swap_then_delete_old(self):
        """先建临时集合 → add → 原子切换 → 最后删旧集合."""
        rag_vector.rebuild_collection(SAMPLE)
        tmp_name = self.client.create_collection.call_args[0][0]
        self.assertTrue(tmp_name.startswith("scenic_knowledge_tmp_"))
        new_coll = self.collections[tmp_name]
        new_coll.add.assert_called_once()
        self.assertIs(rag_vector._chroma_collection, new_coll)
        self.assertEqual(rag_vector._read_active_collection_name(), tmp_name)
        # 旧集合在切换后才删 (初始集合名)
        self.client.delete_collection.assert_called_once_with(INITIAL_NAME)
        self.assertNotIn(INITIAL_NAME, self.names)

    def test_add_failure_keeps_old_collection_and_cleans_tmp(self):
        """add 中途失败: 原集合保持可用, 临时集合被清理."""
        old = rag_vector._chroma_collection  # setUp 替换后的注册集合

        # 注册一个新 tmp 并让它的 add 失败: 需要提前知道名字, 直接覆盖 create 行为
        def _create_failing(name, **kwargs):
            coll = mock.Mock(name=f"coll:{name}")
            coll.name = name
            self.collections[name] = coll
            self.names.add(name)
            coll.add.side_effect = RuntimeError("embedding down")
            return coll

        self.client.create_collection.side_effect = _create_failing
        rag_vector.rebuild_collection(SAMPLE)
        self.assertIs(rag_vector._chroma_collection, old)
        tmp_name = self.client.create_collection.call_args[0][0]
        # 只删了临时集合, 没有动旧集合
        self.client.delete_collection.assert_called_once_with(tmp_name)
        self.assertIn(INITIAL_NAME, self.names)

    def test_marker_write_failure_keeps_old_collection_and_cleans_tmp(self):
        old = rag_vector._chroma_collection
        with mock.patch.object(rag_vector, "_persist_active_collection_name", side_effect=OSError("marker disk full")):
            rag_vector.rebuild_collection(SAMPLE)
        self.assertIs(rag_vector._chroma_collection, old)
        tmp_name = self.client.create_collection.call_args[0][0]
        self.client.delete_collection.assert_called_once_with(tmp_name)
        self.assertIn(INITIAL_NAME, self.names)

    def test_active_collection_marker_is_persisted_and_validated(self):
        rag_vector.rebuild_collection(SAMPLE)
        current_name = rag_vector._chroma_collection.name
        self.assertEqual(rag_vector._read_active_collection_name(), current_name)
        self.marker_path.write_text('{"name":"../../outside"}', encoding="utf-8")
        self.assertEqual(rag_vector._read_active_collection_name(), INITIAL_NAME)

    def test_startup_restores_marker_collection(self):
        restored_name = "scenic_knowledge_tmp_abcdef123456"
        self.marker_path.write_text('{"name":"' + restored_name + '"}', encoding="utf-8")
        restored = mock.Mock()
        restored.name = restored_name
        restored.count.return_value = 7

        def _get(name, **kwargs):
            if name == restored_name:
                return restored
            raise RuntimeError("collection missing")

        self.client.get_collection.side_effect = _get
        collection, count = rag_vector._load_collection()
        self.assertIs(collection, restored)
        self.assertEqual(count, 7)
        self.client.get_collection.assert_called_once_with(restored_name, embedding_function=rag_vector._embedding_function)

    def test_queries_never_empty_during_rebuild(self):
        """重建进行中: 查询走旧集合 (有结果); 切换后: 查询走新集合."""
        old = rag_vector._chroma_collection
        old.query.return_value = _query_result("1", "ticket")
        entered = threading.Event()
        release = threading.Event()

        def slow_add(*args, **kwargs):
            entered.set()
            release.wait(5)

        orig_create = self.client.create_collection.side_effect

        def slow_create(name, **kwargs):
            coll = orig_create(name, **kwargs)
            coll.add.side_effect = slow_add
            return coll

        self.client.create_collection.side_effect = slow_create

        t = threading.Thread(target=rag_vector.rebuild_collection, args=(SAMPLE,))
        t.start()
        self.assertTrue(entered.wait(5))

        # 重建窗口内: 命中旧集合, 不空不抛
        res = rag_vector.search_knowledge_vector("ticket")
        self.assertEqual(res[0]["id"], "1")
        self.assertIs(rag_vector._chroma_collection, old)

        release.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())

        # 切换后: 命中新集合
        tmp_name = self.client.create_collection.call_args[0][0]
        new_coll = self.collections[tmp_name]
        self.assertIs(rag_vector._chroma_collection, new_coll)
        new_coll.query.return_value = _query_result("2", "route")
        res2 = rag_vector.search_knowledge_vector("route")
        self.assertEqual(res2[0]["id"], "2")

    def test_consecutive_rebuilds_clean_old_names(self):
        """连续两次重建: 每次都删掉上一代集合本身 (第二次旧主已是 tmp_*),
        无临时集合残留 (真实 chroma 删除不存在的集合会抛异常)."""
        rag_vector.rebuild_collection(SAMPLE)
        rag_vector.rebuild_collection(SAMPLE)
        delete_names = [c[0][0] for c in self.client.delete_collection.call_args_list]
        self.assertEqual(len(delete_names), 2)
        # 第一代: 初始集合; 第二代: 第一轮的临时集合 (名字以 tmp_ 开头)
        self.assertEqual(delete_names[0], INITIAL_NAME)
        self.assertTrue(delete_names[1].startswith("scenic_knowledge_tmp_"))
        # 注册表仅剩当前主集合 (第二轮的 tmp, 它要继续服务查询, 不应被删)
        current_main = self.client.create_collection.call_args_list[-1][0][0]
        self.assertEqual(self.names, {current_main}, "旧集合全部清理, 仅剩当前主集合")

    def test_empty_vector_result_falls_back_to_keywords(self):
        collection = rag_vector._chroma_collection
        collection.query.return_value = {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        with mock.patch.object(rag_vector, "_fallback_keyword_search", return_value=[{"id": "fallback"}]) as fallback:
            result = rag_vector.search_knowledge_vector("ticket")
        self.assertEqual(result, [{"id": "fallback"}])
        fallback.assert_called_once_with("ticket", 10)


if __name__ == "__main__":
    unittest.main()
