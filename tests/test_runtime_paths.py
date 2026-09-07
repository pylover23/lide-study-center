import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.runtime_paths import ensure_runtime_dirs


class TestRuntimePaths(unittest.TestCase):
    def test_uses_appdata_lide_directory_and_creates_runtime_subdirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"APPDATA": tmp}, clear=False):
                paths = ensure_runtime_dirs()

            root = os.path.join(tmp, "LideStudyCenter")
            self.assertEqual(paths, {
                "root": root,
                "config": os.path.join(root, "gui_config.json"),
                "state": os.path.join(root, "state.json"),
                "logs": os.path.join(root, "logs"),
                "artifacts": os.path.join(root, "artifacts"),
            })
            self.assertTrue(os.path.isdir(paths["logs"]))
            self.assertTrue(os.path.isdir(paths["artifacts"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
