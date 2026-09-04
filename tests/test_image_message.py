import base64
import json
import unittest

from main import XianyuLive


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, value):
        self.messages.append(json.loads(value))


class ImageMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_message_uses_current_conversation_and_receiver(self):
        live = XianyuLive.__new__(XianyuLive)
        live.myid = "seller-1"
        websocket = FakeWebSocket()
        await live.send_image_msg(
            websocket,
            "chat-1",
            "buyer-1",
            "https://img.alicdn.com/menu.jpg",
            1080,
            1440,
        )
        self.assertEqual(1, len(websocket.messages))
        message = websocket.messages[0]
        self.assertEqual("chat-1@goofish", message["body"][0]["cid"])
        self.assertEqual(
            ["buyer-1@goofish", "seller-1@goofish"],
            message["body"][1]["actualReceivers"],
        )
        self.assertEqual(2, message["body"][0]["content"]["custom"]["type"])
        payload = json.loads(
            base64.b64decode(
                message["body"][0]["content"]["custom"]["data"]
            ).decode("utf-8")
        )
        self.assertEqual(2, payload["contentType"])
        self.assertEqual(1080, payload["image"]["pics"][0]["width"])
        self.assertEqual(1440, payload["image"]["pics"][0]["height"])


if __name__ == "__main__":
    unittest.main()
