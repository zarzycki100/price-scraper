#!/usr/bin/env python3
"""
Dodawanie produktow do products.csv: wyszukiwanie produktu we wszystkich sklepach,
potwierdzenie przez uzytkownika i dopiero wtedy dodanie do sledzenia.

Kolejka to issues na GitHubie z etykieta "dodaj-produkt" - tworzy je ukryta strona
docs/dodaj.html. Skrypt uruchamiany z crona (co 5 min i przed scraperem) obsluguje
tylko zgloszenia autora repozytorium:

  wyszukanie (znacznik price-scraper:dodaj, bez etykiety "wyniki"):
    1. produkt zrodlowy - z linku do sklepu albo wyszukujac nazwe w sklepach,
    2. w pozostalych sklepach kandydaci z wyszukiwarki sklepu (kilka zapytan,
       zapasowo DuckDuckGo "site:sklep"); najlepsi sa otwierani i porownywani po EAN,
    3. komentarz z wynikami (tabela + JSON dla strony) i etykieta "wyniki";
       nic nie jest jeszcze dodawane,
  potwierdzenie (znacznik price-scraper:potwierdz, tworzy je strona po wyborze ofert):
    4. dopisuje wybrane linki do products.csv pod podana nazwa,
    5. komentuje i zamyka potwierdzenie oraz wyszukanie.

Reczne uruchomienie (bez GitHuba):
  python3 add_products.py --query "https://www.euro.com.pl/..."      # tylko wyniki
  python3 add_products.py --query "Sony WH-1000XM5" --add --name "Sony WH-1000XM5"
"""

import argparse
import csv
import json
import random
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

import scraper

ISSUE_LABEL = "dodaj-produkt"
RESULTS_LABEL = "wyniki"
SEARCH_MARKER = "<!-- price-scraper:dodaj -->"
CONFIRM_MARKER = "<!-- price-scraper:potwierdz -->"
RESULTS_MARKER = "price-scraper:wyniki"

# Ile stron kandydatow najwyzej otwieramy w jednym sklepie (kazda to zapytanie do sklepu):
# z wyszukiwarki sklepu i osobno z DuckDuckGo, zeby podobne produkty z wyszukiwarki
# nie zuzyly calego limitu przed wyszukiwaniem zapasowym.
MAX_CANDIDATE_PAGES = 4
MAX_WEB_CANDIDATE_PAGES = 2
# Ile najlepszych wynikow z jednego wyszukiwania sprawdzamy.
TOP_PER_QUERY = 3
# Ponizej tego podobienstwa nazwy kandydat nie jest w ogole otwierany.
MIN_SIMILARITY = 0.25

SEARCH_URLS = {
    "www.euro.com.pl": "https://www.euro.com.pl/search.bhtml?keyword={q}",
    "www.mediaexpert.pl": "https://www.mediaexpert.pl/search?query%5Bmenu_item%5D=&query%5Bquerystring%5D={q}",
    "mediamarkt.pl": "https://mediamarkt.pl/pl/search.html?query={q}",
}
PRODUCT_URL = {
    "www.euro.com.pl": re.compile(r"^https://www\.euro\.com\.pl/(?!cms/|marka/)[a-z0-9-]+/[a-z0-9-]+\.bhtml$"),
    "www.mediaexpert.pl": re.compile(r"^https://www\.mediaexpert\.pl/(?!search)[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+$"),
    "mediamarkt.pl": re.compile(r"^https://mediamarkt\.pl/pl/product/_[a-z0-9-]+-\d+\.html$"),
}
# jednostki, ktore wygladaja jak kody modeli (256GB, 11kg), ale nimi nie sa
UNIT_RE = re.compile(r"^\d+([.,]\d+)?(gb|tb|mb|w|kw|kg|g|mm|cm|m|hz|mhz|ghz|mah|l|v|mpix|obr|cali|gbit|gbits|s|ms)$", re.I)


@dataclass
class Product:
    url: str
    host: str
    title: str
    ean: str | None
    brand: str | None
    price: str | None = None
    sale_price: str | None = None
    availability: str | None = None


@dataclass
class ShopResult:
    host: str
    status: str  # "match", "similar", "none", "blocked", "skipped", "source"
    product: Product | None = None
    note: str = ""
    similar: list = field(default_factory=list)  # [(Product, score)]


def shop_name(host: str) -> str:
    return scraper.SHOPS[host][0]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return text.lower()


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", normalize(text)) if len(t) > 1}


def model_codes(title: str) -> list[str]:
    """Kody modeli z nazwy: tokeny z literami i cyframi (TUF-AX3000, WW11DB7B34GWU4) albo dlugie liczby (200925)."""
    codes = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9/-]*[A-Za-z0-9]", title or ""):
        has_digit = any(c.isdigit() for c in raw)
        has_alpha = any(c.isalpha() for c in raw)
        if UNIT_RE.match(raw) or UNIT_RE.match(raw.split("/")[0]):  # 1400obr/min
            continue
        if (has_digit and has_alpha and len(raw) >= 4) or (raw.isdigit() and len(raw) >= 5):
            codes.append(raw)
    return codes


def series_words(title: str, brand: str | None = None) -> list[str]:
    """Wyrozniajace slowa pisane wersalikami (TUF, ROG, JBL) - bez marki i bez kodow modeli."""
    skip = {compact(brand or ""), "wifi", "usb", "led", "oled", "hdmi", "ssd", "ram", "rgb", "pro", "max", "plus"}
    words = re.findall(r"\b[A-Z]{3,6}\b", title or "")
    return [w for w in dict.fromkeys(words) if compact(w) not in skip]


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text))


def similarity(reference: str, candidate: str, brand: str | None = None) -> float:
    """0..1: wspolne slowa nazwy (Jaccard) + premia za kody modelu i slowa serii obecne w kandydacie."""
    a, b = tokens(reference), tokens(candidate)
    jaccard = len(a & b) / len(a | b) if a | b else 0.0
    cand = compact(candidate)
    parts = [(0.35, jaccard)]
    codes = model_codes(reference)
    if codes:
        parts.append((0.45, sum(1 for c in codes if compact(c) in cand) / len(codes)))
    series = series_words(reference, brand)
    if series:
        parts.append((0.2, sum(1 for w in series if compact(w) in cand) / len(series)))
    total = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / total


def candidate_score(source: Product, title: str) -> float:
    """Podobienstwo kandydata do zrodla; inna marka (np. inny router "AX3000") mocno obniza ocene."""
    score = similarity(source.title, title, source.brand)
    if source.brand and compact(source.brand) not in compact(title):
        score *= 0.3
    return score


def identify(html: str, url: str) -> Product | None:
    """Nazwa, EAN i marka ze strony produktu (JSON-LD - jest we wszystkich trzech sklepach)."""
    soup = BeautifulSoup(html, "html.parser")
    ld = scraper.find_json_ld_product(soup)
    if ld is None:
        return None
    ean = next((str(ld[k]).strip() for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8") if ld.get(k)), None)
    brand = ld.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    title = " ".join(str(ld.get("name") or "").split())
    return Product(url=url, host=urlparse(url).hostname, title=title, ean=ean or None, brand=brand or None)


class Searcher:
    """Wyszukiwanie w sklepach jedna przegladarka (ta sama co scraper)."""

    def __init__(self, page):
        self.page = page
        self.requests = 0
        self.warmed: set[str] = set()

    def _goto(self, url: str) -> str:
        if self.requests:
            scraper.human_pause()
        self.requests += 1
        resp = self.page.goto(url, wait_until="domcontentloaded")
        if resp is None:
            raise RuntimeError("brak odpowiedzi serwera")
        body = resp.text()
        if scraper.is_blocked(resp.status, resp.headers, body):
            raise scraper.Blocked(f"HTTP {resp.status}")
        return body

    def warm_up(self, host: str) -> None:
        if host in self.warmed:
            return
        self.warmed.add(host)
        if not scraper.has_cookies_for(self.page, f"https://{host}/"):
            self._goto(f"https://{host}/")

    def product(self, url: str) -> Product | None:
        self.warm_up(urlparse(url).hostname)
        html = self._goto(url)
        scraper.browse_a_bit(self.page)
        found = identify(html, url)
        if found:
            # cena i dostepnosc tym samym parserem co scraper - do pokazania w wynikach
            try:
                data = scraper.SHOPS[found.host][1](BeautifulSoup(html, "html.parser"), html)
                found.price, found.sale_price = data.get("price"), data.get("sale_price")
                found.availability = data.get("availability")
            except Exception:  # noqa: BLE001 - brak ceny nie przeszkadza w dopasowaniu
                pass
        return found

    def search(self, host: str, query: str) -> list[tuple[str, str]]:
        """[(tytul, url)] z wyszukiwarki sklepu; przekierowanie prosto na produkt = jeden wynik."""
        self.warm_up(host)
        self._goto(SEARCH_URLS[host].format(q=quote(query)))
        if host == "www.mediaexpert.pl":
            self._wait("div.offer-box")
        elif host == "mediamarkt.pl":
            self._wait('[data-test="mms-search-srp-productlist"]')
        html = self.page.content()
        final = self.page.url.split("?")[0].split("#")[0]
        if PRODUCT_URL[host].match(final):
            found = identify(html, final)
            return [(found.title if found else "", final)]
        soup = BeautifulSoup(html, "html.parser")
        results = []
        if host == "www.euro.com.pl":
            tag = soup.find("script", id="product-listing-schema")
            data = json.loads(tag.string) if tag and tag.string else {}
            for el in data.get("itemListElement", []):
                item = el.get("item") or {}
                link = item.get("url") or ""
                if link and not link.startswith("http"):
                    link = "https://" + link.lstrip("/")
                results.append((item.get("name") or "", link))
        elif host == "www.mediaexpert.pl":
            for box in soup.select("div.offer-box"):
                a = box.select_one(".name a[href]") or box.find("a", href=True)
                if a:
                    results.append((a.get_text(" ", strip=True), urljoin(final, a["href"])))
        elif host == "mediamarkt.pl":
            box = soup.select_one('[data-test="mms-search-srp-productlist"]')
            for card in box.select('[data-test="mms-product-card"]') if box else []:
                a = card.find("a", href=True)
                t = card.select_one('[data-test="product-title"]')
                if a:
                    results.append((t.get_text(" ", strip=True) if t else a.get_text(" ", strip=True), urljoin(final, a["href"])))
        return self._clean(host, results)

    def web_search(self, host: str, query: str) -> list[tuple[str, str]]:
        """Zapasowo: DuckDuckGo z ograniczeniem do domeny sklepu (np. gdy wyszukiwarka sklepu nie zna modelu)."""
        html = self._goto("https://html.duckduckgo.com/html/?q=" + quote(f"site:{host} {query}"))
        results = []
        for a in BeautifulSoup(html, "html.parser").select("a.result__a[href]"):
            href = a["href"]
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            results.append((a.get_text(" ", strip=True), href))
        return self._clean(host, results)

    def _wait(self, selector: str) -> None:
        try:
            self.page.wait_for_selector(selector, timeout=6000)
        except Exception:  # noqa: BLE001 - brak wynikow to tez wynik
            pass

    @staticmethod
    def _clean(host: str, results: list[tuple[str, str]]) -> list[tuple[str, str]]:
        out, seen = [], set()
        for title, link in results:
            link = link.split("?")[0].split("#")[0]
            if PRODUCT_URL[host].match(link) and link not in seen:
                seen.add(link)
                out.append((title, link))
        return out


def queries_for(source: Product) -> list[str]:
    """Zapytania od najbardziej do najmniej precyzyjnych."""
    codes = model_codes(source.title)
    qs = []
    if source.ean:
        qs.append(source.ean)
    if codes:
        qs.append(model_query(source))
        qs.append(" ".join(codes))
    words = [w for w in source.title.split() if not UNIT_RE.match(w)]
    qs.append(" ".join(words[:6]))
    return list(dict.fromkeys(q for q in qs if q))


def model_query(source: Product) -> str:
    """Marka + slowa serii + kody modelu, np. "ASUS TUF AX3000" - najkrotszy wyrozniajacy opis."""
    parts = [source.brand or "", *series_words(source.title, source.brand), *model_codes(source.title)]
    return " ".join(dict.fromkeys(p for p in parts if p)).strip()


def find_in_shop(searcher: Searcher, host: str, source: Product) -> ShopResult:
    """Szuka produktu zrodlowego w sklepie; dopasowanie = ten sam EAN."""
    checked: dict[str, Product | None] = {}
    similar: list[tuple[Product, float]] = []
    budget = {"left": MAX_CANDIDATE_PAGES}

    def verify(candidates: list[tuple[str, str]]) -> Product | None:
        ranked = sorted(((candidate_score(source, t), t, u) for t, u in candidates), reverse=True)
        for score, _title, link in ranked[:TOP_PER_QUERY]:
            if link in checked or score < MIN_SIMILARITY or budget["left"] <= 0:
                continue
            budget["left"] -= 1
            found = checked[link] = searcher.product(link)
            if found is None:
                continue
            if source.ean and found.ean == source.ean:
                return found
            similar.append((found, candidate_score(source, found.title)))
        return None

    try:
        for q in queries_for(source):
            match = verify(searcher.search(host, q))
            if match:
                return ShopResult(host, "match", match, note=f"zgodny EAN, zapytanie „{q}”")
        if source.brand or model_codes(source.title):
            q = model_query(source) if model_codes(source.title) else source.title
            budget["left"] = MAX_WEB_CANDIDATE_PAGES
            match = verify(searcher.web_search(host, q))
            if match:
                return ShopResult(host, "match", match, note="zgodny EAN, znaleziony przez DuckDuckGo")
    except scraper.Blocked as exc:
        return ShopResult(host, "blocked", note=f"sklep zablokował wyszukiwanie ({exc})")
    similar.sort(key=lambda x: -x[1])
    if not source.ean:
        return ShopResult(host, "similar" if similar else "none", similar=similar[:3],
                          note="produkt źródłowy nie ma EAN – nie da się potwierdzić dopasowania")
    return ShopResult(host, "similar" if similar else "none", similar=similar[:3])


def find_source_by_name(searcher: Searcher, name: str, hosts: list[str]) -> Product | None:
    """Produkt zrodlowy dla zgloszenia z sama nazwa: najlepiej pasujacy wynik z EAN w ktoryms sklepie."""
    best = None
    for host in hosts:
        try:
            results = searcher.search(host, name)
        except scraper.Blocked:
            continue
        ranked = sorted(((similarity(name, t), u) for t, u in results), reverse=True)
        if ranked and ranked[0][0] >= 0.35:
            found = searcher.product(ranked[0][1])
            if found and found.ean:
                score = similarity(name, found.title)
                if best is None or score > best[0]:
                    best = (score, found)
                if score >= 0.6:
                    break
    return best[1] if best else None


def process(searcher: Searcher, query: str, search_other_shops: bool, cooldowns: dict) -> tuple[Product | None, list[ShopResult]]:
    hosts = list(scraper.SHOPS)
    active = [h for h in hosts if cooldowns.get(h, {}).get("until", 0) <= time.time()]
    if query.startswith("http"):
        host = urlparse(query).hostname
        if host not in scraper.SHOPS:
            raise ValueError(f"nieobsługiwany sklep: {host}")
        url = query.split("?")[0].split("#")[0]
        source = searcher.product(url)
        if source is None:
            raise ValueError("nie udało się odczytać produktu z podanej strony")
    else:
        source = find_source_by_name(searcher, query, active)
        if source is None:
            raise ValueError("nie znaleziono produktu o tej nazwie w żadnym sklepie")

    results = [ShopResult(source.host, "source", source)]
    if search_other_shops:
        for host in hosts:
            if host == source.host:
                continue
            if host not in active:
                results.append(ShopResult(host, "skipped", note="sklep ma przerwę po blokadzie"))
                continue
            results.append(find_in_shop(searcher, host, source))
        retry_with_other_names(searcher, source, results)
    return source, results


def retry_with_other_names(searcher: Searcher, source: Product, results: list[ShopResult]) -> None:
    """Druga runda: nazwy z potwierdzonych dopasowan czesto maja kod modelu, ktorego brak
    w nazwie zrodlowej (MediaMarkt: "Kabel sieciowy HAMA CAT 6 ... 10m", Euro: "... Hama 200925 ...").
    Dla sklepow bez wyniku szukamy jeszcze raz z ta nazwa (ten sam EAN do weryfikacji)."""
    if not source.ean:
        return
    tried = {tuple(model_codes(source.title))}
    for alt in [r.product for r in results if r.status == "match"]:
        codes = tuple(model_codes(alt.title))
        if not codes or codes in tried:
            continue
        tried.add(codes)
        alt_source = Product(source.url, source.host, alt.title, source.ean, source.brand or alt.brand)
        for i, r in enumerate(results):
            if r.status not in ("none", "similar"):
                continue
            retry = find_in_shop(searcher, r.host, alt_source)
            if retry.status == "match":
                retry.note += f" (szukane po nazwie z {shop_name(alt.host)})"
                results[i] = retry


# ---------------- products.csv ----------------

def load_product_rows() -> list[dict]:
    with scraper.PRODUCTS_FILE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def add_rows(name: str, urls: list[str]) -> tuple[str, list[str]]:
    """Dopisuje URL-e pod nazwa produktu (istniejaca nazwa = dolaczenie do grupy). Zwraca (nazwa, dodane)."""
    rows = load_product_rows()
    existing_urls = {r["url"].strip() for r in rows}
    for r in rows:  # ta sama nazwa bez wzgledu na wielkosc liter -> istniejaca grupa
        if r["product"].strip().lower() == name.strip().lower():
            name = r["product"].strip()
            break
    new = [u for u in dict.fromkeys(urls) if u not in existing_urls]
    if new:
        text = scraper.PRODUCTS_FILE.read_text(encoding="utf-8")
        with scraper.PRODUCTS_FILE.open("a", newline="", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            writer = csv.writer(f, lineterminator="\n")
            for u in new:
                writer.writerow([name, u])
    return name, new


def default_name(source: Product) -> str:
    title = re.sub(r"\s+", " ", source.title).strip()
    return title if len(title) <= 80 else title[:79].rstrip() + "…"


def valid_product_url(url: str) -> bool:
    host = urlparse(url).hostname
    return host in PRODUCT_URL and bool(PRODUCT_URL[host].match(url))


# ---------------- wyniki ----------------

def offer_dict(p: Product, status: str, note: str = "") -> dict:
    return {
        "shop": shop_name(p.host), "url": p.url, "title": p.title, "ean": p.ean,
        "price": p.price, "sale_price": p.sale_price, "availability": p.availability,
        "status": status, "note": note,
    }


def results_data(query: str, source: Product, results: list[ShopResult]) -> dict:
    """Wyniki dla strony dodawania: per sklep oferty do wyboru (dopasowane i podobne)."""
    shops = []
    for r in results:
        offers = []
        if r.status in ("source", "match"):
            offers.append(offer_dict(r.product, r.status, r.note))
        offers += [offer_dict(p, "similar") for p, _ in r.similar if not r.product or p.url != r.product.url]
        shops.append({"shop": shop_name(r.host), "status": r.status, "note": r.note, "offers": offers})
    return {
        "query": query,
        "name": default_name(source),
        "source": offer_dict(source, "source"),
        "tracked": [r["url"].strip() for r in load_product_rows()],
        "shops": shops,
    }


def fmt_price(o: dict) -> str:
    if o.get("sale_price"):
        return f"{o['sale_price']} zł (zamiast {o['price']} zł)"
    return f"{o['price']} zł" if o.get("price") else "–"


def search_report(data: dict | None, error: str | None, confirm_url: str | None) -> str:
    """Komentarz z wynikami: tabela dla czlowieka + JSON w ukrytym komentarzu HTML dla strony."""
    lines = ["### Wyniki wyszukiwania", ""]
    if error:
        return "\n".join(lines + [f"❌ **Nie znaleziono produktu:** {error}."])
    src = data["source"]
    lines += [f"Produkt: **{src['title']}** — EAN `{src['ean'] or 'brak'}`", "",
              "| Sklep | Wynik | Oferta | Cena |", "|---|---|---|---|"]
    labels = {"source": "✅ źródło", "match": "✅ zgodny EAN", "similar": "⚠️ podobny – sprawdź"}
    for shop in data["shops"]:
        if not shop["offers"]:
            reason = "⏸️ " + shop["note"] if shop["status"] in ("blocked", "skipped") else "❌ nie znaleziono"
            lines.append(f"| {shop['shop']} | {reason} | | |")
        for o in shop["offers"]:
            lines.append(f"| {shop['shop']} | {labels[o['status']]} | [{o['title']}]({o['url']}) "
                         f"(EAN `{o['ean'] or 'brak'}`) | {fmt_price(o)} |")
    lines += ["", f"👉 **Potwierdź i dodaj do śledzenia:** {confirm_url}" if confirm_url else "",
              "", f"<!-- {RESULTS_MARKER} {json_for_comment(data)} -->"]
    return "\n".join(lines)


def json_for_comment(data: dict) -> str:
    # "--" zamknieloby komentarz HTML - w JSON-ie moze wystapic tylko w tekstach, wiec - jest bezpieczne
    return json.dumps(data, ensure_ascii=False).replace("--", "-\\u002d")


# ---------------- zgloszenia z GitHuba ----------------

def gh(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=input_text).stdout


def field_value(body: str, key: str) -> str:
    m = re.search(rf"^{key}:[ \t]*(.*)$", body or "", re.M)
    return m.group(1).strip() if m else ""


def repo_info() -> tuple[str, str]:
    info = json.loads(gh("repo", "view", "--json", "owner,name"))
    return info["owner"]["login"], info["name"]


def pending_issues(owner: str) -> list[dict]:
    issues = json.loads(gh("issue", "list", "--label", ISSUE_LABEL, "--state", "open",
                           "--json", "number,title,body,author,labels", "--limit", "30"))
    # tylko zgloszenia wlasciciela repozytorium - repo jest publiczne, a zgloszenie
    # kaze tej maszynie otwierac strony i zmieniac products.csv
    return [i for i in issues if i["author"]["login"] == owner]


def handle_search(searcher: Searcher, issue: dict, cooldowns: dict, confirm_url: str) -> None:
    query = field_value(issue["body"], "zapytanie")
    print(f"Wyszukiwanie produktu: {query} (zgłoszenie #{issue['number']})")
    if not query or len(query) > 500:
        error, data = "puste albo za długie zapytanie", None
    else:
        try:
            source, results = process(searcher, query, True, cooldowns)
            error, data = None, results_data(query, source, results)
        except (ValueError, scraper.Blocked) as exc:
            error, data = str(exc), None
    text = search_report(data, error, None if error else f"{confirm_url}#{issue['number']}")
    print(text.split("<!--")[0])
    gh("issue", "comment", str(issue["number"]), "--body-file", "-", input_text=text)
    if error:
        gh("issue", "close", str(issue["number"]), "--reason", "not planned")
    else:
        gh("issue", "edit", str(issue["number"]), "--add-label", RESULTS_LABEL)


def handle_confirm(issue: dict, owner: str) -> bool:
    """Dodaje wybrane linki; zwraca True, gdy products.csv sie zmienil."""
    body = issue["body"]
    ref = field_value(body, "zgloszenie").lstrip("#")
    name = field_value(body, "nazwa")[:120]
    urls = [u.strip() for u in re.findall(r"^url:[ \t]*(\S+)", body, re.M)]
    bad = [u for u in urls if not valid_product_url(u)]
    print(f"Potwierdzenie #{issue['number']} do zgłoszenia #{ref}: {len(urls)} linków")
    if not name or not urls or bad:
        reason = "brak nazwy" if not name else "brak linków" if not urls else "nieobsługiwane linki: " + ", ".join(bad)
        gh("issue", "comment", str(issue["number"]), "--body", f"❌ Nie dodano: {reason}.")
        gh("issue", "close", str(issue["number"]), "--reason", "not planned")
        return False
    final_name, added = add_rows(name, urls)
    lines = [f"✅ Dodano do śledzenia jako **{final_name}**:" if added else f"Wszystkie linki były już śledzone (**{final_name}**)."]
    lines += [f"- {u}" + ("" if u in added else " (już było śledzone)") for u in urls]
    lines += ["", "Ceny pojawią się po najbliższym przebiegu scrapera (do ~30 min)."]
    text = "\n".join(lines)
    gh("issue", "comment", str(issue["number"]), "--body-file", "-", input_text=text)
    gh("issue", "close", str(issue["number"]), "--reason", "completed")
    if ref.isdigit():
        original = json.loads(gh("issue", "view", ref, "--json", "author,state"))
        if original["author"]["login"] == owner and original["state"] == "OPEN":
            gh("issue", "comment", ref, "--body-file", "-", input_text=text + f"\n\nPotwierdzenie: #{issue['number']}")
            gh("issue", "close", ref, "--reason", "completed")
    return bool(added)


def run_issues() -> None:
    owner, repo = repo_info()
    issues = pending_issues(owner)
    labels = lambda i: {l["name"] for l in i["labels"]}
    confirms = [i for i in issues if CONFIRM_MARKER in (i["body"] or "")]
    searches = [i for i in issues if SEARCH_MARKER in (i["body"] or "") and RESULTS_LABEL not in labels(i)]
    for issue in confirms:
        handle_confirm(issue, owner)
    if not searches:
        return
    confirm_url = f"https://{owner}.github.io/{repo}/dodaj.html"
    state = scraper.load_state()
    cooldowns = state.setdefault("cooldowns", {})
    with scraper.open_browser(state) as page:
        searcher = Searcher(page)
        for issue in searches:
            handle_search(searcher, issue, cooldowns, confirm_url)
    scraper.save_state(state)


def run_query(query: str, add: bool, name: str) -> None:
    state = scraper.load_state()
    with scraper.open_browser(state) as page:
        try:
            source, results = process(Searcher(page), query, True, state.setdefault("cooldowns", {}))
        except (ValueError, scraper.Blocked) as exc:
            print(search_report(None, str(exc), None))
            return
    scraper.save_state(state)
    data = results_data(query, source, results)
    print(search_report(data, None, None).split("<!--")[0])
    if add:
        urls = [r.product.url for r in results if r.status in ("source", "match")]
        final_name, added = add_rows(name or data["name"], urls)
        print(f"Dodano {len(added)} linków jako: {final_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", help="link do produktu albo nazwa (zamiast zgłoszeń z GitHuba)")
    parser.add_argument("--add", action="store_true", help="z --query: od razu dodaj oferty ze zgodnym EAN")
    parser.add_argument("--name", default="", help="z --add: nazwa produktu w products.csv")
    args = parser.parse_args()
    if args.query:
        run_query(args.query, args.add, args.name)
    else:
        run_issues()


if __name__ == "__main__":
    main()
