from __future__ import annotations

import io
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from agent_runtime_platform.infrastructure.codex_login import main


class CodexLoginTests(unittest.TestCase):
    def test_existing_chatgpt_session_needs_no_new_login(self):
        class StubCodex:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def account(self):
                return types.SimpleNamespace(
                    account=types.SimpleNamespace(root=types.SimpleNamespace(type="chatgpt"))
                )

            def login_chatgpt_device_code(self):
                raise AssertionError("Device login should not start for a signed-in user.")

        output = io.StringIO()
        with patch.dict(sys.modules, {"openai_codex": types.SimpleNamespace(Codex=StubCodex)}):
            with redirect_stdout(output):
                status = main()
        self.assertEqual(status, 0)
        self.assertIn("zaten açık", output.getvalue())

    def test_new_user_can_complete_device_login_via_sdk(self):
        class StubLogin:
            verification_url = "https://example.invalid/device"
            user_code = "TEST-CODE"

            def wait(self):
                return types.SimpleNamespace(success=True)

        class StubCodex:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def account(self):
                return types.SimpleNamespace(account=None)

            def login_chatgpt_device_code(self):
                return StubLogin()

        output = io.StringIO()
        with patch.dict(sys.modules, {"openai_codex": types.SimpleNamespace(Codex=StubCodex)}):
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                status = main()
        self.assertEqual(status, 0)
        self.assertIn(StubLogin.verification_url, output.getvalue())
        self.assertIn(StubLogin.user_code, output.getvalue())
        self.assertIn("tamamlandı", output.getvalue())


if __name__ == "__main__":
    unittest.main()
