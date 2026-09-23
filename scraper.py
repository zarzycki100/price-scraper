#!/usr/bin/env python3
"""
Scraper cen produktow z Media Expert i RTV Euro AGD.

Odczytuje liste URL-i produktow z products.txt, dla kazdego pobiera strone,
wybiera parser na podstawie domeny (SHOPS) i wyciaga cene regularna, promocyjna
i dostepnosc, a nastepnie dopisuje wiersz z wynikiem do data/prices.csv.

Uruchamiane co 15 min przez GitHub Actions (.github/workflows/scrape.yml).
"""

import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

PRODUCTS_FILE = Path("products.txt")
OUTPUT_CSV = Path("data/prices.csv")

FIELDNAMES = [
    "timestamp",
    "shop",
    "title",
    "part_no",
    "price",
    "sale_price",
    "availability",
    "url",
]

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

# schema.org/ItemAvailability (Euro) -> wartosci takie jak w Media Expert
AVAILABILITY = {
    "InStock": "available",
    "OutOfStock": "unavailable",
    "SoldOut": "unavailable",
    "Discontinued": "unavailable",
}


def format_price(value) -> str | None:
    if value in (None, ""):
        return None
    return f"{float(value):.2f}"


def strip_suffix(title: str | None, pattern: str) -> str | None:
    """Usuwa z <title> doklejony przez sklep dopisek SEO (np. ' - Opinie, Cena - RTV EURO AGD')."""
    return re.sub(pattern, "", title).strip() if title else title


def parse_media_expert(soup: BeautifulSoup, html: str) -> dict:
    """Media Expert: cena regularna i promocyjna w meta-tagach <meta property=...>."""

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", attrs={"property": prop})
        if tag is None:
            # niektore strony uzywaja name= zamiast property=
            tag = soup.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.get("content") else None

    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag else None

    return {
        "title": strip_suffix(title, r"\s+-\s+niskie ceny i opinie w Media Expert$"),
        "part_no": meta(META_KEYS["part_no"]),
        "price": meta(META_KEYS["price"]),
        "sale_price": meta(META_KEYS["sale_price"]),
        "availability": meta(META_KEYS["availability"]),
    }


def parse_euro(soup: BeautifulSoup, html: str) -> dict:
    """RTV Euro AGD: nazwa, PLU i dostepnosc z JSON-LD, ceny z osadzonego stanu strony.

    JSON-LD podaje tylko cene koncowa. Pelne ceny (regularna + promocyjna) sa w
    stanie aplikacji: "prices":{"mainPrice":..,"promotionalPrice":{"price":..}}.
    Na stronie sa tez bloki "prices" produktow polecanych, wiec bierzemy pierwszy
    blok po "eanCode" tego produktu.
    """
    product = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            product = data
            break
    if product is None:
        raise ValueError("brak danych JSON-LD Product na stronie")

    offer = product.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    availability = (offer.get("availability") or "").rsplit("/", 1)[-1]

    price = offer.get("price")
    sale_price = None
    ean = product.get("gtin13")
    ean_pos = html.find(f'"eanCode":"{ean}"') if ean else -1
    if ean_pos >= 0:
        prices_pos = html.find('"prices":{', ean_pos)
        if prices_pos >= 0:
            prices, _ = json.JSONDecoder().raw_decode(html, prices_pos + len('"prices":'))
            if prices.get("mainPrice") is not None:
                price = prices["mainPrice"]
            promo = prices.get("promotionalPrice") or {}
            sale_price = promo.get("price")

    return {
        "title": product.get("name"),
        "part_no": product.get("sku"),
        "price": format_price(price),
        "sale_price": format_price(sale_price),
        "availability": AVAILABILITY.get(availability, availability.lower() or None),
    }


# domena -> (nazwa sklepu wyswietlana na stronie, parser)
SHOPS = {
    "www.mediaexpert.pl": ("Media Expert", parse_media_expert),
    "www.euro.com.pl": ("RTV Euro AGD", parse_euro),
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
    """Pobiera strone produktu i wyciaga dane cenowe parserem wlasciwym dla sklepu."""
    host = urlparse(url).hostname
    if host not in SHOPS:
        raise ValueError(f"nieobslugiwany sklep: {host}")
    shop, parser = SHOPS[host]

    resp = requests.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return {"url": url, "shop": shop, **parser(soup, resp.text)}


def shop_for_url(url: str) -> str:
    return SHOPS.get(urlparse(url).hostname, (urlparse(url).hostname or "",))[0]


def migrate_csv() -> None:
    """Przepisuje istniejacy CSV do aktualnych kolumn (np. po dodaniu kolumny 'shop')."""
    with OUTPUT_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == FIELDNAMES:
            return
        old_rows = list(reader)
    for row in old_rows:
        if not row.get("shop"):
            row["shop"] = shop_for_url(row.get("url") or "")
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(old_rows)
    print(f"Zmigrowano {OUTPUT_CSV} do kolumn: {', '.join(FIELDNAMES)}")


def append_rows(rows: list[dict]) -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    file_exists = OUTPUT_CSV.exists()
    if file_exists:
        migrate_csv()
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
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
            print(f"OK  [{data['shop']}] {data['title']!r} -> cena: {data['price']}, promo: {data['sale_price']}")
        except Exception as exc:  # noqa: BLE001 - chcemy zebrac reszte produktow nawet po bledzie
            print(f"BLAD dla {url}: {exc}", file=sys.stderr)

    if rows:
        append_rows(rows)
        print(f"Zapisano {len(rows)} wierszy do {OUTPUT_CSV}")
    else:
        sys.exit("Nie udalo sie pobrac zadnego produktu.")


if __name__ == "__main__":
    main()
