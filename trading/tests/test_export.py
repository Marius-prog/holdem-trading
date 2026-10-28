import csv
import io
from uuid import uuid4

from trading.models import Order

from .base import HEADERS, ApiTestCase


class FillsCsvTests(ApiTestCase):
    def rows(self, response):
        return list(csv.reader(io.StringIO(response.text)))

    def test_buy_and_sell_rows_newest_first(self):
        self.buy("AAPL", 2)
        sell = {
            "symbol": "AAPL",
            "side": "sell",
            "quantity": 1,
            "client_order_id": str(uuid4()),
        }
        posted = self.client.post("/api/orders", json=sell, headers=HEADERS)
        self.assertEqual(posted.status_code, 200)
        response = self.client.get("/api/fills.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response.headers["content-disposition"], 'attachment; filename="paperdesk-fills.csv"'
        )
        rows = self.rows(response)
        self.assertEqual(rows[0], ["time", "symbol", "side", "quantity", "price", "reason"])
        self.assertEqual([row[1:] for row in rows[1:]], [
            ["AAPL", "sell", "1", "227.52", "Manual"],
            ["AAPL", "buy", "2", "227.52", "Manual"],
        ])
        self.assertRegex(rows[1][0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")

    def test_automatic_orders_use_activity_labels(self):
        self.buy("AAPL", 2)
        Order.objects.update(trigger="dca_burst")
        self.assertEqual(self.rows(self.client.get("/api/fills.csv"))[1][5], "DCA burst")

    def test_empty_export_has_only_header(self):
        self.assertEqual(len(self.rows(self.client.get("/api/fills.csv"))), 1)

    def test_refuses_foreign_host(self):
        response = self.client.get("/api/fills.csv", headers={"Host": "evil.example:8765"})
        self.assertEqual(response.status_code, 400)
