import sys
import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.execution import SubprocessCommandExecutor
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.domain.models import CommandSpec


class SubprocessCommandExecutorTests(unittest.TestCase):
    def test_complete_stdout_and_stderr_are_saved_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            executor = SubprocessCommandExecutor(storage)
            result = executor.execute(
                CommandSpec(
                    (sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"),
                    timeout_seconds=5,
                ),
                root,
            )

            self.assertTrue(result.succeeded)
            log = storage.load(result.stdout).decode()
            self.assertIn("out", log)
            self.assertIn("err", log)

    def test_missing_executable_is_a_recorded_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            result = SubprocessCommandExecutor(storage).execute(
                CommandSpec(("dprauto-command-that-does-not-exist",), timeout_seconds=5),
                root,
            )

            self.assertEqual(result.exit_code, 127)
            self.assertIn("failed to execute", storage.load(result.stdout).decode())


if __name__ == "__main__":
    unittest.main()
