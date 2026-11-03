"""Tests for utils and FastAPI room server."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from air_ferry.server import create_app
from air_ferry.utils import safe_filename, unique_destination


class UtilsTests(unittest.TestCase):
    def test_safe_filename(self) -> None:
        self.assertEqual(safe_filename("../x/y.txt"), "y.txt")
        with self.assertRaises(ValueError):
            safe_filename("..")

    def test_unique_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = unique_destination(root, "a.txt")
            a.write_text("1", encoding="utf-8")
            self.assertEqual(unique_destination(root, "a.txt").name, "a_1.txt")


class WebAppTests(unittest.TestCase):
    def test_health_and_room_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                port=8765,
                dest_dir=Path(tmp),
                instance_name="t",
                enable_mdns=False,
            )
            payload = b"x" * 2000
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/health").json()["status"], "ok")
                self.assertIn("air-ferry", client.get("/").text)
                self.assertEqual(client.get("/api/qr.svg").status_code, 200)

                with client.websocket_connect("/ws/ui") as a, client.websocket_connect(
                    "/ws/ui"
                ) as b:
                    self.assertEqual(a.receive_json()["type"], "hello")
                    a.send_json({"type": "hello", "label": "Phone"})
                    self.assertEqual(b.receive_json()["type"], "hello")
                    b.send_json({"type": "hello", "label": "PC"})

                    fid = "f-test"
                    a.send_json(
                        {"type": "share", "id": fid, "filename": "n.bin", "size": len(payload)}
                    )

                    def wait(ws, wanted):
                        for _ in range(40):
                            msg = ws.receive_json()
                            if msg.get("type") == wanted:
                                return msg
                        self.fail(wanted)

                    self.assertEqual(wait(a, "accept")["id"], fid)
                    a.send_bytes(payload)
                    a.send_json({"type": "done", "id": fid})
                    self.assertEqual(wait(a, "complete")["filename"], "n.bin")
                    file_evt = wait(b, "file")
                    dl = client.get(file_evt["file"]["download_url"])
                    self.assertEqual(dl.content, payload)


if __name__ == "__main__":
    unittest.main()
