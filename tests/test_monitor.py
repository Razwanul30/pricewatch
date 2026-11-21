from decimal import Decimal
import json
import os
from unittest.mock import patch, MagicMock
import pytest

from monitor import (
    load_products_config,
    load_history,
    append_history_record,
    evaluate_product,
    format_notification,
    send_telegram_notification,
    get_telegram_config,
    safe_decimal,
    run_monitoring,
    load_env_file,
)
from scraper import ScrapingError


# 1. Decimal Parsing Tests
def test_safe_decimal():
    assert safe_decimal("51.77") == Decimal("51.77")
    assert safe_decimal("1,250.50") == Decimal("1250.50")
    assert safe_decimal(42) == Decimal("42")
    assert safe_decimal("Unknown") is None
    assert safe_decimal("") is None
    assert safe_decimal(None) is None
    assert safe_decimal("invalid") is None


# 2. First Observation Tests
def test_first_observation_no_target():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": None}
    scrape_data = {"price": "50.00", "currency": "USD ($)", "stock_status": "In Stock"}
    prev_obs = None

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "INITIAL"
    assert result["current_price"] == "50.00"
    assert result["previous_price"] == ""
    assert result["stock_changed"] is False
    assert result["target_reached"] == "N/A"
    assert len(result["reasons"]) == 0


def test_first_observation_target_reached():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": "55.00"}
    scrape_data = {"price": "50.00", "currency": "USD ($)", "stock_status": "In Stock"}
    prev_obs = None

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "INITIAL"
    assert result["target_reached"] is True
    assert any("Target price reached" in r for r in result["reasons"])


# 3. Price Increase Tests
def test_price_increase():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": None}
    scrape_data = {"price": "60.00", "currency": "USD ($)", "stock_status": "In Stock"}
    prev_obs = {
        "price": Decimal("50.00"),
        "price_str": "50.00",
        "currency": "USD ($)",
        "stock": "In Stock",
        "target_reached": False,
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "INCREASE"
    assert result["price_diff"] == "+10.00"
    assert any("Price increased" in r for r in result["reasons"])


# 4. Price Decrease Tests
def test_price_decrease():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": None}
    scrape_data = {"price": "40.00", "currency": "USD ($)", "stock_status": "In Stock"}
    prev_obs = {
        "price": Decimal("50.00"),
        "price_str": "50.00",
        "currency": "USD ($)",
        "stock": "In Stock",
        "target_reached": False,
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "DECREASE"
    assert result["price_diff"] == "-10.00"
    assert any("Price decreased" in r for r in result["reasons"])


# 5. Unchanged Price Tests
def test_unchanged_price():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": "30.00"}
    scrape_data = {"price": "50.00", "currency": "USD ($)", "stock_status": "In Stock"}
    prev_obs = {
        "price": Decimal("50.00"),
        "price_str": "50.00",
        "currency": "USD ($)",
        "stock": "In Stock",
        "target_reached": False,
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "UNCHANGED"
    assert result["stock_changed"] is False
    assert result["target_reached"] is False
    assert len(result["reasons"]) == 0


# 6. Stock Status Change Tests
def test_stock_status_change():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": None}
    scrape_data = {"price": "50.00", "currency": "USD ($)", "stock_status": "Out of Stock"}
    prev_obs = {
        "price": Decimal("50.00"),
        "price_str": "50.00",
        "currency": "USD ($)",
        "stock": "In Stock (5 available)",
        "target_reached": False,
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["stock_changed"] is True
    assert any("Stock status changed" in r for r in result["reasons"])


# 7. Target Price Reached Tests
def test_target_price_reached_on_drop():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": "45.00"}
    scrape_data = {"price": "42.00", "currency": "GBP (£)", "stock_status": "In Stock"}
    prev_obs = {
        "price": Decimal("50.00"),
        "price_str": "50.00",
        "currency": "GBP (£)",
        "stock": "In Stock",
        "target_reached": False,
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "DECREASE"
    assert result["target_reached"] is True
    # Should report both price decrease and target reached
    assert any("Price decreased" in r for r in result["reasons"])
    assert any("Target price reached" in r for r in result["reasons"])


def test_target_price_not_repeated_when_unchanged():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": "45.00"}
    scrape_data = {"price": "42.00", "currency": "GBP (£)", "stock_status": "In Stock"}
    prev_obs = {
        "price": Decimal("42.00"),
        "price_str": "42.00",
        "currency": "GBP (£)",
        "stock": "In Stock",
        "target_reached": True,  # Already reached in previous run
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "UNCHANGED"
    assert result["target_reached"] is True
    # Target was already reached, so no duplicate alert should trigger
    assert len(result["reasons"]) == 0


# 8. Currency Mismatch Safety
def test_currency_mismatch_prevents_comparison():
    prod_config = {"name": "Test Item", "url": "https://example.com/1", "target_price": None}
    scrape_data = {"price": "50.00", "currency": "EUR (€)", "stock_status": "In Stock"}
    prev_obs = {
        "price": Decimal("50.00"),
        "price_str": "50.00",
        "currency": "USD ($)",
        "stock": "In Stock",
        "target_reached": False,
    }

    result = evaluate_product(prod_config, scrape_data, prev_obs)
    assert result["price_change"] == "CURRENCY_MISMATCH"
    assert len(result["reasons"]) == 0


# 9. Configuration Validation Tests
def test_load_products_config_valid(tmp_path):
    cfg_file = tmp_path / "products.json"
    cfg_data = {
        "products": [
            {"name": "Item 1", "url": "https://example.com/1", "target_price": "19.99"},
            {"name": "Item 2", "url": "https://example.com/2"},
            {"name": "Invalid item", "url": ""},  # Should be skipped
            {"name": "", "url": "https://example.com/3"},  # Should be skipped
            "not a dict",  # Should be skipped
        ]
    }
    cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

    prods = load_products_config(str(cfg_file))
    assert len(prods) == 2
    assert prods[0]["name"] == "Item 1"
    assert prods[0]["target_price"] == "19.99"
    assert prods[1]["name"] == "Item 2"
    assert prods[1]["target_price"] is None


# 10. CSV History Persistence & Never Overwriting on Failure
def test_csv_history_persistence(tmp_path):
    csv_file = tmp_path / "price_history.csv"

    # 1. Record a successful observation
    rec1 = {
        "checked_at": "2026-11-21 12:00:00 UTC",
        "product_name": "Book A",
        "url": "https://books.toscrape.com/a",
        "current_price": "51.77",
        "currency": "GBP (£)",
        "previous_price": "",
        "price_change": "INITIAL",
        "current_stock": "In stock",
        "previous_stock": "",
        "stock_changed": "False",
        "target_price": "50.00",
        "target_reached": "False",
        "status": "SUCCESS",
        "error": "",
    }
    append_history_record(rec1, str(csv_file))

    # 2. Record a subsequent failed observation
    rec2 = {
        "checked_at": "2026-11-21 13:00:00 UTC",
        "product_name": "Book A",
        "url": "https://books.toscrape.com/a",
        "current_price": "",
        "currency": "",
        "previous_price": "51.77",
        "price_change": "",
        "current_stock": "",
        "previous_stock": "In stock",
        "stock_changed": "False",
        "target_price": "50.00",
        "target_reached": "N/A",
        "status": "FAILED",
        "error": "HTTP 404",
    }
    append_history_record(rec2, str(csv_file))

    # 3. Load history: should return Book A with 51.77 from rec1, NOT overwritten by rec2
    history = load_history(str(csv_file))
    assert "https://books.toscrape.com/a" in history
    assert history["https://books.toscrape.com/a"]["price"] == Decimal("51.77")
    assert history["https://books.toscrape.com/a"]["stock"] == "In stock"


# 11. Telegram Notification Formatting & Behavior Tests
def test_telegram_format_notification():
    msg = format_notification(
        product_name="Sample Book",
        url="https://example.com/book",
        current_price="45.00",
        currency="GBP (£)",
        previous_price="50.00",
        current_stock="In stock (10 available)",
        previous_stock="In stock (15 available)",
        reasons=["📉 Price decreased from 50.00 to 45.00", "🎯 Target price reached!"],
        target_price="48.00",
    )

    assert "Sample Book" in msg
    assert "45.00 (GBP (£))" in msg
    assert "50.00" in msg
    assert "48.00" in msg
    assert "https://example.com/book" in msg
    assert "Price decreased" in msg
    assert "Target price reached" in msg


@patch("requests.post")
def test_telegram_send_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    success = send_telegram_notification("fake_token", "fake_chat", "Test message")
    assert success is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert "https://api.telegram.org/botfake_token/sendMessage" in args[0]
    assert kwargs["json"]["chat_id"] == "fake_chat"
    assert kwargs["json"]["text"] == "Test message"


@patch("requests.post")
def test_telegram_send_failure(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": False, "description": "Bad Request"}
    mock_resp.status_code = 400
    mock_post.return_value = mock_resp

    success = send_telegram_notification("fake_token", "fake_chat", "Test message")
    assert success is False


def test_telegram_disabled_when_missing_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    token, chat_id = get_telegram_config()
    assert token is None
    assert chat_id is None


# 12. Monitoring Run End-to-End Logic with Mocks
@patch("monitor.scrape_product")
def test_run_monitoring_flow(mock_scrape, tmp_path):
    cfg_file = tmp_path / "products.json"
    csv_file = tmp_path / "price_history.csv"

    cfg_file.write_text(json.dumps({
        "products": [
            {"name": "Product 1", "url": "https://example.com/p1", "target_price": "20.00"},
            {"name": "Product 2", "url": "https://example.com/p2"},
        ]
    }), encoding="utf-8")

    # Scraper returns success for p1, failure for p2
    def side_effect(url):
        if url == "https://example.com/p1":
            return {"name": "Product 1", "price": "18.50", "currency": "USD ($)", "stock_status": "In Stock", "url": url}
        raise ScrapingError("Product 404")

    mock_scrape.side_effect = side_effect

    log_file = tmp_path / "test.log"
    lock_file = tmp_path / "test.lock"

    results = run_monitoring(
        config_path=str(cfg_file),
        csv_path=str(csv_file),
        log_path=str(log_file),
        lock_path=str(lock_file),
        retry_delay=0.01,
    )

    assert len(results) == 2
    assert results[0]["status"] == "SUCCESS"
    assert results[0]["details"]["target_reached"] is True
    assert results[1]["status"] == "FAILED"

    # Verify CSV has both records
    history = load_history(str(csv_file))
    assert "https://example.com/p1" in history
    assert history["https://example.com/p1"]["price"] == Decimal("18.50")
    assert "https://example.com/p2" not in history  # Failed product not in valid history


# =====================================================================
# Phase 3 Tests: Retries, Locks, Stale Recovery, Logging
# =====================================================================

from monitor import (
    scrape_with_retry,
    SingleInstanceLock,
    setup_logging,
    is_pid_running,
)


@patch("monitor.scrape_product")
def test_retry_temporary_error_recovers(mock_scrape):
    """Temporary errors should retry and return result if subsequent attempt succeeds."""
    mock_scrape.side_effect = [
        ScrapingError("Temporary Timeout", retryable=True),
        ScrapingError("Connection Reset", retryable=True),
        {"name": "Widget", "price": "25.00", "currency": "USD ($)", "stock_status": "In Stock", "url": "https://example.com/item"},
    ]

    result = scrape_with_retry("https://example.com/item", max_retries=3, retry_delay=0.01)
    assert result["price"] == "25.00"
    assert mock_scrape.call_count == 3


@patch("monitor.scrape_product")
def test_retry_exhaustion(mock_scrape):
    """Temporary errors that fail all retry attempts should exhaust and raise ScrapingError."""
    mock_scrape.side_effect = ScrapingError("Server Down 500", retryable=True)

    with pytest.raises(ScrapingError) as exc_info:
        scrape_with_retry("https://example.com/item", max_retries=3, retry_delay=0.01)

    assert "Server Down 500" in str(exc_info.value)
    assert mock_scrape.call_count == 3


@patch("monitor.scrape_product")
def test_permanent_error_no_retry(mock_scrape):
    """Permanent errors (e.g. 404, 403, bad URL) must not be retried."""
    mock_scrape.side_effect = ScrapingError("Product page not found (HTTP 404).", retryable=False)

    with pytest.raises(ScrapingError) as exc_info:
        scrape_with_retry("https://example.com/item", max_retries=3, retry_delay=0.01)

    assert "HTTP 404" in str(exc_info.value)
    assert mock_scrape.call_count == 1  # Exactly 1 call, zero retries


def test_lock_acquisition_and_release(tmp_path):
    """SingleInstanceLock should create lock file on acquire and remove it on release."""
    lock_file = tmp_path / "test.lock"
    lock = SingleInstanceLock(lock_path=str(lock_file))

    assert lock.acquire() is True
    assert lock_file.exists()

    with open(lock_file, "r") as f:
        data = json.load(f)
    assert data["pid"] == os.getpid()

    lock.release()
    assert not lock_file.exists()


def test_concurrent_run_prevention(tmp_path):
    """An active lock should prevent a concurrent execution from running."""
    lock_file = tmp_path / "test.lock"
    lock1 = SingleInstanceLock(lock_path=str(lock_file))
    lock2 = SingleInstanceLock(lock_path=str(lock_file))

    assert lock1.acquire() is True

    # lock2 attempts to acquire while lock1 is held by current active process
    assert lock2.acquire() is False

    lock1.release()
    # After release, lock2 should succeed
    assert lock2.acquire() is True
    lock2.release()


def test_stale_lock_recovery(tmp_path):
    """A stale lock from a dead PID should be reclaimed safely."""
    lock_file = tmp_path / "test.lock"

    # Write a stale lock referencing a PID that does not exist
    dead_pid = 99999999
    while is_pid_running(dead_pid):
        dead_pid -= 1

    lock_file.write_text(json.dumps({
        "pid": dead_pid,
        "started_at": "2026-01-01T00:00:00+00:00",
    }), encoding="utf-8")

    lock = SingleInstanceLock(lock_path=str(lock_file))
    # Should detect stale lock and acquire successfully
    assert lock.acquire() is True

    with open(lock_file, "r") as f:
        data = json.load(f)
    assert data["pid"] == os.getpid()
    lock.release()


def test_logging_behavior(tmp_path):
    """Logging should write timestamped log messages with rotation support."""
    log_file = tmp_path / "test.log"
    logger = setup_logging(log_path=str(log_file))

    logger.info("Test info message for PriceWatch")
    logger.error("Test error message for PriceWatch")
    for h in logger.handlers:
        h.flush()

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "Test info message for PriceWatch" in content
    assert "Test error message for PriceWatch" in content
    assert "[INFO]" in content
    assert "[ERROR]" in content


# =====================================================================
# Telegram Delivery Failure Handling & Deduplication Tests
# =====================================================================

@patch("requests.post")
def test_telegram_error_logging_safe_and_detailed(mock_post, tmp_path):
    """Telegram errors should safely log status code and description without revealing bot token."""
    secret_token = "SECRET_TOKEN_998877"
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    mock_resp.reason = "Bad Request"
    mock_post.return_value = mock_resp

    log_file = tmp_path / "telegram_err.log"
    logger = setup_logging(log_path=str(log_file))

    success = send_telegram_notification(secret_token, "invalid_chat", "Test alert", logger=logger)
    assert success is False
    assert send_telegram_notification.last_error == "HTTP 400 - Bad Request: chat not found"

    for h in logger.handlers:
        h.flush()

    log_content = log_file.read_text(encoding="utf-8")
    assert "HTTP 400 - Bad Request: chat not found" in log_content
    # Bot token must NEVER be exposed
    assert secret_token not in log_content


@patch("requests.post")
def test_telegram_error_logging_exception_sanitization(mock_post, tmp_path):
    """Network exception containing token URL must be redacted in logs."""
    secret_token = "SECRET_TOKEN_112233"
    mock_post.side_effect = Exception(
        f"HTTP error connecting to https://api.telegram.org/bot{secret_token}/sendMessage"
    )

    log_file = tmp_path / "telegram_sanitize.log"
    logger = setup_logging(log_path=str(log_file))

    success = send_telegram_notification(secret_token, "chat_1", "Test", logger=logger)
    assert success is False

    for h in logger.handlers:
        h.flush()

    log_content = log_file.read_text(encoding="utf-8")
    assert secret_token not in log_content
    assert "[REDACTED]" in log_content


@patch("monitor.scrape_product")
@patch("requests.post")
def test_failed_delivery_retried_then_deduplicated(mock_tg_post, mock_scrape, tmp_path, monkeypatch):
    """
    Test the full lifecycle:
    1. Event detected, Telegram delivery fails (HTTP 400).
    2. Next run: product is UNCHANGED, but notification is RETRIED.
    3. Telegram delivery succeeds.
    4. Subsequent run: product is UNCHANGED, notification is DEDUPLICATED (not sent).
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "mock_chat")

    cfg_file = tmp_path / "products.json"
    csv_file = tmp_path / "price_history.csv"
    log_file = tmp_path / "monitor.log"
    lock_file = tmp_path / "monitor.lock"

    cfg_file.write_text(json.dumps({
        "products": [
            {"name": "Tracked Book", "url": "https://example.com/book", "target_price": "50.00"}
        ]
    }), encoding="utf-8")

    # Scraper returns price 45.00 (below target 50.00)
    mock_scrape.return_value = {
        "name": "Tracked Book",
        "price": "45.00",
        "currency": "GBP (£)",
        "stock_status": "In stock",
        "url": "https://example.com/book",
    }

    # --- RUN 1: Target reached, Telegram delivery fails (HTTP 400) ---
    fail_resp = MagicMock()
    fail_resp.status_code = 400
    fail_resp.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    fail_resp.reason = "Bad Request"
    mock_tg_post.return_value = fail_resp

    results_run1 = run_monitoring(
        config_path=str(cfg_file),
        csv_path=str(csv_file),
        log_path=str(log_file),
        lock_path=str(lock_file),
    )
    assert len(results_run1) == 1
    assert results_run1[0]["status"] == "SUCCESS"
    assert results_run1[0]["details"]["target_reached"] is True
    # Telegram was called once and failed
    assert mock_tg_post.call_count == 1

    # Check history on disk: target was reached, but error records telegram delivery failure
    history_after_run1 = load_history(str(csv_file))
    assert history_after_run1["https://example.com/book"]["target_reached"] is True
    assert history_after_run1["https://example.com/book"]["target_notified"] is False

    # --- RUN 2: Product price is UNCHANGED, Telegram delivery succeeds (HTTP 200) ---
    mock_tg_post.reset_mock()
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {"ok": True}
    mock_tg_post.return_value = success_resp

    results_run2 = run_monitoring(
        config_path=str(cfg_file),
        csv_path=str(csv_file),
        log_path=str(log_file),
        lock_path=str(lock_file),
    )
    assert len(results_run2) == 1
    assert results_run2[0]["status"] == "SUCCESS"
    assert results_run2[0]["details"]["price_change"] == "UNCHANGED"
    # CRUCIAL: Telegram delivery must be RETRIED because Run 1 failed!
    assert mock_tg_post.call_count == 1
    assert any("Target price reached" in r for r in results_run2[0]["details"]["reasons"])

    # Check history on disk: now target_notified is True
    history_after_run2 = load_history(str(csv_file))
    assert history_after_run2["https://example.com/book"]["target_reached"] is True
    assert history_after_run2["https://example.com/book"]["target_notified"] is True

    # --- RUN 3: Product price is still UNCHANGED, notification must be DEDUPLICATED ---
    mock_tg_post.reset_mock()

    results_run3 = run_monitoring(
        config_path=str(cfg_file),
        csv_path=str(csv_file),
        log_path=str(log_file),
        lock_path=str(lock_file),
    )
    assert len(results_run3) == 1
    assert results_run3[0]["status"] == "SUCCESS"
    assert results_run3[0]["details"]["price_change"] == "UNCHANGED"
    # CRUCIAL: Telegram must NOT be called again (deduplication)!
    assert mock_tg_post.call_count == 0
    assert len(results_run3[0]["details"]["reasons"]) == 0


