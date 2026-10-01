from uuid import uuid4

from django.test import TestCase
from django_bolt.testing import TestClient

from trading.api import api

HEADERS = {"X-Paper-Trade": "1"}


class ApiTestCase(TestCase):
    def setUp(self):
        self.client = TestClient(api, base_url="http://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def buy(self, symbol="AAPL", quantity=2):
        payload = {
            "symbol": symbol,
            "side": "buy",
            "quantity": quantity,
            "client_order_id": str(uuid4()),
        }
        response = self.client.post("/api/orders", json=payload, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def set_price(self, symbol, price, headers=HEADERS):
        return self.client.post(
            "/api/quotes", json={"symbol": symbol, "price": price}, headers=headers
        )

    def holding(self, symbol):
        state = self.client.get("/api/state").json()
        return next((item for item in state["holdings"] if item["symbol"] == symbol), None)
