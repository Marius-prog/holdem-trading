# Paperdesk

A local paper-trading workspace built with **Django and Django Bolt**. Start with
$100,000 in virtual cash, buy and sell whole shares, and keep a persistent ledger
of filled orders. The dashboard includes cash, portfolio value, positions,
capital allocation, and the latest 100 fills.

This is a single shared practice account for local use. Prices are simulated
demo values that you move yourself, not current market prices. There is no
authentication, live feed, broker connection, real money, short selling, fee, or
deployment setup. Do not expose this development app to a network.

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
- Six simulated quotes start at fixed demo prices and persist in SQLite. Set a
  symbol's price in Market watch, or use **Next tick** to move every price by up
  to 3% either way (a nonzero step moves a price at least one cent). Orders fill immediately at the current server price, with no
  fees; an order request cannot supply its fill price.
- Monetary calculations use Decimal. A database transaction updates cash,
  holdings, and the order together. SQLite transactions acquire the write lock
  before reading the balance to serialize concurrent trades.
- Orders cannot exceed available cash or owned shares. Unknown assets, invalid
  sides, and nonpositive, fractional, or excessive quantities are rejected.
- Each order has a client-generated UUID. Retrying that same payload and UUID
  returns the original fill; reusing a UUID for a different order is rejected.
  The browser retains an unconfirmed submission within the tab and offers a
  safe retry before accepting another order.
- Buying more of a held symbol re-averages its cost and restarts its stop as a
  hard stop on the new average. If the price is already 7% above that average,
  the trailing stop starts again from the current price.
- Every position has a stop. It starts as a hard stop 25% below average cost.
  Once the price reaches 7% above average cost, it becomes a trailing stop 12%
  below the highest price since then, and it only moves up. When a price change
  reaches the stop, the whole position sells at the new price, which is below
  the stop after a gap. These sales show as Hard stop or Trailing stop in Recent
  activity.
- Profit-taking: when a price change reaches 15% above average cost, 30% of the
  shares sell; at 25% above, another 30% of the original position sells (3/7 of
  what is left), and the rest rides the trailing stop. Share counts round down,
  each level fires once per position, a gap through both takes both, and the
  levels reset when the position closes. These sales show as Profit take.
- Kill switch: the account tracks its highest portfolio value after each price
  change. While the value is more than 15% below that peak, new buys are refused
  and the dashboard says so; sells, stops and profit-taking keep working, and
  buys resume once the value is back at the threshold.
- Paper cash is capped at $1,000,000,000,000.00 so SQLite keeps it exact to the
  cent. A sale, or a price move whose automatic sale would pass the cap, is refused
  and nothing changes.
- This demo does not model spread, liquidity, exchange hours, or real execution.

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
| `POST /api/quotes` | Set one symbol's price, apply stops, return the new state |
| `POST /api/tick` | Move every price by up to 3%, apply stops, return the new state |

Money is returned as decimal strings. Order and price writes require the
`X-Paper-Trade: 1` header, with JSON bodies. This prevents ordinary cross-origin form submissions;
it is not user authentication. Keep the server on the loopback interface.

```sh
curl http://127.0.0.1:8000/api/state
curl http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Paper-Trade: 1' \
  --data '{"symbol":"AAPL","side":"buy","quantity":2,"client_order_id":"00000000-0000-4000-8000-000000000001"}'
curl http://127.0.0.1:8000/api/quotes \
  -H 'Content-Type: application/json' \
  -H 'X-Paper-Trade: 1' \
  --data '{"symbol":"AAPL","price":"200.00"}'
```

A price is a decimal string in whole cents from 0.01 to 1000000.00.

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
Then set AAPL more than 7% above its average cost, confirm the position's stop
shows TRAIL, and set a price at or below the stop to see a Trailing stop sale.

The implementation follows the official [Django Bolt quickstart](https://bolt.farhana.li/getting-started/quickstart/).
