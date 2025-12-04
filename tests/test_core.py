import queue
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from src.core import SearchEngine


class TestSearchEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        (self.root / "script.py").write_text(
            "def hello():\n    print('world')", encoding="utf-8"
        )
        (self.root / "readme.txt").write_text("Nothing here", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_basic_search(self):
        q = queue.Queue()
        stop = threading.Event()
        opts = {"text": "print", "regex": False}

        engine = SearchEngine(self.root, opts, ["*.py"], [], stop)
        engine.run(q)

        matches = [m for msg, m in list(q.queue) if msg == "match"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].path.name, "script.py")


if __name__ == "__main__":
    unittest.main()
