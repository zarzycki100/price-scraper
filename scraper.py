#!/usr/bin/env python3
"""
Scraper cen produktow z RTV Euro AGD, Media Expert i MediaMarkt.

Odczytuje liste produktow z products.csv (kolumny: product - wspolna nazwa
produktu, url - strona produktu w jednym sklepie; ten sam produkt w kilku
sklepach to kilka wierszy z ta sama nazwa). Dla kazdego URL-a pobiera strone,
wybiera parser na podstawie domeny (SHOPS) i wyciaga cene regularna, promocyjna
i dostepnosc, a nastepnie dopisuje wiersz z wynikiem do data/prices.csv.
Strona (docs/index.html) laczy ceny z products.csv po URL-u.

Uruchamiane co 15 min z lokalnego crona (scripts/cron_scrape.sh).
"""

import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

PRODUCTS_FILE = Path("products.csv")
OUTPUT_CSV = Path("data/prices.csv")

# Stan miedzy przebiegami (przerwy po blokadach) i profil przegladarki z cookies.
# Poza repo, bo cron robi na swoim klonie `git reset --hard`.
STATE_DIR = Path(os.environ.get("SCRAPER_STATE_DIR") or Path.home() / ".local/state/price-scraper")
STATE_FILE = STATE_DIR / "state.json"
BROWSER_PROFILE_DIR = STATE_DIR / "browser-profile"

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

# Sklepy stoja za bot-managerami (Media Expert - Cloudflare, Euro - Akamai),
# ktore oceniaja nie tylko odcisk TLS/HTTP2, ale tez wykonuja w przegladarce
# JavaScript zbierajacy dane o srodowisku i zachowaniu. Dlatego strony pobiera
# prawdziwy Google Chrome (Playwright) z trwalym profilem na dysku - i to
# w trybie z oknem, na wirtualnym ekranie Xvfb: headless Chromium Akamai
# rozpoznawal i blokowal (403), choc zwykla przegladarka z tego IP przechodzila.
BROWSER_CHANNEL = "chrome"
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-position=0,0",
    # zawsze X11 (Xvfb) - inaczej w sesji Wayland Chrome otwieralby okno na pulpicie
    "--ozone-platform=x11",
]
VIEWPORTS = [(1920, 1080), (1536, 864), (1440, 900), (1366, 768), (1600, 900)]
PAGE_TIMEOUT_MS = 45_000

# Losowa przerwa (w sekundach) miedzy kolejnymi produktami - rowne odstepy
# co do milisekundy to typowy slad bota. Czasem robimy dluzsza pauze,
# jak czlowiek, ktory zatrzymal sie na stronie produktu.
#
# Przebieg (~45 stron w 3 sklepach) musi zmiescic sie w 15 min miedzy uruchomieniami
# crona - okolo 10 s na strone razem z ladowaniem i przewijaniem. Sklepy sa
# przeplatane (losowa kolejnosc), wiec kazdy dostaje zapytanie srednio co ~30 s.
DELAY_RANGE = (3, 8)
LONG_PAUSE_CHANCE = 0.05
LONG_PAUSE_RANGE = (12, 25)

# Odpowiedzi oznaczajace blokade / limit. Ponawiamy (z rosnaca przerwa) tylko
# przeciazenie i limit - 403 od bot-managera to decyzja, a nie chwilowy blad,
# i ponawianie tylko pogarsza ocene IP.
BLOCK_STATUSES = {403, 429, 503}
RETRY_STATUSES = {429, 503}
RETRY_DELAYS = [(20, 40), (60, 90)]
# Po tylu kolejnych blokadach z jednego sklepu odpuszczamy go w tym przebiegu,
# zeby nie dobijac sie dalej i nie utrwalac blokady.
MAX_SHOP_FAILURES = 2

# Jak dlugo trzymamy ten sam profil przegladarki (cookies, localStorage). Powracajacy
# "uzytkownik" z tymi samymi cookies wyglada naturalniej niz nowa, pusta
# przegladarka przy kazdym przebiegu - ale co kilka dni zmieniamy tozsamosc.
IDENTITY_TTL_RANGE = (1 * 86400, 3 * 86400)

# Po blokadzie sklep odpoczywa przez kilka przebiegow: 1 h, 2 h, 4 h ... max 12 h.
# Dobijanie sie przy kazdym przebiegu do zablokowanego sklepu tylko przedluza blokade.
COOLDOWN_BASE = 3600
COOLDOWN_MAX = 12 * 3600

# Strony "sprawdzania przegladarki" / blokady potrafia przyjsc z kodem 200.
CHALLENGE_MARKERS = (
    "<title>Just a moment",
    "Attention Required! | Cloudflare",
    "cf-chl",
    "RTV EURO AGD - Blokada",
)

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


# onlineStatus z MediaMarkt -> wartosci takie jak w Media Expert
MEDIAMARKT_STATUS = {
    "AVAILABLE": "available",
    "NOT_AVAILABLE": "unavailable",
    "TEMPORARILY_NOT_AVAILABLE": "unavailable",
    "PERMANENTLY_NOT_AVAILABLE": "unavailable",
}


def find_json_ld_product(soup: BeautifulSoup) -> dict | None:
    """Pierwszy obiekt Product z JSON-LD (takze zagniezdzony, np. w BuyAction.object).

    Produkty z wariantami (kolor, pojemnosc) sa opisane jako ProductGroup - jego
    sku i offers dotycza ogladanego wariantu, wiec traktujemy go jak Product.
    """
    types = ("Product", "ProductGroup")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            if item.get("@type") in types:
                return item
            nested = item.get("object")
            if isinstance(nested, dict) and nested.get("@type") in types:
                return nested
    return None


def parse_mediamarkt(soup: BeautifulSoup, html: str) -> dict:
    """MediaMarkt: nazwa, SKU i cena koncowa z JSON-LD, pozostale ceny ze stanu aplikacji.

    Stan (Apollo/GraphQL) ma dla kazdego produktu na stronie wpis
    {"__typename":"CofrPriceFeature","id":"Media:pl:<SKU>", "price":{"amount":..},
    "promoPrice":{"amount":..}, "strikePrice":{"amount":..}|null}. Sa tam tez
    produkty polecane, wiec szukamy wpisu z SKU tego produktu.
    """
    product = find_json_ld_product(soup)
    if product is None:
        raise ValueError("brak danych JSON-LD Product na stronie")
    sku = str(product.get("sku") or "")
    offer = product.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    current = offer.get("price")
    availability = (offer.get("availability") or "").rsplit("/", 1)[-1]
    availability = AVAILABILITY.get(availability, availability.lower() or None)

    regular = current
    marker = f'{{"__typename":"CofrPriceFeature","id":"Media:pl:{sku}"'
    pos = html.find(marker) if sku else -1
    if pos >= 0:
        feature, _ = json.JSONDecoder().raw_decode(html, pos)
        amounts = {k: (feature.get(k) or {}).get("amount") for k in ("price", "promoPrice", "strikePrice")}
        if current is None:
            current = min((v for v in (amounts["price"], amounts["promoPrice"]) if v is not None), default=None)
        # cena regularna = wyzsza z ceny bazowej i przekreslonej
        candidates = [v for v in (amounts["price"], amounts["strikePrice"]) if v is not None]
        if candidates:
            regular = max(candidates)
        status_pos = html.find(f'"CofrOnlineStatusFeature","id":"Media:pl:{sku}"')
        if status_pos >= 0:
            status = re.search(r'"onlineStatus":"(\w+)"', html[status_pos:status_pos + 2000])
            if status:
                availability = MEDIAMARKT_STATUS.get(status.group(1), status.group(1).lower())

    sale = current if current is not None and regular is not None and float(current) < float(regular) else None
    return {
        "title": " ".join((product.get("name") or "").split()) or None,
        "part_no": sku or None,
        "price": format_price(regular if regular is not None else current),
        "sale_price": format_price(sale),
        "availability": availability,
    }


# domena -> (nazwa sklepu wyswietlana na stronie, parser)
SHOPS = {
    "www.mediaexpert.pl": ("Media Expert", parse_media_expert),
    "www.euro.com.pl": ("RTV Euro AGD", parse_euro),
    "mediamarkt.pl": ("MediaMarkt", parse_mediamarkt),
}


def load_products(path: Path) -> list[str]:
    """URL-e z products.csv (kolumny product,url). Wiersze bez URL-a sa pomijane."""
    if not path.exists():
        sys.exit(f"Brak pliku {path}. Utworz go z kolumnami: product,url")
    urls = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()
            if not url or url.startswith("#"):
                continue
            if urlparse(url).hostname not in SHOPS:
                print(f"UWAGA: nieobslugiwany sklep, pomijam: {url}", file=sys.stderr)
                continue
            if url in urls:
                print(f"UWAGA: zdublowany URL, pomijam: {url}", file=sys.stderr)
                continue
            urls.append(url)
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
    """Po blokadzie porzucamy profil przegladarki - mogl zostac oznaczony jako bot."""
    state.pop("identity", None)


@contextmanager
def virtual_display(width: int, height: int):
    """Wirtualny ekran Xvfb - przegladarka dziala "z oknem", ale nic nie pokazuje sie na pulpicie."""
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", "1", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    display = proc.stdout.readline().decode().strip()
    if not display:
        proc.kill()
        raise RuntimeError("Xvfb nie wystartowal (sudo apt install xvfb)")
    try:
        yield f":{display}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@contextmanager
def open_browser(state: dict):
    """Chromium z profilem na dysku - ten sam przez kilka dni (cookies, localStorage).

    Powracajacy uzytkownik z historia wyglada naturalniej niz nowa, pusta
    przegladarka przy kazdym przebiegu. Po wygasnieciu lub blokadzie profil
    jest kasowany i zaczynamy od zera.
    """
    now = time.time()
    identity = state.get("identity")
    if not identity or identity.get("expires", 0) < now or "viewport" not in identity:
        shutil.rmtree(BROWSER_PROFILE_DIR, ignore_errors=True)
        identity = state["identity"] = {
            "viewport": random.choice(VIEWPORTS),
            "expires": now + random.uniform(*IDENTITY_TTL_RANGE),
        }
        print("Nowy profil przegladarki")

    width, height = identity["viewport"]
    with virtual_display(width, height) as display, sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            channel=BROWSER_CHANNEL,
            headless=False,
            env={**{k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}, "DISPLAY": display},
            args=[*BROWSER_ARGS, f"--window-size={width},{height}"],
            # bez --enable-automation: pasek "Chrome jest kontrolowany..." i navigator.webdriver
            ignore_default_args=["--enable-automation"],
            no_viewport=True,  # okno na caly ekran, jak u zwyklego uzytkownika
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
        )
        context.set_default_timeout(PAGE_TIMEOUT_MS)
        try:
            yield context.pages[0] if context.pages else context.new_page()
        finally:
            context.close()


def human_pause() -> None:
    if random.random() < LONG_PAUSE_CHANCE:
        time.sleep(random.uniform(*LONG_PAUSE_RANGE))
    else:
        time.sleep(random.uniform(*DELAY_RANGE))


def browse_a_bit(page: Page) -> None:
    """Kilka ruchow myszy i przewiniec - skrypty bot-managerow zbieraja takie zdarzenia."""
    width, height = page.evaluate("[innerWidth, innerHeight]")
    for _ in range(random.randint(2, 5)):
        page.mouse.move(random.randint(50, width - 50), random.randint(50, height - 50),
                        steps=random.randint(5, 25))
        page.mouse.wheel(0, random.randint(150, 700))
        page.wait_for_timeout(random.randint(400, 1500))


def is_blocked(status: int, headers: dict, body: str) -> bool:
    if status in BLOCK_STATUSES or headers.get("cf-mitigated"):
        return True
    return any(marker in body for marker in CHALLENGE_MARKERS)


def load_page(page: Page, url: str) -> str:
    """Otwiera strone i zwraca HTML z serwera (przed przerobkami JS), z ponawianiem po blokadzie.

    Bierzemy tresc odpowiedzi, a nie page.content(): po hydratacji aplikacja
    moze usunac z DOM osadzony stan strony, z ktorego parsery czytaja ceny.
    """
    for attempt in range(len(RETRY_DELAYS) + 1):
        resp = page.goto(url, wait_until="domcontentloaded")
        if resp is None:
            raise RuntimeError("brak odpowiedzi serwera")
        status, headers, body = resp.status, resp.headers, resp.text()
        if not is_blocked(status, headers, body):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            browse_a_bit(page)
            return body
        if status not in RETRY_STATUSES or attempt == len(RETRY_DELAYS):
            break
        retry_after = headers.get("retry-after", "")
        wait = int(retry_after) if retry_after.isdigit() else random.uniform(*RETRY_DELAYS[attempt])
        if wait > max(RETRY_DELAYS[-1]):
            break  # kaze czekac dluzej niz przebieg - niech zadziala cooldown
        print(f"Blokada (HTTP {status}) dla {url} - ponawiam za {wait:.0f} s", file=sys.stderr)
        time.sleep(wait)
    raise Blocked(f"HTTP {status}, blokada po {attempt + 1} probach")


def home_url(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}/"


def has_cookies_for(page: Page, url: str) -> bool:
    return bool(page.context.cookies(home_url(url)))


def warm_up(page: Page, url: str) -> None:
    """Wejscie na strone glowna sklepu przed produktami.

    Przegladarka bez zadnych cookies wchodzaca prosto na strone produktu
    wyglada dla bot-managera podejrzanie. Strona glowna ustawia jego cookies
    (w Euro: ak_bmsc, bm_s ...), z ktorymi przechodza strony produktow.
    """
    print(f"Rozgrzewka: strona glowna {home_url(url)}")
    load_page(page, home_url(url))
    human_pause()


def fetch_product(page: Page, url: str) -> dict:
    """Pobiera strone produktu i wyciaga dane cenowe parserem wlasciwym dla sklepu."""
    host = urlparse(url).hostname
    if host not in SHOPS:
        raise ValueError(f"nieobslugiwany sklep: {host}")
    shop, parser = SHOPS[host]

    html = load_page(page, url)
    soup = BeautifulSoup(html, "html.parser")
    return {"url": url, "shop": shop, **parser(soup, html)}


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
    # losowa kolejnosc - ten sam porzadek w kazdym przebiegu to latwy do wylapania wzorzec
    random.shuffle(urls)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state()
    cooldowns: dict[str, dict] = state.setdefault("cooldowns", {})

    to_fetch = []
    for url in urls:
        host = urlparse(url).hostname
        until = cooldowns.get(host, {}).get("until", 0)
        if until > time.time():
            left = (until - time.time()) / 60
            print(f"POMIJAM {url}: {host} odpoczywa po blokadzie jeszcze {left:.0f} min", file=sys.stderr)
        else:
            to_fetch.append(url)
    if not to_fetch:
        print("Wszystkie sklepy odpoczywaja po blokadzie - nic nie pobieram.")
        return

    rows = []
    attempted = 0
    blocked: dict[str, int] = {}  # host -> liczba kolejnych blokad w tym przebiegu
    warmed: set[str] = set()
    with open_browser(state) as page:
        for url in to_fetch:
            host = urlparse(url).hostname
            if blocked.get(host, 0) >= MAX_SHOP_FAILURES:
                print(f"POMIJAM {url}: {host} blokuje w tym przebiegu", file=sys.stderr)
                continue
            if attempted > 0:
                human_pause()
            attempted += 1
            try:
                if host not in warmed and not has_cookies_for(page, url):
                    warmed.add(host)
                    warm_up(page, url)
                data = fetch_product(page, url)
                blocked[host] = 0
                cooldowns.pop(host, None)
                data["timestamp"] = timestamp
                rows.append(data)
                print(f"OK  [{data['shop']}] {data['title']!r} -> cena: {data['price']}, promo: {data['sale_price']}")
            except Blocked as exc:
                blocked[host] = blocked.get(host, 0) + 1
                if blocked[host] >= MAX_SHOP_FAILURES:
                    streak = cooldowns.get(host, {}).get("streak", 0) + 1
                    pause = min(COOLDOWN_BASE * 2 ** (streak - 1), COOLDOWN_MAX) * random.uniform(0.8, 1.2)
                    cooldowns[host] = {"until": time.time() + pause, "streak": streak}
                    print(f"{host} blokuje - przerwa {pause / 3600:.1f} h, zmiana profilu przegladarki", file=sys.stderr)
                print(f"BLAD dla {url}: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - chcemy zebrac reszte produktow nawet po bledzie
                print(f"BLAD dla {url}: {exc}", file=sys.stderr)

    if any(n >= MAX_SHOP_FAILURES for n in blocked.values()):
        forget_identity(state)
    save_state(state)

    if rows:
        append_rows(rows)
        print(f"Zapisano {len(rows)} wierszy do {OUTPUT_CSV}")
    else:
        sys.exit("Nie udalo sie pobrac zadnego produktu.")


if __name__ == "__main__":
    main()
