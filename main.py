import sys
from monitor import run_monitoring
from scraper import scrape_product, ScrapingError


def print_single_product(product: dict) -> None:
    """Print extracted data for a single product (Phase 1 output)."""
    print("\n" + "=" * 50)
    print("                PRODUCT DETAILS")
    print("=" * 50)
    print(f"Product Name : {product['name']}")
    print(f"Current Price: {product['price']}")
    print(f"Currency     : {product['currency']}")
    print(f"Stock Status : {product['stock_status']}")
    print(f"Product URL  : {product['url']}")
    print("=" * 50 + "\n")


def single_scrape(url: str) -> None:
    """Execute single URL scrape (Phase 1 workflow)."""
    try:
        product = scrape_product(url)
        print_single_product(product)
    except ScrapingError as e:
        print(f"\nError: {e}\n")
    except Exception as e:
        print(f"\nUnexpected error occurred: {e}\n")


def main() -> None:
    args = sys.argv[1:]

    # Optional Phase 1 CLI modes
    if args:
        if args[0] in ("-u", "--url") and len(args) > 1:
            single_scrape(args[1])
            return
        elif args[0] in ("-i", "--interactive"):
            try:
                url = input("Enter product URL: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting.")
                return
            if not url:
                print("Error: No URL provided.")
                return
            single_scrape(url)
            return
        elif args[0].startswith("http://") or args[0].startswith("https://"):
            single_scrape(args[0])
            return

    # Default Phase 2 monitoring workflow
    run_monitoring()


if __name__ == "__main__":
    main()
