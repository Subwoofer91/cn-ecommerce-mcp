from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from http_server import BearerAuthMiddleware


class BearerAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            return None

        self.app = BearerAuthMiddleware(downstream, "test-secret")

    def test_mcp_rejects_missing_token(self) -> None:
        self.assertFalse(self.app.is_authorized([]))

    def test_mcp_accepts_valid_token(self) -> None:
        headers = [(b"authorization", b"Bearer test-secret")]
        self.assertTrue(self.app.is_authorized(headers))

    def test_authorization_header_name_is_case_insensitive(self) -> None:
        headers = [(b"Authorization", b"Bearer test-secret")]
        self.assertTrue(self.app.is_authorized(headers))

    def test_wrong_token_is_rejected(self) -> None:
        headers = [(b"authorization", b"Bearer wrong-secret")]
        self.assertFalse(self.app.is_authorized(headers))

    def test_anonymous_mode_accepts_missing_token(self) -> None:
        app = BearerAuthMiddleware(self.app.app, None)
        self.assertTrue(app.is_authorized([]))


if __name__ == "__main__":
    unittest.main()
