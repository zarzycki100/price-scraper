#!/usr/bin/env python3
"""
Dodawanie produktow do products.csv z wyszukiwaniem tego samego produktu w innych sklepach.

Zgloszenia przychodza jako issues na GitHubie z etykieta "dodaj-produkt" (tworzy je
ukryta strona docs/dodaj.html). Skrypt uruchamiany z crona przed scraperem:
  1. czyta otwarte zgloszenia autora repozytorium (gh issue list),
  2. ustala produkt zrodlowy - z linku do sklepu albo wyszukujac nazwe w sklepach,
  3. w pozostalych sklepach szuka kandydatow (wyszukiwarka sklepu kilkoma zapytaniami,
     na koniec DuckDuckGo "site:sklep") i otwiera najlepszych,
  4. dodaje do products.csv oferty ze zgodnym EAN (gtin13 z JSON-LD strony produktu),
     podobne bez zgodnego EAN tylko opisuje w raporcie,
  5. komentuje i zamyka zgloszenie.

Reczne uruchomienie (test, bez GitHuba):
  python3 add_products.py --query "https://www.euro.com.pl/..." --dry-run
  python3 add_products.py --query "Hama 200925 10 m" --name "Kabel Hama 10 m"
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
ISSUE_MARKER = "<!-- price-scraper:dodaj -->"

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
        return identify(html, url)

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


def add_rows(name: str, urls: list[str], dry_run: bool) -> tuple[str, list[str]]:
    """Dopisuje URL-e pod nazwa produktu (istniejaca nazwa = dolaczenie do grupy). Zwraca (nazwa, dodane)."""
    rows = load_product_rows()
    existing_urls = {r["url"].strip() for r in rows}
    for r in rows:  # ta sama nazwa bez wzgledu na wielkosc liter -> istniejaca grupa
        if r["product"].strip().lower() == name.strip().lower():
            name = r["product"].strip()
            break
    new = [u for u in dict.fromkeys(urls) if u not in existing_urls]
    if new and not dry_run:
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


# ---------------- raport ----------------

def report(query: str, source: Product | None, results: list[ShopResult], name: str | None, added: list[str],
           error: str | None, dry_run: bool = False) -> str:
    lines = ["### Wynik dodawania produktu" + (" (próba – nic nie zapisano)" if dry_run else ""), "", f"Zapytanie: `{query}`", ""]
    if error:
        lines += [f"❌ **Nie dodano:** {error}."]
        return "\n".join(lines)
    lines += [f"Produkt źródłowy: [{source.title}]({source.url}) — EAN: `{source.ean or 'brak'}`", "",
              "| Sklep | Wynik | Oferta |", "|---|---|---|"]
    for r in results:
        shop = shop_name(r.host)
        if r.status == "source":
            status = "✅ źródło" + (" (dodano)" if r.product.url in added else " (już było śledzone)")
            lines.append(f"| {shop} | {status} | [{r.product.title}]({r.product.url}) |")
        elif r.status == "match":
            status = "✅ dodano" if r.product.url in added else "✅ znaleziono (już było śledzone)"
            lines.append(f"| {shop} | {status} – {r.note} | [{r.product.title}]({r.product.url}) |")
        elif r.status == "similar":
            links = "<br>".join(f"[{p.title}]({p.url}) (EAN `{p.ean or 'brak'}`)" for p, _ in r.similar)
            lines.append(f"| {shop} | ⚠️ tylko podobne, nie dodano{' – ' + r.note if r.note else ''} | {links} |")
        elif r.status == "none":
            lines.append(f"| {shop} | ❌ nie znaleziono{' – ' + r.note if r.note else ''} | |")
        else:
            lines.append(f"| {shop} | ⏸️ pominięto – {r.note} | |")
    lines.append("")
    if added:
        lines.append(f"Dodano {len(added)} {'ofertę' if len(added) == 1 else 'oferty' if len(added) < 5 else 'ofert'} "
                     f"do `products.csv` jako **{name}**. Ceny pojawią się po najbliższym przebiegu scrapera.")
    else:
        lines.append("Nic nowego nie dodano.")
    if any(r.status == "similar" for r in results):
        lines.append("\nPodobne oferty bez zgodnego EAN można dodać ręcznie: na stronie dodawania wklej link, "
                     f"wpisz nazwę **{name or ''}** i odznacz wyszukiwanie w innych sklepach.")
    return "\n".join(lines)


# ---------------- zgloszenia z GitHuba ----------------

def gh(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=input_text).stdout


def parse_issue(body: str) -> dict | None:
    if ISSUE_MARKER not in (body or ""):
        return None
    fields = {}
    for key in ("zapytanie", "nazwa", "szukaj"):
        m = re.search(rf"^{key}:[ \t]*(.*)$", body, re.M)
        fields[key] = m.group(1).strip() if m else ""
    if not fields["zapytanie"] or len(fields["zapytanie"]) > 500 or len(fields["nazwa"]) > 120:
        return None
    return fields


def pending_issues() -> list[dict]:
    owner = json.loads(gh("repo", "view", "--json", "owner"))["owner"]["login"]
    issues = json.loads(gh("issue", "list", "--label", ISSUE_LABEL, "--state", "open",
                           "--json", "number,title,body,author", "--limit", "20"))
    # tylko zgloszenia wlasciciela repozytorium - repo jest publiczne, a zgloszenie
    # kaze tej maszynie otwierac strony
    return [i for i in issues if i["author"]["login"] == owner]


def handle(searcher: Searcher, query: str, name: str, search: bool, cooldowns: dict, dry_run: bool) -> tuple[str, bool]:
    try:
        source, results = process(searcher, query, search, cooldowns)
    except (ValueError, scraper.Blocked) as exc:
        return report(query, None, [], None, [], str(exc), dry_run), False
    urls = [r.product.url for r in results if r.status in ("source", "match")]
    final_name, added = add_rows(name or default_name(source), urls, dry_run)
    return report(query, source, results, final_name, added, None, dry_run), bool(added)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", help="link do produktu albo nazwa (zamiast zgłoszeń z GitHuba)")
    parser.add_argument("--name", default="", help="nazwa produktu w products.csv")
    parser.add_argument("--no-search", action="store_true", help="nie szukaj w innych sklepach")
    parser.add_argument("--dry-run", action="store_true", help="nie zapisuj products.csv i nie zmieniaj zgłoszeń")
    args = parser.parse_args()

    if args.query:
        jobs = [(None, {"zapytanie": args.query, "nazwa": args.name, "szukaj": "nie" if args.no_search else "tak"})]
    else:
        jobs = [(i["number"], f) for i in pending_issues() if (f := parse_issue(i["body"]))]
        if not jobs:
            return
    state = scraper.load_state()
    cooldowns = state.setdefault("cooldowns", {})
    with scraper.open_browser(state) as page:
        searcher = Searcher(page)
        for number, f in jobs:
            print(f"Dodawanie produktu: {f['zapytanie']}" + (f" (zgłoszenie #{number})" if number else ""))
            text, added = handle(searcher, f["zapytanie"], f["nazwa"], f["szukaj"] != "nie", cooldowns, args.dry_run)
            print(text)
            if number and not args.dry_run:
                gh("issue", "comment", str(number), "--body-file", "-", input_text=text)
                gh("issue", "close", str(number), "--reason", "completed" if added else "not planned")
    scraper.save_state(state)


if __name__ == "__main__":
    main()
