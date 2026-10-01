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
  below the highest price since then, and it only moves up. Stops round down to
  the cent. When a price change reaches the stop, the whole position sells at
  the new price, which is below the stop after a gap. These sales show as Hard
  stop or Trailing stop in Recent activity.
- Profit-taking: when a price change reaches 15% above average cost, 30% of the
  shares sell; at 25% above, another 30% of the original position sells (3/7 of
  what is left), and the rest rides the trailing stop. Share counts round down,
  each level fires once per position, a gap through both takes both, and the
  levels reset when the position closes. A level whose share count rounds to 0
  stays open (and later levels wait) until the position is large enough. Takes
  run only on price changes, never inside a buy. These sales show as Profit take.
- Ladder buys: when a price change drops a price below the position's entry price
  (its first fill) by a ladder level, more shares buy at the new price. AAPL,
  MSFT and GOOGL buy 10, 15 and 15 shares at 7%, 14% and 21% below entry; AMZN
  and NVDA at 10%, 20% and 30%; TSLA at 15%, 25% and 35%. Levels go in order, a
  gap through several buys them all, and each fires once per position (reset on
  close). A ladder buy shrinks to keep the position's cost basis within
  $14,000 and to the cash on hand; a level that would buy nothing stays open.
  The stop is checked first, ladders wait while the price is at or above the
  average cost or while the kill switch is on, and they run only on price
  changes, never inside a manual buy. These buys show as
  Ladder buy.
- SPY dollar-cost averaging (Strategy 3): SPY is a seventh quote with its own
  stops (a 12% hard stop that becomes a 5% trailing stop at +7%) and no ladders,
  profit-taking or re-entry. **Start SPY DCA** in Market watch buys 4 shares at
  once and 4 more every 7 SPY price changes (one "week"), up to 39 shares or a
  $30,000 position value, then holds. When QQQ crosses back above its average,
  DCA buys the rest up to 39 at once and holds (if cash runs short, the weekly
  buys continue instead). A stop-out starts a new cycle
  that buys again 7 changes later. The kill switch pauses DCA buys, and **Stop
  SPY DCA** ends them while keeping the position. Buys show as DCA buy or DCA
  burst.
- GLD sleeve: GLD ($400.00) runs the same dollar-cost averaging with its own
  settings: **Start GLD DCA** buys 2 shares at once and 2 more every 7 GLD price
  changes, up to 37 shares or a $15,000 position value, with the same stops,
  burst, restart and kill-switch rules. Each plan counts its own symbol's
  changes.
- Market regime: QQQ is a market index you cannot trade. It starts at $480.00,
  moves with every Next tick (before the quotes, so their rules see the new
  regime), and can be set in the Set price form. It keeps its
  last 20 prices (one per change); while QQQ is below their average the market
  is bearish and new ladder buys pause (stops, profit-taking, re-entry and your
  own buys continue). Market watch shows QQQ, its average and the regime.
- Re-entry after a stop-out: each price change of a symbol counts as one trading
  day, and the symbol keeps a 10-change moving average (EMA). After a stop sells
  a whole position, from the next change on, while the price is above its EMA, a
  limit buy for 10 shares is placed 5% below the price (shown in Market watch). A
  later change at or below the limit buys at the new price and opens a fresh
  position; a limit unfilled after 5 more changes is re-priced. The kill switch
  cancels pending re-entries and a manual buy of the symbol replaces them. The
  buy shrinks to the $14,000 cost cap and the cash on hand, and shows as
  Re-entry. A manual sell is not a stop-out.
- Kill switch: the account tracks its highest portfolio value after each price
  change. While the value is more than 15% below that peak, new buys are refused
  and the dashboard says so; sells, stops and profit-taking keep working. Buys
  resume once the value is back at the threshold, or after **Reset kill switch**
  (shown only while buys are halted), which restarts the peak at the current
  value. Closing every position does not lift the halt on its own.
- Congress copy trader (Strategy 2): record a disclosed trade under Congress
  filings (filer, ticker, buy or sell, size range, trade date); nothing is
  scraped or preloaded. JPM, GS and KO filings are mirrored: buys at $500,
  $1,500, $2,500, $4,000 or $5,000 by size range (up to $10,000 cost per stock,
  within cash, paused by the kill switch), sells of a third of the position.
  Trades more than 45 days old, other tickers and duplicates are recorded
  without an order. Duplicates are refused, except a buy the kill switch
  stopped, which can be entered again once buys resume. Copied positions have no hard stop;
  once up 15% a 10% trailing stop covers the whole position (including shares
  copied later), and they get no ladders, profit-taking or re-entry. Orders show as Copy trade.
- Alerts: automatic orders (stops as warnings; profit takes, ladder, re-entry
  and DCA buys as info), a stop switching to trailing, a re-entry limit being
  placed, and the kill switch turning on (critical), off or being reset are
  recorded as alerts, and so is a trailing stop reset to a hard stop by added
  shares. The newest 500 are kept and the dashboard's Alerts panel
  shows the latest 20. Manual orders raise no alerts, and nothing is sent
  outside the app.
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
| `POST /api/quotes` | Set one symbol's (or QQQ's) price, apply the rules, return the new state |
| `POST /api/tick` | Move every price by up to 3%, apply stops, return the new state |
| `POST /api/filings` | `{"filer", "symbol", "side", "bucket": 0-4, "traded_on": "YYYY-MM-DD"}` records a filing and mirrors it |
| `POST /api/dca` | `{"symbol": "SPY" or "GLD", "enabled": true}` starts that plan (buying now if due); `false` stops it |
| `POST /api/kill-switch/reset` | While buys are halted, restart the equity peak at the current value |

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
