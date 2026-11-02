"""Exercise saved-login startup and user cancellation with a deferred Secret Service."""

import argparse
from contextlib import ExitStack
from itertools import count
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import credentials
import viewer


@unittest.skipIf(credentials.Secret is None, "libsecret bindings are required")
class SavedLoginTests(unittest.TestCase):
    application_ids = count()

    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        directory = self.resources.enter_context(tempfile.TemporaryDirectory(prefix="cctv-login-check-"))
        config = Path(directory) / "settings.json"
        config.write_text(json.dumps(dict(viewer.DEFAULTS, host="127.0.0.1", port=9,
                                         username="test-user", remember=True)))
        self.resources.enter_context(patch.object(viewer, "CONFIG", config))
        self.resources.enter_context(patch.dict(os.environ))
        os.environ.pop("CCTV_PASSWORD", None)
        self.lookups = []
        self.clears = []
        self.resources.enter_context(patch.object(credentials.Secret, "password_lookup",
                                                  side_effect=lambda *args: self.lookups.append(args[-2])))
        self.resources.enter_context(patch.object(credentials.Secret, "password_lookup_finish",
                                                  return_value="synthetic-password"))
        self.resources.enter_context(patch.object(credentials.Secret, "password_clear",
                                                  side_effect=lambda *args: self.clears.append(args[-2])))
        self.resources.enter_context(patch.object(credentials.Secret, "password_clear_finish", return_value=True))
        self.app = viewer.Viewer(argparse.Namespace(demo=False, smoke_test=False))
        self.app.set_application_id(f"io.local.CCTVViewer.LoginTest{next(self.application_ids)}")
        self.assertTrue(self.app.register(None))
        self.app.activate()
        self.addCleanup(self.app.window.close)

    def test_disconnect_cancels_pending_automatic_connection(self):
        self.app.activate_action("disconnect", None)
        self.lookups.pop()(None, None, None)
        self.assertEqual(self.app.content_stack.get_visible_child_name(), "empty")
        self.assertEqual(self.app.password, "")

    def test_saved_login_still_connects_when_not_cancelled(self):
        self.lookups.pop()(None, None, None)
        self.assertEqual(self.app.content_stack.get_visible_child_name(), "cameras")
        self.assertEqual(self.app.password, "synthetic-password")

    def test_forgotten_login_cannot_be_restored_by_pending_lookup(self):
        self.app.activate_action("forget-login", None)
        self.clears.pop()(None, None, None)
        self.lookups.pop()(None, None, None)
        self.assertEqual(self.app.content_stack.get_visible_child_name(), "empty")
        self.assertEqual(self.app.password, "")

    def test_forget_cancels_connection_before_keyring_deletion_finishes(self):
        self.app.activate_action("forget-login", None)
        self.lookups.pop()(None, None, None)
        self.assertEqual(self.app.content_stack.get_visible_child_name(), "empty")
        self.assertEqual(self.app.password, "")


if __name__ == "__main__":
    unittest.main()
