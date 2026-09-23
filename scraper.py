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
import os
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

# Stan miedzy przebiegami (profil przegladarki, cookies, przerwy po blokadach).
# Poza repo, bo cron robi na swoim klonie `git reset --hard`.
STATE_FILE = Path(
    os.environ.get("SCRAPER_STATE_DIR") or Path.home() / ".local/state/price-scraper"
) / "state.json"

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
#
# Profil losujemy raz na przebieg (a nie na request): prawdziwy uzytkownik nie
# zmienia przegladarki miedzy kliknieciami, a rozne odciski TLS z jednego IP
# w ciagu minuty wygladaja podejrzanie. Tylko wspolczesne profile desktopowe.
IMPERSONATE_PROFILES = [
    "chrome136", "chrome142", "chrome145", "chrome146",
    "edge101",
    "safari184", "safari260",
    "firefox144", "firefox147",
]

# Losowa przerwa (w sekundach) miedzy kolejnymi produktami - rowne odstepy
# co do milisekundy to typowy slad bota. Czasem robimy dluzsza pauze,
# jak czlowiek, ktory zatrzymal sie na stronie produktu.
DELAY_RANGE = (4, 12)
LONG_PAUSE_CHANCE = 0.15
LONG_PAUSE_RANGE = (15, 40)

# Odpowiedzi oznaczajace blokade / limit - ponawiamy z rosnaca przerwa.
RETRY_STATUSES = {403, 429, 503}
RETRY_DELAYS = [(20, 40), (60, 90)]
# Po tylu kolejnych blokadach z jednego sklepu odpuszczamy go w tym przebiegu,
# zeby nie dobijac sie dalej i nie utrwalac blokady.
MAX_SHOP_FAILURES = 2

# Jak dlugo trzymamy ten sam profil przegladarki i cookies. Powracajacy
# "uzytkownik" z tymi samymi cookies wyglada naturalniej niz nowa, pusta
# przegladarka co 15 minut - ale co kilka dni zmieniamy tozsamosc.
IDENTITY_TTL_RANGE = (1 * 86400, 3 * 86400)

# Po blokadzie sklep odpoczywa przez kilka przebiegow: 1 h, 2 h, 4 h ... max 12 h.
# Dobijanie sie co 15 min do zablokowanego sklepu tylko przedluza blokade.
COOLDOWN_BASE = 3600
COOLDOWN_MAX = 12 * 3600

# Strony "sprawdzania przegladarki" (np. Cloudflare) potrafia przyjsc z kodem 200.
CHALLENGE_MARKERS = ("<title>Just a moment", "Attention Required! | Cloudflare", "cf-chl")

ACCEPT_LANGUAGES = [
    "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "pl-PL,pl;q=0.9,en;q=0.8",
    "pl,en-US;q=0.9,en;q=0.8",
    "pl-PL,pl;q=0.8,en-US;q=0.5,en;q=0.3",
]

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


class Blocked(Exception):
    """Sklep odrzucil zapytanie (403/429/503 albo strona challenge)."""


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def forget_identity(state: dict) -> None:
    """Po blokadzie porzucamy profil i cookies - mogly zostac oznaczone jako bot."""
    state.pop("identity", None)
    state.pop("cookies", None)


def new_session(state: dict) -> requests.Session:
    """Sesja udajaca jedna przegladarke - ta sama przez kilka dni, z zapisanymi cookies.

    Sesja trzyma cookies miedzy requestami (np. te ustawiane przez Cloudflare),
    tak jak robi to prawdziwa przegladarka.
    """
    now = time.time()
    identity = state.get("identity")
    if not identity or identity.get("expires", 0) < now or identity.get("profile") not in IMPERSONATE_PROFILES:
        forget_identity(state)
        identity = state["identity"] = {
            "profile": random.choice(IMPERSONATE_PROFILES),
            "accept_language": random.choice(ACCEPT_LANGUAGES),
            "expires": now + random.uniform(*IDENTITY_TTL_RANGE),
        }
        print(f"Nowy profil przegladarki: {identity['profile']}")
    else:
        print(f"Profil przegladarki: {identity['profile']}")

    session = requests.Session(
        impersonate=identity["profile"],
        headers={"Accept-Language": identity["accept_language"]},
        timeout=20,
    )
    for c in state.get("cookies", []):
        if c.get("expires") is None or c["expires"] > now:
            session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return session


def dump_cookies(session: requests.Session) -> list[dict]:
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": c.expires}
        for c in session.cookies.jar
    ]


def human_pause() -> None:
    if random.random() < LONG_PAUSE_CHANCE:
        time.sleep(random.uniform(*LONG_PAUSE_RANGE))
    else:
        time.sleep(random.uniform(*DELAY_RANGE))


def is_blocked(resp: requests.Response) -> bool:
    if resp.status_code in RETRY_STATUSES or resp.headers.get("cf-mitigated"):
        return True
    return any(marker in resp.text for marker in CHALLENGE_MARKERS)


def get_with_retry(session: requests.Session, url: str) -> requests.Response:
    """GET z ponawianiem po blokadzie (z uwzglednieniem Retry-After)."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        resp = session.get(url)
        if not is_blocked(resp):
            resp.raise_for_status()
            return resp
        if attempt == len(RETRY_DELAYS):
            break
        retry_after = resp.headers.get("Retry-After", "")
        wait = int(retry_after) if retry_after.isdigit() else random.uniform(*RETRY_DELAYS[attempt])
        if wait > max(RETRY_DELAYS[-1]):
            break  # kaze czekac dluzej niz przebieg - niech zadziala cooldown
        print(f"Blokada (HTTP {resp.status_code}) dla {url} - ponawiam za {wait:.0f} s", file=sys.stderr)
        time.sleep(wait)
    raise Blocked(f"HTTP {resp.status_code}, blokada po {attempt + 1} probach")


def fetch_product(session: requests.Session, url: str) -> dict:
    """Pobiera strone produktu i wyciaga dane cenowe parserem wlasciwym dla sklepu."""
    host = urlparse(url).hostname
    if host not in SHOPS:
        raise ValueError(f"nieobslugiwany sklep: {host}")
    shop, parser = SHOPS[host]

    resp = get_with_retry(session, url)
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
    # losowa kolejnosc - ten sam porzadek co 15 min to latwy do wylapania wzorzec
    random.shuffle(urls)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state()
    cooldowns: dict[str, dict] = state.setdefault("cooldowns", {})
    session = new_session(state)

    rows = []
    attempted = 0
    blocked: dict[str, int] = {}  # host -> liczba kolejnych blokad w tym przebiegu
    for url in urls:
        host = urlparse(url).hostname
        cooldown = cooldowns.get(host, {})
        if cooldown.get("until", 0) > time.time():
            left = (cooldown["until"] - time.time()) / 60
            print(f"POMIJAM {url}: {host} odpoczywa po blokadzie jeszcze {left:.0f} min", file=sys.stderr)
            continue
        if blocked.get(host, 0) >= MAX_SHOP_FAILURES:
            print(f"POMIJAM {url}: {host} blokuje w tym przebiegu", file=sys.stderr)
            continue
        if attempted > 0:
            human_pause()
        attempted += 1
        try:
            data = fetch_product(session, url)
            blocked[host] = 0
            cooldowns.pop(host, None)
            data["timestamp"] = timestamp
            rows.append(data)
            print(f"OK  [{data['shop']}] {data['title']!r} -> cena: {data['price']}, promo: {data['sale_price']}")
        except Blocked as exc:
            blocked[host] = blocked.get(host, 0) + 1
            if blocked[host] >= MAX_SHOP_FAILURES:
                streak = cooldown.get("streak", 0) + 1
                pause = min(COOLDOWN_BASE * 2 ** (streak - 1), COOLDOWN_MAX) * random.uniform(0.8, 1.2)
                cooldowns[host] = {"until": time.time() + pause, "streak": streak}
                print(f"{host} blokuje - przerwa {pause / 3600:.1f} h, zmiana profilu przegladarki", file=sys.stderr)
            print(f"BLAD dla {url}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - chcemy zebrac reszte produktow nawet po bledzie
            print(f"BLAD dla {url}: {exc}", file=sys.stderr)

    if any(n >= MAX_SHOP_FAILURES for n in blocked.values()):
        forget_identity(state)
    else:
        state["cookies"] = dump_cookies(session)
    save_state(state)

    if rows:
        append_rows(rows)
        print(f"Zapisano {len(rows)} wierszy do {OUTPUT_CSV}")
    elif attempted == 0:
        print("Wszystkie sklepy odpoczywaja po blokadzie - nic nie pobieram.")
    else:
        sys.exit("Nie udalo sie pobrac zadnego produktu.")


if __name__ == "__main__":
    main()
