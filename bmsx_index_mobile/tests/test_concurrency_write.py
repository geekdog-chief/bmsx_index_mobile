import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from update_index import save_json, save_text, load_json


class ConcurrentWriteTests(unittest.TestCase):
    """原实现固定.tmp名并发写同路径必崩（FileNotFoundError）。uuid临时名+os.replace必须扛住。"""

    def test_concurrent_save_json_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forum_cache.json"
            errors = []
            barrier = threading.Barrier(16)

            def worker(i):
                try:
                    barrier.wait(timeout=10)
                    save_json(path, {"thread": i, "items": list(range(50))})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            data = load_json(path, {})
            self.assertIn("thread", data)
            # 无残留tmp
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "forum_cache.json"]
            self.assertEqual(leftovers, [])

    def test_concurrent_save_text_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.js"
            errors = []
            barrier = threading.Barrier(16)

            def worker(i):
                try:
                    barrier.wait(timeout=10)
                    save_text(path, f"window.INDEX_DATA = {{'t': {i}}};")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            self.assertTrue(path.read_text(encoding="utf-8").startswith("window.INDEX_DATA"))


if __name__ == "__main__":
    unittest.main()
