import unittest

from dprauto.errors import ConfigurationError, ErrorCode


class ErrorTests(unittest.TestCase):
    def test_error_exposes_stable_code_and_details(self) -> None:
        error = ConfigurationError("bad value", details={"field": "timeout"})
        self.assertEqual(error.code, ErrorCode.CONFIGURATION)
        self.assertEqual(error.details["field"], "timeout")
        self.assertIn(ErrorCode.CONFIGURATION.value, str(error))


if __name__ == "__main__":
    unittest.main()
