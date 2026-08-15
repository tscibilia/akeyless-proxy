import tempfile
import unittest
from pathlib import Path

from proxy import read_credential


class TestReadCredential(unittest.TestCase):
    def test_direct_env_var_wins(self):
        env = {"X": "direct", "X_FILE": "/nonexistent"}
        self.assertEqual(read_credential(env, "X"), "direct")

    def test_file_variant_is_read_and_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "secret"
            p.write_text("  from-file\n")
            self.assertEqual(
                read_credential({"X_FILE": str(p)}, "X"), "from-file"
            )

    def test_returns_none_when_neither_is_set(self):
        self.assertIsNone(read_credential({}, "X"))


class TestMain(unittest.TestCase):
    def test_exits_non_zero_without_credentials(self):
        from proxy import main

        self.assertNotEqual(main(env={}), 0)


if __name__ == "__main__":
    unittest.main()
