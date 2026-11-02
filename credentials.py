"""Asynchronous account-scoped storage in the desktop Secret Service."""

import gi

try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ValueError, ImportError):
    Secret = None


class Credentials:
    def __init__(self):
        self.available = Secret is not None
        self.schema = Secret.Schema.new(
            "io.local.CCTVViewer", Secret.SchemaFlags.NONE,
            {key: Secret.SchemaAttributeType.STRING for key in ("host", "port", "username")},
        ) if self.available else None

    def attributes(self, settings):
        return {"host": settings["host"].strip().lower(), "port": str(settings["port"]),
                "username": settings["username"]}

    def lookup(self, settings, callback):
        if not self.available:
            callback(None, "Secret Service is unavailable")
            return

        def finished(_source, result, _data):
            try:
                value = Secret.password_lookup_finish(result)
            except Exception:
                callback(None, "Could not read the saved login")
                return
            callback(value, None)

        Secret.password_lookup(self.schema, self.attributes(settings), None, finished, None)

    def store(self, settings, password, callback):
        if not self.available:
            callback(False)
            return

        def finished(_source, result, _data):
            try:
                success = Secret.password_store_finish(result)
            except Exception:
                success = False
            callback(success)

        Secret.password_store(self.schema, self.attributes(settings), Secret.COLLECTION_DEFAULT,
                              "CCTV Viewer login", password, None, finished, None)

    def clear(self, settings, callback):
        if not self.available:
            callback(False)
            return

        def finished(_source, result, _data):
            try:
                Secret.password_clear_finish(result)
            except Exception:
                callback(False)
                return
            callback(True)

        Secret.password_clear(self.schema, self.attributes(settings), None, finished, None)
