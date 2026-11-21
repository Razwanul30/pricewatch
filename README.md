# PriceWatch

A robust, practical Python automation for monitoring e-commerce product prices and stock availability with historical tracking, Telegram alerts, retry resilience, file logging, and automated Linux cron scheduling.

---

## Features

- **Automated Web Scraping**: Extracts product name, current price, currency, and stock status using JSON-LD metadata, OpenGraph/microdata tags, and website-specific/generic HTML fallbacks.
- **Multi-Product Tracking**: Monitors multiple items configured via a simple `products.json` file.
- **Price & Stock History**: Records timestamped scrape results to `data/price_history.csv`.
- **Intelligent Change Detection**: Accurate `Decimal`-based price comparison detecting price drops, price hikes, stock status changes, and target price attainment.
- **Telegram Notifications**: Real-time alerts via Telegram Bot API when price changes, stock status updates, or target prices are met.
- **Alert Deduplication**: Alerts are sent only on meaningful changes or newly reached target prices—never spammed on unchanged runs.
- **Retry Resilience**: Automatic retry mechanism with configurable attempts and backoff delay for transient network hiccups and server errors (5xx/timeouts), while failing immediately on permanent errors (404/invalid URLs).
- **Process Overlap Prevention**: Safe file-based locking (`data/pricewatch.lock`) preventing concurrent cron runs, with automatic stale-lock recovery if a process terminates unexpectedly.
- **Rotated Application Logging**: Structured logging to `logs/pricewatch.log` with automatic log rotation (1 MB limit, 3 backups) to prevent unlimited disk growth.
- **Data Integrity**: Never fabricates prices, never compares different currencies, and never overwrites valid historical data with failed scrape results.

---

## Project Structure

```text
pricewatch/
├── main.py              # CLI entry point (Monitoring & Single URL scrape)
├── scraper.py           # Core web scraping & metadata extraction logic
├── monitor.py           # Monitoring engine, change detection, locking & alerts
├── products.json        # Configured list of tracked products
├── requirements.txt     # Python dependencies
├── .env.example         # Example environment template for Telegram credentials
├── .gitignore           # Git ignore rules for virtualenvs, logs, locks, and secrets
├── data/
│   └── price_history.csv # Persistent price and stock history
├── logs/
│   └── pricewatch.log   # Application log file (automatically rotated)
├── tests/
│   └── test_monitor.py  # Unit and integration test suite (27 tests)
├── .github/
│   └── workflows/
│       └── pricewatch.yml  # GitHub Actions cloud scheduling workflow
└── README.md            # Documentation
```

---

## Installation & Setup

1. **Navigate to the project directory:**
   ```bash
   cd /home/razwanul/Documents/codingWorld/python/pricewatch
   ```

2. **Create and activate a Python virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## Product Configuration

Tracked products are defined in `products.json`:

```json
{
  "products": [
    {
      "name": "A Light in the Attic",
      "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
      "target_price": "50.00"
    },
    {
      "name": "Tipping the Velvet",
      "url": "https://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html",
      "target_price": "55.00"
    }
  ]
}
```

### Configuration Fields:
- `name` *(required)*: Descriptive name of the product.
- `url` *(required)*: Full HTTP or HTTPS URL to the product page.
- `target_price` *(optional)*: Price threshold. An alert triggers when `current_price <= target_price`.

---

## Telegram Bot Setup

To enable automated Telegram alerts:

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and fill in your credentials:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
   TELEGRAM_CHAT_ID=987654321
   ```
3. **How to get your credentials:**
   - **Bot Token**: Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and copy the provided API token.
   - **Chat ID**: Start a chat with your bot, then message [@userinfobot](https://t.me/userinfobot) or open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in your browser to view your `chat_id`.

> **Note**: If Telegram credentials are not configured, PriceWatch will clearly log that Telegram notifications are disabled and safely continue monitoring and recording to CSV without error.

---

## Manual Execution

### Run Full Product Monitoring (Default Workflow)
```bash
python main.py
```
Or directly via the virtual environment binary:
```bash
/home/razwanul/Documents/codingWorld/python/pricewatch/.venv/bin/python main.py
```

### Inspect a Single Product URL (Phase 1 Workflow)
```bash
python main.py --url "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
```
Or interactively:
```bash
python main.py --interactive
```

---

## Automated Scheduling with Linux Cron

PriceWatch is designed for dependable unattended background execution via `cron`.

### 1. Open your crontab editor:
```bash
crontab -e
```

### 2. Add a scheduled entry:

Always use **absolute paths** and ensure the command navigates (`cd`) to the project directory so relative files (`products.json`, `.env`, `data/`, `logs/`) resolve properly.

#### Run every hour (at minute 0):
```cron
0 * * * * cd /home/razwanul/Documents/codingWorld/python/pricewatch && /home/razwanul/Documents/codingWorld/python/pricewatch/.venv/bin/python main.py >> /home/razwanul/Documents/codingWorld/python/pricewatch/logs/cron.log 2>&1
```

#### Run every 15 minutes:
```cron
*/15 * * * * cd /home/razwanul/Documents/codingWorld/python/pricewatch && /home/razwanul/Documents/codingWorld/python/pricewatch/.venv/bin/python main.py >> /home/razwanul/Documents/codingWorld/python/pricewatch/logs/cron.log 2>&1
```

#### Run twice daily (e.g., 9:00 AM and 6:00 PM):
```cron
0 9,18 * * * cd /home/razwanul/Documents/codingWorld/python/pricewatch && /home/razwanul/Documents/codingWorld/python/pricewatch/.venv/bin/python main.py >> /home/razwanul/Documents/codingWorld/python/pricewatch/logs/cron.log 2>&1
```

### 3. Verify Active Cron Jobs:
```bash
crontab -l
```

### 4. How to Stop or Remove Scheduled Automation:
- **To temporarily pause or disable**: Run `crontab -e` and comment out the PriceWatch line with a `#` at the start.
- **To delete the job**: Run `crontab -e` and remove the line, or run `crontab -r` to remove all crontab entries.

---

## Process Locking & Overlap Prevention

When running via `cron`, long-running or delayed monitoring runs could theoretically overlap with the next scheduled tick. PriceWatch prevents this using `SingleInstanceLock`:

- Before starting, the process writes its PID and timestamp to `data/pricewatch.lock`.
- If a secondary execution starts while the primary is still running, it detects the active PID, logs:
  `Another PriceWatch instance is currently running. Skipping execution to avoid overlap.`
  and exits cleanly with status `SKIPPED`.
- **Stale Lock Recovery**: If a prior process terminated unexpectedly or crashed, PriceWatch inspects the locked PID via `os.kill(pid, 0)`. If the process is no longer alive or the lock has exceeded 30 minutes, it automatically reclaims the lock and continues without requiring manual intervention.

---

## GitHub Actions Cloud Scheduling

PriceWatch can run automatically on GitHub-hosted runners every hour, even when your laptop is off.

### How it works

The workflow (`.github/workflows/pricewatch.yml`):

1. Checks out your public code repository (`Razwanul30/pricewatch`).
2. Checks out your private state repository (`Razwanul30/pricewatch-state`) which holds `products.json` and `data/price_history.csv`.
3. Copies those files into the expected paths before running the monitor.
4. Runs `python main.py` with Telegram credentials injected from GitHub Actions Secrets.
5. On success, commits any changes to `price_history.csv` back to the private state repository.

### Required GitHub Actions Secrets

Configure these in **Settings → Secrets and variables → Actions** of your public repository (`Razwanul30/pricewatch`):

| Secret | Description |
|--------|-------------|
| `STATE_REPO_TOKEN` | Fine-grained PAT with **Contents: Read and Write** permission on `Razwanul30/pricewatch-state` |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot API token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |

> **Note**: No `.env` file is needed in the GitHub Actions environment. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are injected directly as environment variables from secrets.

### Private State Repository Structure

`Razwanul30/pricewatch-state` must contain:

```text
products.json         # Your tracked products list
data/
  price_history.csv   # Created automatically on first successful run
```

To add or remove tracked products, edit `products.json` directly in `Razwanul30/pricewatch-state`.

### Trigger Schedule

The workflow runs automatically at **minute 25 of every hour** (`25 * * * *`). This avoids the top-of-hour congestion on GitHub's shared infrastructure.

### Manual Trigger

Go to **Actions → PriceWatch Cloud Monitor → Run workflow** in your repository to trigger a run on demand at any time.

### Concurrency Protection

GitHub Actions prevents overlapping runs using a concurrency group (`pricewatch-monitoring`). If a run is already in progress when the next hourly schedule fires, the new run waits — it is not cancelled.

### Limitations

- **Scheduled runs may be delayed** by up to 10–15 minutes (or longer) during peak hours on GitHub's shared infrastructure. This is a GitHub platform limitation.
- The workflow does not commit `products.json` back to the state repo — edit it directly in `Razwanul30/pricewatch-state` to add or remove tracked products.
- Run logs are visible in the GitHub Actions console and are retained for 90 days (GitHub's default) before expiring.

---

## Logging & Troubleshooting

### Log Location:
Application logs are stored at:
```text
logs/pricewatch.log
```
Cron execution stderr/stdout output (if configured in crontab) is stored at:
```text
logs/cron.log
```

### Log Features:
- Standardized formatting: `YYYY-MM-DD HH:MM:SS [LEVEL] message`.
- Automatic log rotation: Each log file is capped at 1 MB, keeping up to 3 backups (`pricewatch.log.1`, `pricewatch.log.2`, etc.).
- Never logs Telegram bot tokens or credentials.

### Viewing Recent Logs:
```bash
tail -n 50 -f /home/razwanul/Documents/codingWorld/python/pricewatch/logs/pricewatch.log
```

---

## How CSV Price History Works

Every monitoring run appends results to `data/price_history.csv`:

| Column | Description |
|---|---|
| `checked_at` | Timestamp in UTC format (`YYYY-MM-DD HH:MM:SS UTC`) |
| `product_name` | Name of the configured product |
| `url` | Product URL |
| `current_price` | Extracted numeric price |
| `currency` | Extracted currency symbol/ISO code |
| `previous_price` | Last recorded successful price |
| `price_change` | `INITIAL`, `DECREASE`, `INCREASE`, `UNCHANGED`, or `CURRENCY_MISMATCH` |
| `current_stock` | Current stock availability description |
| `previous_stock` | Previous stock availability description |
| `stock_changed` | `True` if stock status changed, else `False` |
| `target_price` | Configured target price (if set) |
| `target_reached` | `True`, `False`, or `N/A` |
| `status` | `SUCCESS` or `FAILED` |
| `error` | Failure message if scraping failed |

---

## Running Tests

Run the complete test suite:

```bash
cd /home/razwanul/Documents/codingWorld/python/pricewatch
.venv/bin/python -m pytest -v tests/
```

The test suite covers:
- Safe Decimal conversion.
- First observation & target attainment.
- Price increase, decrease, and unchanged status.
- Currency mismatch safety.
- Stock status change detection.
- Target price notification deduplication.
- Configuration validation and invalid entry skipping.
- CSV history persistence and error isolation.
- Telegram notification formatting and API mocking.
- Transient error retries and recovery.
- Retry exhaustion on prolonged failures.
- Permanent error avoidance (404/403).
- Process lock acquisition, release, and concurrency blocking.
- Stale lock detection and recovery.
- Structured rotating file logging.

---

## Known Limitations

1. **Client-Side Rendering (SPA / JavaScript-only apps)**: Sites that render prices strictly via client-side JavaScript execution (without server-side rendering or static JSON-LD/OpenGraph metadata) require a headless browser (e.g. Playwright), which is outside the scope of this lightweight solution.
2. **Aggressive Bot Protection**: Advanced bot mitigation platforms (e.g., Cloudflare Turnstile, Akamai) may block automated HTTP requests.
3. **Multi-Variant / Dynamic Options**: Products requiring active variant selection (e.g. selecting a specific shoe size or color before a price is rendered) will capture the base/default price present in static HTML or metadata.
