# Paperdesk candidate evidence

Owner task: qitem-20260930173923-9f6397fd. Scope: all new files in
`trading-platform/`; no changes to existing OpenRig product code or dependencies.
Candidate fingerprint: `evidence/candidate.sha256` (paths relative to this folder).
No commit or publish performed.

## Implemented contract

Actual Django Bolt server; one local persisted paper account with $100,000 cash;
six fixed simulated stock quotes; browser buy/sell with whole shares; Decimal
accounting and atomic updates; history and holdings; rejected invalid orders;
UUID idempotency including browser retry after uncertain transport failure.
README contains install, initialization, server and test commands.

## Owner checks, 2026-09-30

From `trading-platform/`:

- `.venv/bin/python manage.py test -v 1`: 9 tests, OK, 0.133 seconds. Tests cover
  API and ledger buy/sell/close, invalid body values, insufficient funds/shares,
  rollback, replay/conflict, required write header, no permissive CORS, initial
  state, history bound and dashboard render. Bolt logs expected validation
  tracebacks for malformed request tests; assertions verify HTTP 422.
- `.venv/bin/python manage.py check`: no issues.
- `.venv/bin/python manage.py makemigrations --check --dry-run`: no changes.
- `ruff check trading-platform` from repo root: all checks passed.
- `uvx pyright`: 0 errors, warnings or information. Untyped library inference
  is disabled in pyproject; this does not prove Django's dynamic field types.
- Inline dashboard JavaScript extracted and checked with `node --check`: exit 0.
- Backend implementation agent additionally ran two file-backed concurrency
  probes: concurrent MSFT purchases cannot overdraw; a concurrent repeated UUID
  creates one fill. These probes were observed by that agent, not rerun by owner.

## Actual browser journey

Started Bolt on `127.0.0.1:8765` against
`/private/tmp/paperdesk-owner-smoke.sqlite3`, a separate smoke database.

1. Initial state: cash and total $100,000.00; six quotes; no holdings or history.
2. Buy two AAPL at $227.52: cash $99,544.96; two shares worth $455.04.
3. Sell one AAPL: cash $99,772.48; one share worth $227.52; total $100,000.00.
4. Reload: same cash and holdings; both history rows persisted.
5. Attempt 100,000 AAPL buy: insufficient cash shown. Attempt two-share sale
   when only one is owned: insufficient shares shown. Cash/history unchanged.
6. Mobile at 390×844: document width 390; market selection updates ticket and
   scrolls to it. Desktop at 1440×1000: document width 1440.
7. Simulated lost response after the server filled one MSFT order: browser
   displayed safe retry. Reload preserved pending order; retry produced just
   one MSFT fill, with total history growing from two to three and cash $99,343.72.

Screenshots: repository `.playwright-mcp/paperdesk-desktop-initial.png` and
`.playwright-mcp/paperdesk-mobile.png`. Visual inspection passed; no supplied
reference image. Browser console errors corresponded to deliberately rejected
HTTP requests and the deliberately aborted transport; no JavaScript exception
was observed in the successful journey.

## QA findings resolved (dev-check qitem-20260930183045-c8661ce7)

dev-check accepted the first candidate with three low findings. Owner fixes:

- F1 Host header: Bolt routes skipped `ALLOWED_HOSTS`, so a DNS-rebinding page
  could read state and trade. `/api/state` and `/api/orders` now refuse a Host
  not in `ALLOWED_HOSTS` with 400 (Django `validate_host`). New test
  `test_foreign_host_is_refused` failed before the fix (200 != 400). Live on a
  fresh DB: `Host: evil.example` order 400 and state 400 with the ledger untouched,
  while `localhost` and `127.0.0.1` succeed. Tests now use `base_url="http://testserver"`.
- F2 320px nav: added a `max-width:360px` rule to tighten nav padding. Chromium
  document width is 320 at 320 (was 329), 360 at 360 and 390 at 390. Nav links
  are 29px tall.
- F3 manifest: `.DS_Store` files removed from `evidence/candidate.sha256`.

Rechecked: 10 tests OK, `ruff check trading-platform` passed, `uvx pyright` 0 errors.

## Limits

Local shared account only. No authentication, broker, live quotes, price movement,
fees, fractional shares, limit orders, deployment, or production hardening.
No paid/live financial operation was performed. Independent dev-check review is
still required before final acceptance.
