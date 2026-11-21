import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urlparse
import requests

from scraper import scrape_product, ScrapingError


CSV_FIELDNAMES = [
    "checked_at",
    "product_name",
    "url",
    "current_price",
    "currency",
    "previous_price",
    "price_change",
    "current_stock",
    "previous_stock",
    "stock_changed",
    "target_price",
    "target_reached",
    "status",
    "error",
]


def setup_logging(
    log_path: str = "logs/pricewatch.log",
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Configure structured logging with file rotation and console output.
    Ensures secrets are never exposed in log output.
    """
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    logger = logging.getLogger("pricewatch")
    logger.setLevel(level)

    has_file_handler = False
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            if os.path.abspath(handler.baseFilename) == os.path.abspath(log_path):
                has_file_handler = True
            else:
                handler.close()
                logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not has_file_handler:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in logger.handlers
    )
    if not has_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    return logger


def is_pid_running(pid: int) -> bool:
    """Check whether a process with the given PID is currently active."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as err:
        import errno
        return err.errno == errno.EPERM


class SingleInstanceLock:
    """
    File-based lock mechanism to prevent overlapping scheduled executions.
    Detects and safely recovers from stale locks if a previous process died.
    """
    def __init__(self, lock_path: str = "data/pricewatch.lock", stale_timeout: int = 1800):
        self.lock_path = lock_path
        self.stale_timeout = stale_timeout
        self.acquired = False

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        if os.path.isfile(self.lock_path):
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    lock_info = json.load(f)
                locked_pid = lock_info.get("pid")
                started_at_str = lock_info.get("started_at")

                is_active = is_pid_running(locked_pid) if locked_pid else False

                if is_active:
                    # Active process check with timeout fallback
                    if started_at_str:
                        started_dt = datetime.fromisoformat(started_at_str)
                        age = (datetime.now(timezone.utc) - started_dt).total_seconds()
                        if age <= self.stale_timeout:
                            return False
                    else:
                        return False
            except Exception:
                # Corrupted lock file -> recover and overwrite
                pass

        try:
            lock_payload = {
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.lock_path, "w", encoding="utf-8") as f:
                json.dump(lock_payload, f)
            self.acquired = True
            return True
        except Exception:
            return False

    def release(self) -> None:
        if self.acquired and os.path.isfile(self.lock_path):
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    lock_info = json.load(f)
                if lock_info.get("pid") == os.getpid():
                    os.remove(self.lock_path)
            except Exception:
                pass
            self.acquired = False

    def __enter__(self):
        if not self.acquire():
            return None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def load_env_file(path: str = ".env") -> None:
    """Load key-value pairs from a .env file into os.environ if not already set."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, val = stripped.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        print(f"[Warning] Could not load .env file: {e}")


def get_telegram_config() -> tuple[Optional[str], Optional[str]]:
    """Retrieve Telegram credentials from environment variables."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None, None
    return token, chat_id


def safe_decimal(value: any) -> Optional[Decimal]:
    """Convert an arbitrary value to Decimal or return None."""
    if value is None:
        return None
    val_str = str(value).strip().replace(",", "")
    if not val_str or val_str.lower() in ("unknown", "none", "n/a", ""):
        return None
    try:
        dec = Decimal(val_str)
        return dec
    except (InvalidOperation, ValueError, TypeError):
        return None


def format_notification(
    product_name: str,
    url: str,
    current_price: str,
    currency: str,
    previous_price: Optional[str],
    current_stock: str,
    previous_stock: Optional[str],
    reasons: list[str],
    target_price: Optional[str] = None,
) -> str:
    """Format an informative Telegram notification message."""
    reason_lines = "\n".join(f"• {r}" for r in reasons)
    lines = [
        f"🔔 PriceWatch Alert: {product_name}",
        "",
        "Detected Changes:",
        reason_lines,
        "",
        f"💰 Current Price: {current_price} ({currency})" if currency != "Unknown" else f"💰 Current Price: {current_price}",
    ]
    if previous_price:
        lines.append(f"⏱️ Previous Price: {previous_price}")
    if target_price:
        lines.append(f"🎯 Target Price: {target_price}")
    lines.append(f"📦 Stock Status: {current_stock}")
    lines.append(f"🔗 URL: {url}")

    return "\n".join(lines)


def send_telegram_notification(
    token: str,
    chat_id: str,
    message: str,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """
    Send a message via the Telegram Bot API.
    Safely captures and logs Telegram's response description/status without exposing secrets.
    """
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": False,
    }
    log_err = logger.error if logger else print

    try:
        response = requests.post(api_url, json=payload, timeout=10)
        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code == 200 and data.get("ok"):
            send_telegram_notification.last_error = ""
            return True

        status_code = response.status_code
        description = data.get("description") if isinstance(data, dict) else None
        if description:
            error_detail = f"HTTP {status_code} - {description}"
        elif response.reason:
            error_detail = f"HTTP {status_code} - {response.reason}"
        else:
            error_detail = f"HTTP {status_code}"

        log_err(f"[Telegram] Delivery failed ({error_detail})")
        send_telegram_notification.last_error = error_detail
        return False

    except requests.exceptions.Timeout:
        error_detail = "Request timed out"
        log_err(f"[Telegram] Delivery failed ({error_detail})")
        send_telegram_notification.last_error = error_detail
        return False
    except requests.exceptions.ConnectionError:
        error_detail = "Connection error connecting to Telegram API"
        log_err(f"[Telegram] Delivery failed ({error_detail})")
        send_telegram_notification.last_error = error_detail
        return False
    except Exception as e:
        # Sanitize exception message so token and token-bearing URL are NEVER exposed
        clean_msg = str(e)
        if token:
            clean_msg = clean_msg.replace(token, "[REDACTED]")
        clean_msg = re.sub(r"/bot[^/\s]+", "/bot[REDACTED]", clean_msg)
        error_detail = clean_msg
        log_err(f"[Telegram] Delivery failed ({error_detail})")
        send_telegram_notification.last_error = error_detail
        return False


def load_products_config(config_path: str = "products.json") -> list[dict]:
    """
    Load and validate products configuration from a JSON file.
    Reports invalid entries without crashing.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "products" not in data:
        raise ValueError("Invalid configuration: root must be an object with a 'products' list.")

    raw_products = data.get("products")
    if not isinstance(raw_products, list):
        raise ValueError("Invalid configuration: 'products' must be a list.")

    valid_products = []
    for idx, item in enumerate(raw_products, start=1):
        if not isinstance(item, dict):
            print(f"[Warning] Product #{idx} is not an object. Skipping.")
            continue

        name = item.get("name")
        url = item.get("url")

        if not name or not isinstance(name, str) or not name.strip():
            print(f"[Warning] Product #{idx} missing valid 'name'. Skipping.")
            continue

        if not url or not isinstance(url, str) or not url.strip():
            print(f"[Warning] Product '{name}' missing valid 'url'. Skipping.")
            continue

        target_price_raw = item.get("target_price")
        target_price_str = None
        if target_price_raw is not None and str(target_price_raw).strip():
            target_dec = safe_decimal(target_price_raw)
            if target_dec is None or target_dec < Decimal(0):
                print(f"[Warning] Product '{name}' has invalid target_price '{target_price_raw}'. Ignoring target price.")
            else:
                target_price_str = f"{target_dec:.2f}"

        valid_products.append({
            "name": name.strip(),
            "url": url.strip(),
            "target_price": target_price_str,
        })

    return valid_products


def load_history(csv_path: str = "data/price_history.csv") -> dict[str, dict]:
    """
    Load the latest valid observation for each product URL from price_history.csv.
    Ignores failed scrapes so they never overwrite last known valid values.
    Also tracks notification delivery state so failed alerts can be retried.
    """
    if not os.path.isfile(csv_path):
        return {}

    history: dict[str, dict] = {}
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") != "SUCCESS":
                    continue

                url = row.get("url", "").strip()
                if not url:
                    continue

                price_dec = safe_decimal(row.get("current_price"))
                target_reached_val = (row.get("target_reached", "").lower() == "true")
                error_val = (row.get("error") or "").strip().lower()

                # Check if Telegram alert delivery failed in this record
                tg_failed = "telegram delivery failed" in error_val or "telegram alert delivery failed" in error_val
                target_notified = False if tg_failed else target_reached_val

                history[url] = {
                    "price": price_dec,
                    "price_str": row.get("current_price"),
                    "currency": row.get("currency"),
                    "stock": row.get("current_stock"),
                    "target_reached": target_reached_val,
                    "target_notified": target_notified,
                    "checked_at": row.get("checked_at"),
                }
    except Exception as e:
        print(f"[Warning] Could not load price history from {csv_path}: {e}")

    return history


def append_history_record(record: dict, csv_path: str = "data/price_history.csv") -> None:
    """Append a single record to the price history CSV file."""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    file_exists = os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)


def scrape_with_retry(
    url: str,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    logger: Optional[logging.Logger] = None,
) -> dict:
    """
    Fetch a product URL with a configurable retry mechanism for temporary errors.
    Permanent errors (e.g. HTTP 404, HTTP 403, invalid URL) are not retried.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = scrape_product(url)
            if attempt > 1 and logger:
                logger.info(f"Successfully scraped '{url}' on attempt {attempt}/{max_retries}.")
            return result
        except ScrapingError as e:
            last_error = e
            is_retryable = getattr(e, "retryable", False)
            if is_retryable and attempt < max_retries:
                if logger:
                    logger.warning(
                        f"Temporary scraping failure for '{url}': {e}. "
                        f"Retrying in {retry_delay:.1f}s (attempt {attempt}/{max_retries})..."
                    )
                time.sleep(retry_delay)
            else:
                if is_retryable and logger:
                    logger.error(f"Retry limit ({max_retries}) exhausted for '{url}': {e}")
                elif not is_retryable and logger:
                    logger.warning(f"Permanent error scraping '{url}': {e}. Skipping retries.")
                raise e
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                if logger:
                    logger.warning(
                        f"Unexpected error scraping '{url}': {e}. "
                        f"Retrying in {retry_delay:.1f}s (attempt {attempt}/{max_retries})..."
                    )
                time.sleep(retry_delay)
            else:
                if logger:
                    logger.error(f"Retry limit ({max_retries}) exhausted for '{url}': {e}")
                raise e

    if last_error:
        raise last_error


def evaluate_product(
    product_config: dict,
    scrape_result: dict,
    prev_observation: Optional[dict],
) -> dict:
    """
    Compare current scraped data with previous observation and target price.
    Uses Decimal for safe price comparison and detects changes.
    """
    current_price_str = scrape_result.get("price", "Unknown")
    current_currency = scrape_result.get("currency", "Unknown")
    current_stock = scrape_result.get("stock_status", "Unknown")
    target_price_str = product_config.get("target_price")

    curr_price_dec = safe_decimal(current_price_str)
    target_price_dec = safe_decimal(target_price_str)

    prev_price_dec = prev_observation.get("price") if prev_observation else None
    prev_price_str = prev_observation.get("price_str") if prev_observation else None
    prev_currency = prev_observation.get("currency") if prev_observation else None
    prev_stock = prev_observation.get("stock") if prev_observation else None
    prev_target_reached = prev_observation.get("target_reached", False) if prev_observation else False
    prev_target_notified = prev_observation.get("target_notified") if prev_observation else None
    if prev_target_notified is None:
        prev_target_notified = prev_target_reached

    # 1. Price comparison
    price_change = "INITIAL"
    price_diff_str = ""
    reasons = []

    # Check for currency mismatch
    if prev_currency and current_currency and prev_currency != "Unknown" and current_currency != "Unknown":
        if prev_currency.lower() != current_currency.lower():
            price_change = "CURRENCY_MISMATCH"

    if price_change != "CURRENCY_MISMATCH":
        if prev_price_dec is not None and curr_price_dec is not None:
            if curr_price_dec < prev_price_dec:
                price_change = "DECREASE"
                diff = prev_price_dec - curr_price_dec
                price_diff_str = f"-{diff:.2f}"
                reasons.append(f"📉 Price decreased from {prev_price_dec} to {curr_price_dec} (saved {diff:.2f})")
            elif curr_price_dec > prev_price_dec:
                price_change = "INCREASE"
                diff = curr_price_dec - prev_price_dec
                price_diff_str = f"+{diff:.2f}"
                reasons.append(f"📈 Price increased from {prev_price_dec} to {curr_price_dec} (+{diff:.2f})")
            else:
                price_change = "UNCHANGED"
        elif prev_price_dec is None and curr_price_dec is not None:
            price_change = "INITIAL"
        else:
            price_change = "UNKNOWN"

    # 2. Stock status change
    stock_changed = False
    if prev_stock and current_stock and current_stock != "Unknown":
        if current_stock.strip().lower() != prev_stock.strip().lower():
            stock_changed = True
            reasons.append(f"📦 Stock status changed: '{prev_stock}' ➔ '{current_stock}'")

    # 3. Target price reached
    target_reached = False
    if target_price_dec is not None and curr_price_dec is not None:
        if curr_price_dec <= target_price_dec:
            target_reached = True
            if not prev_target_notified:
                reasons.append(f"🎯 Target price reached! ({curr_price_dec} <= {target_price_dec})")

    # 4. Carry over any previously undelivered alert reasons
    if prev_observation and prev_observation.get("undelivered_reasons"):
        for r in prev_observation["undelivered_reasons"]:
            if r not in reasons:
                reasons.append(r)

    return {
        "current_price": current_price_str,
        "currency": current_currency,
        "previous_price": prev_price_str or "",
        "price_change": price_change,
        "price_diff": price_diff_str,
        "current_stock": current_stock,
        "previous_stock": prev_stock or "",
        "stock_changed": stock_changed,
        "target_price": target_price_str or "",
        "target_reached": target_reached if target_price_str else "N/A",
        "reasons": reasons,
    }


def run_monitoring(
    config_path: str = "products.json",
    csv_path: str = "data/price_history.csv",
    log_path: str = "logs/pricewatch.log",
    lock_path: str = "data/pricewatch.lock",
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> list[dict]:
    """
    Execute robust PriceWatch monitoring run.
    Guards against concurrent runs, rotates application logs, retries transient failures,
    records results in CSV, and sends Telegram notifications.
    """
    logger = setup_logging(log_path)

    lock = SingleInstanceLock(lock_path=lock_path)
    if not lock.acquire():
        logger.warning("Another PriceWatch instance is currently running. Skipping execution to avoid overlap.")
        return [{"status": "SKIPPED", "reason": "LOCKED"}]

    try:
        load_env_file(".env")
        token, chat_id = get_telegram_config()

        logger.info("=================================================================")
        logger.info("                PRICEWATCH MONITORING RUN STARTED                ")
        logger.info("=================================================================")

        if token and chat_id:
            logger.info("Telegram notifications: ENABLED")
        else:
            logger.info("Telegram notifications: DISABLED (credentials not configured)")

        try:
            products = load_products_config(config_path)
        except Exception as e:
            logger.error(f"Failed to load product configuration from '{config_path}': {e}")
            return []

        if not products:
            logger.warning("No valid products configured to monitor.")
            return []

        logger.info(f"Loaded {len(products)} product(s) to monitor.")

        history = load_history(csv_path)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        results = []
        success_count = 0
        failure_count = 0

        for idx, prod in enumerate(products, start=1):
            name = prod["name"]
            url = prod["url"]
            target_price = prod.get("target_price")

            logger.info(f"[{idx}/{len(products)}] Checking: '{name}'")
            prev_obs = history.get(url)

            try:
                scrape_data = scrape_with_retry(
                    url=url,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                    logger=logger,
                )
                eval_result = evaluate_product(prod, scrape_data, prev_obs)

                reasons = eval_result["reasons"]
                delivery_error = ""
                alert_delivered = True
                if reasons:
                    logger.info(f"   ⚡ Events detected for '{name}':")
                    for r in reasons:
                        logger.info(f"      {r}")

                    if token and chat_id:
                        msg = format_notification(
                            product_name=name,
                            url=url,
                            current_price=eval_result["current_price"],
                            currency=eval_result["currency"],
                            previous_price=eval_result["previous_price"],
                            current_stock=eval_result["current_stock"],
                            previous_stock=eval_result["previous_stock"],
                            reasons=reasons,
                            target_price=target_price,
                        )
                        sent = send_telegram_notification(token, chat_id, msg, logger=logger)
                        if sent:
                            logger.info(f"   📨 Telegram alert sent successfully for '{name}'.")
                            alert_delivered = True
                        else:
                            err_detail = getattr(send_telegram_notification, "last_error", "Unknown error")
                            logger.error(f"   ⚠️ Telegram alert delivery failed for '{name}': {err_detail}")
                            delivery_error = f"Telegram delivery failed: {err_detail}"
                            alert_delivered = False
                    else:
                        alert_delivered = True
                else:
                    logger.info(
                        f"   ✓ Price: {eval_result['current_price']} {eval_result['currency']} | "
                        f"Stock: {eval_result['current_stock']} (Status: {eval_result['price_change']})"
                    )

                record = {
                    "checked_at": now_iso,
                    "product_name": name,
                    "url": url,
                    "current_price": eval_result["current_price"],
                    "currency": eval_result["currency"],
                    "previous_price": eval_result["previous_price"],
                    "price_change": eval_result["price_change"],
                    "current_stock": eval_result["current_stock"],
                    "previous_stock": eval_result["previous_stock"],
                    "stock_changed": str(eval_result["stock_changed"]),
                    "target_price": eval_result["target_price"],
                    "target_reached": str(eval_result["target_reached"]),
                    "status": "SUCCESS",
                    "error": delivery_error,
                }
                append_history_record(record, csv_path)

                if alert_delivered:
                    target_notified = (eval_result["target_reached"] is True)
                    undelivered_reasons = []
                else:
                    target_notified = False
                    undelivered_reasons = list(reasons)

                history[url] = {
                    "price": safe_decimal(eval_result["current_price"]),
                    "price_str": eval_result["current_price"],
                    "currency": eval_result["currency"],
                    "stock": eval_result["current_stock"],
                    "target_reached": (eval_result["target_reached"] is True),
                    "target_notified": target_notified,
                    "undelivered_reasons": undelivered_reasons,
                    "checked_at": now_iso,
                }

                results.append({"product": name, "status": "SUCCESS", "details": eval_result})
                success_count += 1

            except ScrapingError as e:
                logger.error(f"   ❌ Scrape failed for '{name}' ({url}): {e}")
                record = {
                    "checked_at": now_iso,
                    "product_name": name,
                    "url": url,
                    "current_price": "",
                    "currency": "",
                    "previous_price": prev_obs.get("price_str", "") if prev_obs else "",
                    "price_change": "",
                    "current_stock": "",
                    "previous_stock": prev_obs.get("stock", "") if prev_obs else "",
                    "stock_changed": "False",
                    "target_price": target_price or "",
                    "target_reached": "N/A",
                    "status": "FAILED",
                    "error": str(e),
                }
                append_history_record(record, csv_path)
                results.append({"product": name, "status": "FAILED", "error": str(e)})
                failure_count += 1

            except Exception as e:
                logger.error(f"   ❌ Unexpected failure for '{name}' ({url}): {e}")
                record = {
                    "checked_at": now_iso,
                    "product_name": name,
                    "url": url,
                    "current_price": "",
                    "currency": "",
                    "previous_price": prev_obs.get("price_str", "") if prev_obs else "",
                    "price_change": "",
                    "current_stock": "",
                    "previous_stock": prev_obs.get("stock", "") if prev_obs else "",
                    "stock_changed": "False",
                    "target_price": target_price or "",
                    "target_reached": "N/A",
                    "status": "FAILED",
                    "error": str(e),
                }
                append_history_record(record, csv_path)
                results.append({"product": name, "status": "FAILED", "error": str(e)})
                failure_count += 1

        logger.info("=================================================================")
        logger.info(
            f"MONITORING COMPLETE — Total: {len(products)}, "
            f"Succeeded: {success_count}, Failed: {failure_count}."
        )
        logger.info(f"History recorded in: {csv_path}")
        logger.info("=================================================================\n")

        return results

    finally:
        lock.release()
