import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.errors import StorageError


class LocalArtifactStorageTests(unittest.TestCase):
    def test_round_trip_digest_and_immutable_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(Path(directory))
            artifact = storage.save("logs/build.log", b"complete log", media_type="text/plain")

            self.assertTrue(storage.exists(artifact))
            self.assertEqual(storage.load(artifact), b"complete log")
            self.assertEqual(len(artifact.digest or ""), 64)
            self.assertEqual(
                storage.save("logs/build.log", b"complete log", media_type="text/plain"),
                artifact,
            )
            with self.assertRaises(StorageError):
                storage.save("logs/build.log", b"different")

    def test_unsafe_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(Path(directory))
            with self.assertRaises(StorageError):
                storage.save("../outside", b"bad")


if __name__ == "__main__":
    unittest.main()
