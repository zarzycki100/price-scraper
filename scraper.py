#!/usr/bin/env python3
"""
Scraper cen produktow z Media Expert.

Odczytuje liste URL-i produktow z products.txt, dla kazdego pobiera strone
i wyciaga cene z meta-tagow (meta-product:price:amount, meta-product:sale_price:amount),
a nastepnie dopisuje wiersz z wynikiem do data/prices.csv.

Uruchamiane co 15 min przez GitHub Actions (.github/workflows/scrape.yml).
"""

import csv
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

PRODUCTS_FILE = Path("products.txt")
OUTPUT_CSV = Path("data/prices.csv")

# Media Expert stoi za Cloudflare, ktory odrzuca (403, "cf-mitigated: challenge")
# zwykle requests/urllib po odcisku TLS/HTTP2. curl_cffi podszywa sie pod
# prawdziwa przegladarke (TLS, HTTP/2, naglowki), wiec przechodzi bez challenge.
IMPERSONATE = "chrome"

# Losowa przerwa (w sekundach) miedzy kolejnymi produktami - rowne odstepy
# co do milisekundy to typowy slad bota.
DELAY_RANGE = (3, 10)

HEADERS = {
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

# Nazwy meta-tagow <meta property="..." content="..."> uzywanych przez Media Expert.
META_KEYS = {
    "price": "product:price:amount",
    "sale_price": "product:sale_price:amount",
    "availability": "product:availability",
    "part_no": "product:retailer_part_no",
}


def load_products(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"Brak pliku {path}. Utworz go i wklej po jednym URL na linie.")
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def fetch_product(url: str) -> dict:
    """Pobiera strone produktu i wyciaga dane cenowe z meta-tagow <meta property=...>."""
    resp = requests.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", attrs={"property": prop})
        if tag is None:
            # niektore strony uzywaja name= zamiast property=
            tag = soup.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.get("content") else None

    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag else None

    return {
        "url": url,
        "title": title,
        "part_no": meta(META_KEYS["part_no"]),
        "price": meta(META_KEYS["price"]),
        "sale_price": meta(META_KEYS["sale_price"]),
        "availability": meta(META_KEYS["availability"]),
    }


def append_rows(rows: list[dict]) -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    file_exists = OUTPUT_CSV.exists()
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "title",
                "part_no",
                "price",
                "sale_price",
                "availability",
                "url",
            ],
        )
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    urls = load_products(PRODUCTS_FILE)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(random.uniform(*DELAY_RANGE))
        try:
            data = fetch_product(url)
            data["timestamp"] = timestamp
            rows.append(data)
            print(f"OK  {data['title']!r} -> cena: {data['price']}, promo: {data['sale_price']}")
        except Exception as exc:  # noqa: BLE001 - chcemy zebrac reszte produktow nawet po bledzie
            print(f"BLAD dla {url}: {exc}", file=sys.stderr)

    if rows:
        append_rows(rows)
        print(f"Zapisano {len(rows)} wierszy do {OUTPUT_CSV}")
    else:
        sys.exit("Nie udalo sie pobrac zadnego produktu.")


if __name__ == "__main__":
    main()
