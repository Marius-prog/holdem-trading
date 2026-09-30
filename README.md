# Paperdesk

A local paper-trading workspace built with **Django and Django Bolt**. Start with
$100,000 in virtual cash, buy and sell whole shares, and keep a persistent ledger
of filled orders. The dashboard includes cash, portfolio value, positions,
capital allocation, and the latest 100 fills.

This is a single shared practice account for local use. Prices are fixed demo
fixtures, not current market prices. There is no authentication, live feed,
broker connection, real money, short selling, fee, or deployment setup. Do not
expose this development app to a network.

## Start

Requirements: Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

From this directory:

```sh
uv sync --locked
uv run python manage.py migrate
uv run python manage.py runbolt --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/**. Select a company in Market watch, enter a whole
share quantity, and choose **Place paper buy**. Your cash decreases, the position
appears, and the filled order is recorded. Switch to **Sell** to sell shares you
own. **Refresh** reads the current ledger; reloading or restarting the server
preserves trades in SQLite.

The lockfile pins all dependency versions. This directory is independent of the
surrounding OpenRig Node workspace; its setup does not change that workspace.

## Data and order behavior

- A new database receives $100,000 in virtual cash on its first account read.
- The server chooses prices from six fixed simulated quotes; the client cannot
  supply a fill price. Fills are immediate, with no fees.
- Monetary calculations use Decimal. A database transaction updates cash,
  holdings, and the order together. SQLite transactions acquire the write lock
  before reading the balance to serialize concurrent trades.
- Orders cannot exceed available cash or owned shares. Unknown assets, invalid
  sides, and nonpositive, fractional, or excessive quantities are rejected.
- Each order has a client-generated UUID. Retrying that same payload and UUID
  returns the original fill; reusing a UUID for a different order is rejected.
  The browser retains an unconfirmed submission within the tab and offers a
  safe retry before accepting another order.
- Buy/sell at an unchanged demo price leaves total portfolio value unchanged.
  Unrealized profit and loss therefore stays zero. This demo does not model
  price movement, spread, liquidity, exchange hours, or real execution.

SQLite data is excluded from version control. To use a separate database without
changing your existing account, set `PAPER_TRADING_DB` on both commands:

```sh
PAPER_TRADING_DB=/tmp/paperdesk-demo.sqlite3 uv run python manage.py migrate
PAPER_TRADING_DB=/tmp/paperdesk-demo.sqlite3 uv run python manage.py runbolt --host 127.0.0.1 --port 8001
```

## API

| Request | Result |
| --- | --- |
| `GET /` | Browser workspace |
| `GET /api/state` | Account, quotes, holdings, and latest 100 orders |
| `POST /api/orders` | Submit or replay a paper order |

Money is returned as decimal strings. Order writes require JSON and the
`X-Paper-Trade: 1` header. This prevents ordinary cross-origin form submissions;
it is not user authentication. Keep the server on the loopback interface.

```sh
curl http://127.0.0.1:8000/api/state
curl http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Paper-Trade: 1' \
  --data '{"symbol":"AAPL","side":"buy","quantity":2,"client_order_id":"00000000-0000-4000-8000-000000000001"}'
```

Generate a new UUID for each new order; keep the UUID unchanged when retrying an
uncertain request. Repeating the example fills once.

## Verify

```sh
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py test
```

Tests use a separate test database. For a browser smoke check, use a fresh
database, buy two AAPL shares, sell one, refresh, and confirm the cash, position,
and two history rows agree. Attempt a buy beyond available cash and a sale beyond
the remaining share; both should show a clear error without changing the ledger.

The implementation follows the official [Django Bolt quickstart](https://bolt.farhana.li/getting-started/quickstart/).
