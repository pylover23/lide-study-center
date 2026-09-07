import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GitHubPackagingTests(unittest.TestCase):
    def test_example_config_exists_and_has_no_real_credentials(self):
        example_path = ROOT / "config.example.json"

        data = json.loads(example_path.read_text(encoding="utf-8"))

        self.assertEqual(data["username"], "your_student_id")
        self.assertEqual(data["password"], "your_password")
        self.assertIsInstance(data["rooms"], list)
        self.assertGreaterEqual(len(data["rooms"]), 1)
        self.assertIn("sliderAttempts", data)
        self.assertIn("preWarmSec", data)

    def test_gitignore_excludes_local_state_and_build_outputs(self):
        ignored = set()
        for raw_line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                ignored.add(line)

        required_patterns = {
            "config.json",
            "state.json",
            "logs/",
            "artifacts/",
            "build/",
            "dist/",
            "*.exe",
            "captcha_*.png",
            "_probe_*.py",
            ".venv/",
            "venv/",
            ".env",
            "*.py[cod]",
            "**/__pycache__/",
        }
        self.assertTrue(required_patterns.issubset(ignored))


if __name__ == "__main__":
    unittest.main()
