import json
import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup


class ScrapingError(Exception):
    """Custom exception for expected scraping errors."""
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


CURRENCY_SYMBOLS = {
    "£": "GBP (£)",
    "GBP": "GBP (£)",
    "$": "USD ($)",
    "USD": "USD ($)",
    "€": "EUR (€)",
    "EUR": "EUR (€)",
    "¥": "JPY (¥)",
    "JPY": "JPY (¥)",
    "₹": "INR (₹)",
    "INR": "INR (₹)",
    "CAD": "CAD ($)",
    "AUD": "AUD ($)",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _validate_and_normalize_url(url: str) -> str:
    """Validate and normalize product URL."""
    cleaned_url = url.strip()
    if not cleaned_url:
        raise ScrapingError("URL cannot be empty.")

    if not cleaned_url.startswith(("http://", "https://")):
        cleaned_url = "https://" + cleaned_url

    parsed = urlparse(cleaned_url)
    if not parsed.netloc or "." not in parsed.netloc:
        raise ScrapingError(f"Invalid URL '{url}'. Please provide a valid web address.")

    return cleaned_url


def _extract_price_and_currency(raw_price: str, default_currency: str = None) -> tuple[str, str]:
    """Extract numeric price and currency string from raw text."""
    if not raw_price:
        currency = CURRENCY_SYMBOLS.get(default_currency, default_currency) if default_currency else "Unknown"
        return "Unknown", currency

    text = str(raw_price).strip()

    currency = default_currency
    for symbol, label in CURRENCY_SYMBOLS.items():
        if symbol in text:
            currency = label
            break

    if currency:
        currency = CURRENCY_SYMBOLS.get(currency, currency)
    else:
        currency = "Unknown"

    # Match numeric price (e.g. 51.77, 1,299.99, 10)
    match = re.search(r"(\d+(?:[.,]\d{1,2})?)", text.replace(",", ""))
    price = match.group(1) if match else "Unknown"

    return price, currency


def _clean_stock_status(raw_status: str) -> str:
    """Clean and normalize stock status string."""
    if not raw_status:
        return "Unknown"

    text = " ".join(raw_status.split())
    lowered = text.lower()

    if "instock" in lowered or "in stock" in lowered:
        return text if "(" in text else "In Stock"
    if "outofstock" in lowered or "out of stock" in lowered:
        return "Out of Stock"
    if "soldout" in lowered or "sold out" in lowered:
        return "Sold Out"
    if "preorder" in lowered or "pre-order" in lowered:
        return "Pre-order"
    if "discontinued" in lowered:
        return "Discontinued"
    if "backorder" in lowered or "back-order" in lowered:
        return "Backorder"

    return text if text else "Unknown"


def _extract_from_json_ld(soup: BeautifulSoup) -> dict:
    """Extract product metadata from JSON-LD scripts."""
    data = {
        "name": None,
        "price": None,
        "currency": None,
        "stock_status": None,
    }

    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            if "@graph" in payload and isinstance(payload["@graph"], list):
                items = payload["@graph"]
            else:
                items = [payload]

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                if not data["name"] and item.get("name"):
                    data["name"] = str(item.get("name")).strip()

                offers = item.get("offers")
                offer = None
                if isinstance(offers, dict):
                    offer = offers
                elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
                    offer = offers[0]

                if offer:
                    raw_price = offer.get("price") or offer.get("lowPrice")
                    raw_currency = offer.get("priceCurrency")
                    price, currency = _extract_price_and_currency(raw_price, default_currency=raw_currency)
                    if price != "Unknown" and not data["price"]:
                        data["price"] = price
                    if currency != "Unknown" and not data["currency"]:
                        data["currency"] = currency

                    availability = offer.get("availability")
                    if availability and not data["stock_status"]:
                        # Schema.org availability URLs (e.g. https://schema.org/InStock)
                        status_str = availability.rsplit("/", 1)[-1]
                        data["stock_status"] = _clean_stock_status(status_str)

    return data


def _extract_from_meta_tags(soup: BeautifulSoup) -> dict:
    """Extract product metadata from OpenGraph and standard meta tags."""
    data = {
        "name": None,
        "price": None,
        "currency": None,
        "stock_status": None,
    }

    # Product Name
    for selector in [
        {"property": "og:title"},
        {"name": "twitter:title"},
        {"property": "product:name"},
        {"itemprop": "name"},
    ]:
        tag = soup.find("meta", selector)
        if tag and tag.get("content"):
            data["name"] = tag["content"].strip()
            break

    # Price & Currency
    price_tag = (
        soup.find("meta", {"property": "product:price:amount"})
        or soup.find("meta", {"property": "og:price:amount"})
        or soup.find("meta", {"itemprop": "price"})
    )
    curr_tag = (
        soup.find("meta", {"property": "product:price:currency"})
        or soup.find("meta", {"property": "og:price:currency"})
        or soup.find("meta", {"itemprop": "priceCurrency"})
    )

    raw_price = price_tag["content"] if price_tag and price_tag.get("content") else None
    raw_curr = curr_tag["content"] if curr_tag and curr_tag.get("content") else None
    if raw_price:
        p, c = _extract_price_and_currency(raw_price, default_currency=raw_curr)
        if p != "Unknown":
            data["price"] = p
        if c != "Unknown":
            data["currency"] = c

    # Stock Status
    avail_tag = (
        soup.find("meta", {"property": "product:availability"})
        or soup.find("meta", {"itemprop": "availability"})
        or soup.find("link", {"itemprop": "availability"})
    )
    if avail_tag:
        content = avail_tag.get("content") or avail_tag.get("href")
        if content:
            status_str = content.rsplit("/", 1)[-1]
            data["stock_status"] = _clean_stock_status(status_str)

    return data


def _extract_site_specific(soup: BeautifulSoup, url: str) -> dict:
    """Website-specific parsing for recognized sites."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    data = {
        "name": None,
        "price": None,
        "currency": None,
        "stock_status": None,
    }

    # books.toscrape.com
    if "books.toscrape.com" in domain:
        main_div = soup.find("div", class_="product_main")
        if main_div:
            h1 = main_div.find("h1")
            if h1:
                data["name"] = h1.get_text(strip=True)

            price_tag = main_div.find("p", class_="price_color")
            if price_tag:
                raw_text = price_tag.get_text(strip=True)
                p, c = _extract_price_and_currency(raw_text)
                data["price"] = p
                data["currency"] = c

            stock_tag = main_div.find("p", class_="availability")
            if stock_tag:
                data["stock_status"] = _clean_stock_status(stock_tag.get_text(" ", strip=True))

    return data


def _extract_html_fallback(soup: BeautifulSoup) -> dict:
    """Generic HTML heuristic fallback for product pages."""
    data = {
        "name": None,
        "price": None,
        "currency": None,
        "stock_status": None,
    }

    # Product Name
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        data["name"] = h1.get_text(strip=True)
    elif soup.title and soup.title.get_text(strip=True):
        # Clean title suffix (e.g. "Product Name | Store")
        title_text = soup.title.get_text(strip=True)
        for sep in [" | ", " - ", " : ", " • "]:
            if sep in title_text:
                title_text = title_text.split(sep)[0].strip()
                break
        data["name"] = title_text

    # Price & Currency
    price_selectors = [
        {"class_": re.compile(r"(product[_-]?price|current[_-]?price|price[_-]?current|price_color|price)", re.I)},
        {"id": re.compile(r"(product[_-]?price|current[_-]?price|price)", re.I)},
    ]
    for selector in price_selectors:
        tag = soup.find(**selector)
        if tag:
            text = tag.get_text(strip=True)
            p, c = _extract_price_and_currency(text)
            if p != "Unknown":
                data["price"] = p
                data["currency"] = c
                break

    # Stock Status
    stock_selectors = [
        {"class_": re.compile(r"(availability|stock[_-]?status|in[_-]?stock|inventory)", re.I)},
        {"id": re.compile(r"(availability|stock[_-]?status|stock)", re.I)},
    ]
    for selector in stock_selectors:
        tag = soup.find(**selector)
        if tag:
            text = tag.get_text(" ", strip=True)
            cleaned = _clean_stock_status(text)
            if cleaned != "Unknown":
                data["stock_status"] = cleaned
                break

    return data


def scrape_product(url: str) -> dict:
    """
    Fetch a product URL and extract product details.
    
    Returns:
        dict: {
            "name": str,
            "price": str,
            "currency": str,
            "stock_status": str,
            "url": str,
        }
    """
    normalized_url = _validate_and_normalize_url(url)

    try:
        response = requests.get(normalized_url, headers=HEADERS, timeout=10)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise ScrapingError("Request timed out while connecting to the website.", retryable=True)
    except requests.exceptions.ConnectionError:
        raise ScrapingError(f"Failed to connect to the server at '{urlparse(normalized_url).netloc}'. Check your internet connection or URL.", retryable=True)
    except requests.exceptions.HTTPError as err:
        status = err.response.status_code if err.response is not None else None
        if status == 404:
            raise ScrapingError("Product page not found (HTTP 404). Check the URL.", retryable=False)
        elif status == 403:
            raise ScrapingError("Access forbidden (HTTP 403). The website is blocking automated requests.", retryable=False)
        elif status and status >= 500:
            raise ScrapingError(f"Website returned temporary server error {status}.", retryable=True)
        else:
            raise ScrapingError(f"Website returned HTTP error {status}: {err}.", retryable=False)
    except requests.exceptions.RequestException as err:
        raise ScrapingError(f"Network request failed: {err}", retryable=True)

    response.encoding = response.apparent_encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    # 1. Try structured metadata (JSON-LD)
    extracted = _extract_from_json_ld(soup)

    # 2. Try OpenGraph and meta tags for any missing fields
    meta_data = _extract_from_meta_tags(soup)
    for key in ["name", "price", "currency", "stock_status"]:
        if not extracted[key] and meta_data[key]:
            extracted[key] = meta_data[key]

    # 3. Try site-specific parsing for recognized websites
    site_data = _extract_site_specific(soup, normalized_url)
    for key in ["name", "price", "currency", "stock_status"]:
        if not extracted[key] and site_data[key]:
            extracted[key] = site_data[key]

    # 4. Try generic HTML heuristic parsing
    html_data = _extract_html_fallback(soup)
    for key in ["name", "price", "currency", "stock_status"]:
        if not extracted[key] and html_data[key]:
            extracted[key] = html_data[key]

    # Fill defaults for anything unresolved
    name = extracted["name"] or "Unknown"
    price = extracted["price"] or "Unknown"
    currency = extracted["currency"] or "Unknown"
    stock_status = extracted["stock_status"] or "Unknown"

    # If the page completely lacked product information
    if name == "Unknown" and price == "Unknown":
        raise ScrapingError("Could not extract product details. The page may not be a product page or relies on dynamic client-side rendering.")

    return {
        "name": name,
        "price": price,
        "currency": currency,
        "stock_status": stock_status,
        "url": response.url or normalized_url,
    }
