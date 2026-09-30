# Paper Trader

Virtual swing trading for NSE stocks. You place buy orders with a stop loss, a
watchman in the cloud checks the market every 5 minutes, and the app records
every fill, stop-loss hit, gap down, charge and rupee of profit or loss.

- Market and limit buy orders, with stop loss required on every trade
- Stop loss can be moved any time; profit booking is manual, full or partial
- Gap down below your SL exits at the opening price, like real life
- Real Zerodha delivery charges (STT, stamp duty, exchange, SEBI, GST, DP)
- Starting balance ₹1,00,000; buying locks money like a real broker
- Full history: buy/sell times, what happened, gross and net P&L, win rate, results by setup

Free to run: **GitHub Actions** (watchman), **Neon** (database), **Streamlit Community Cloud** (screen).

---

## Setup (about 20 minutes, once)

### 1. Put the code on GitHub

Unzip to a folder **outside** `E:\Trading` (for example `E:\paper-trader`), because
it needs its own repository. Create a new **public** repo called `paper-trader` on
GitHub (public = unlimited free Actions minutes; your database password stays secret). Then:

```
cd E:\paper-trader
git init
git add .
git commit -m "Paper trader v1"
git branch -M main
git remote add origin https://github.com/AnantThakur06/paper-trader.git
git push -u origin main
```

### 2. Create the free database (Neon)

1. Go to **neon.com**, sign up with your GitHub account.
2. Create a project: name `paper-trader`, region **AWS Asia Pacific (Singapore)** (closest to India).
3. Click **Connect**, copy the connection string. It starts with `postgresql://`.

### 3. Give GitHub the database address

Repo on GitHub → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Name: `DATABASE_URL`. Value: the Neon string. Save.

### 4. Test that GitHub can get prices (important)

Repo → **Actions** tab (click the green "enable" button if GitHub asks) →
**Data test** → **Run workflow**. After about a minute:

- Green tick = Yahoo works from GitHub. Open the run to see the prices it got.
- Red cross = Yahoo blocked GitHub's server. See "If something goes wrong" below.

The **Watchman** workflow now runs by itself every 5 minutes, Monday to Friday,
09:17–16:12 IST. To test it right away: Actions → Watchman → Run workflow.

### 5. Put the screen online (Streamlit Community Cloud)

1. Go to **share.streamlit.io**, sign in with GitHub.
2. **Create app** → deploy from GitHub → repo `AnantThakur06/paper-trader`, branch `main`, file `app.py`.
3. **Advanced settings** → Python version **3.12** → in **Secrets** paste:
   ```
   DATABASE_URL = "postgresql://...your Neon string..."
   APP_PASSWORD = "pick-a-password"
   ```
4. **Deploy**. Bookmark the link on your phone.

If you don't open the app for about 12 hours, Streamlit puts it to sleep. Press the
"wake up" button and wait a minute. Your trades keep being checked by the watchman meanwhile.

---

## Daily use

- **Trade page**: place orders (quick −1/−2/−3% stop loss, or size by risk), see the cost,
  charges and your loss if the SL hits. Below: **Active trades** with room to SL, P&L,
  and Move SL / Sell on each row, then **Pending orders** with Cancel.
- **History page**: one row per finished trade with what happened, stats (win rate, charges
  as % of profit), P&L over time, results by setup, filters, and CSV download.
- **Settings page**: reset the account, auto-check status, charge rates.

The app also checks your trades every time you open it, so the screen is correct even
if GitHub's schedule runs late.

## How the app decides what happened

| Situation | What the app does |
|---|---|
| Market order, market open | Buys at the latest price |
| Market order, market closed | Buys at the next day's opening price (money blocked with a 5% buffer) |
| Limit order | Buys when the price comes down to your limit. If the stock opens below it, you get the better opening price |
| Price touches your SL during the day | Sells all remaining shares at your SL price |
| Stock opens below your SL (gap down) | Sells at the opening price, so the loss is bigger, like real life |
| Buy and SL touch in the same minute | Assumes the SL hit too (it can't know the order inside one minute, so it stays cautious) |
| You sell while market is open | Sells at the latest price |
| You sell while market is closed | Sells at the next opening price |
| You move the SL | The new SL counts from the moment you changed it, not before |

It checks 1-minute candles, so a quick dip to your SL between two checks is still caught.

**Charges** (`papertrade/config.py`, from zerodha.com/charges): brokerage ₹0, STT 0.1% on
buy and sell, NSE 0.00307%, SEBI ₹10/crore, stamp duty 0.015% on buy, GST 18% on
exchange + SEBI + brokerage, DP ₹15.34 once per stock per day on sell. Using a different
broker? Change the numbers in `config.py`.

## Limits to know

- **Prices are about 15 minutes behind** the live market (free Yahoo data).
- **GitHub's schedule can run late** (5–30 minutes at busy times). Nothing is missed;
  late checks catch up on every candle since the last check.
- **Stop loss is treated as "sell at market"**, the worst case. A real GTT limit order
  might not fill at all in a big gap, leaving you still holding the stock.
- **Not modelled:** upper/lower circuits, stock splits and bonuses, and the T+1
  settlement delay on sell money.
- **Long trades only** (buy first, sell later), NSE stocks only.

## Try it on your PC (optional)

```
cd E:\paper-trader
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Without secrets this uses a local file (`paper_trades.db`), separate from your cloud
account. To use the cloud account from your PC, copy `.streamlit/secrets.toml.example`
to `.streamlit/secrets.toml` and fill it in.

Run the tests: `pip install pytest` then `python -m pytest -q` (35 tests: all trading rules and every page).

## If something goes wrong

| What you see | What to do |
|---|---|
| Data test is red / "data problem" in the app | Yahoo blocked the server. Wait a few hours and run it again. If it stays red, change `yfinance==1.7.0` to the newest version in both requirements files and push |
| "Couldn't find SYMBOL on NSE" | Use the NSE symbol exactly (e.g. TMPV, not TATAMOTORS; M&M; BAJAJ-AUTO) |
| Auto-check warning in the app | GitHub is running late or the workflow is off. Actions → Watchman → check it's enabled |
| App says "Wrong password" | Check `APP_PASSWORD` in Streamlit → app settings → Secrets |
| Watchman red: "DATABASE_URL is not set" | Step 3 again: the secret name must be exactly `DATABASE_URL` |

## Project layout

```
app.py                  starts the app: sidebar, market bar, page menu
ui.py                   pieces shared by all pages (account card, index strip)
views/trade.py          Trade page: place order, active trades, pending orders
views/history.py        History page: finished trades, stats, P&L curve
views/settings.py       Settings page: reset, auto-check status, charges
watchman.py             the 5-minute checker (GitHub Actions runs it)
check_data.py           one-time Yahoo data test
papertrade/engine.py    the rules: fills, stop loss, gaps (no internet, no database)
papertrade/service.py   orders, sells, balance, history
papertrade/charges.py   Indian delivery charges
papertrade/prices.py    Yahoo Finance data
papertrade/db.py        database tables (SQLite on PC, PostgreSQL in the cloud)
papertrade/config.py    numbers you may want to change
tests/                  35 tests: trading rules with hand-checked numbers, plus a click-through of every page
```
