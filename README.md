# Monitor cen Media Expert i RTV Euro AGD

Automatyczny scraper cen produktów z mediaexpert.pl i euro.com.pl, uruchamiany co godzinę
przez crona na lokalnym komputerze, z historią zapisywaną do CSV i wizualizacją na
GitHub Pages.

## Jak uruchomić

1. **Stwórz nowe repozytorium na GitHub** i wgraj do niego wszystkie pliki
   z tego projektu (zachowując strukturę katalogów).

2. **Dodaj produkty do śledzenia** — edytuj `products.txt`, jeden URL
   produktu z mediaexpert.pl lub euro.com.pl na linię (sklep jest
   rozpoznawany po domenie).

3. **Włącz uprawnienia do zapisu dla Actions:**
   Settings → Actions → General → Workflow permissions →
   „Read and write permissions” → Save.
   (Bez tego workflow nie będzie mógł commitować `data/prices.csv`.)

4. **Włącz GitHub Pages:**
   Settings → Pages → Source: „Deploy from a branch” →
   Branch: `main`, folder: `/docs` → Save.
   Strona z wykresem będzie dostępna pod
   `https://<twoj-login>.github.io/<nazwa-repo>/`.

5. **Odpal workflow ręcznie pierwszy raz**, żeby sprawdzić, czy działa:
   zakładka „Actions” → „Scrape prices” → „Run workflow”.

6. **Dodaj lokalnego crona** (sklepy blokują adresy IP GitHub Actions,
   więc cykliczne pobieranie działa z Twojego komputera):
   ```
   gh auth login        # jednorazowo - cron pushuje przez token gh (HTTPS)
   pip install --user -r requirements.txt   # Ubuntu 24+: dodaj --break-system-packages
   sudo apt install xvfb                    # wirtualny ekran dla przegladarki
   # wymagany tez zainstalowany Google Chrome (/usr/bin/google-chrome)
   crontab -e
   # dopisz:
   17 * * * * /sciezka/do/repo/scripts/cron_scrape.sh
   ```
   Skrypt pracuje na osobnym klonie repo (`~/.local/share/price-scraper-cron`),
   więc nie rusza Twojej kopii roboczej. Dopisuje wiersze do
   `data/prices.csv`, commituje i pushuje je do repo.
   Log: `~/.local/state/price-scraper/cron.log`.
   Strony pobiera Google Chrome sterowany przez Playwright, w trybie z oknem
   na wirtualnym ekranie Xvfb (nic nie pojawia się na pulpicie) — sklepy
   chronią się bot-managerami (Cloudflare, Akamai), które wykonują
   w przeglądarce JavaScript i rozpoznają tryb headless.
   Stan scrapera (przerwy po blokadach) jest w `~/.local/state/price-scraper/state.json`,
   a profil przeglądarki z cookies w `~/.local/state/price-scraper/browser-profile/` —
   usuń oba, żeby zacząć od zera.
   Strona na GitHub Pages odczytuje `data/prices.csv` na żywo i ma dwa widoki:
   - **Produkt** — historia ceny regularnej i promocyjnej jednego produktu
     (lista pogrupowana po sklepach, z oznaczeniem sklepu i linkiem),
   - **Porównanie w sklepie** (`#porownanie` w adresie) — ceny wszystkich
     produktów z wybranego sklepu na jednym wykresie, z legendą-tabelą pod
     wykresem (nazwy z linkami, cena teraz / najniższa / najwyższa,
     ukrywanie pojedynczych linii).

## Dodawanie kolejnego sklepu

W `scraper.py` dopisz funkcję `parse_<sklep>(soup, html)` zwracającą
`title`, `part_no`, `price`, `sale_price`, `availability` i dodaj domenę do
słownika `SHOPS`. Na stronie warto dopisać domenę do `SHOP_BY_HOST`
w `docs/index.html` (używane tylko dla starych wierszy bez kolumny `shop`).

## Struktura plików

```
scraper.py                     - skrypt scrapujący (Playwright/Chrome + BeautifulSoup)
products.txt                   - lista URL-i produktów do monitorowania
requirements.txt               - zależności Pythona
data/prices.csv                - historia cen (tworzona automatycznie)
docs/index.html                - strona z wykresem (Chart.js) dla GitHub Pages
scripts/cron_scrape.sh         - uruchamianie z lokalnego crona (co 1 h)
.github/workflows/scrape.yml   - ręczne uruchomienie w GitHub Actions
```

## Uwagi

- Z runnerów GitHub Actions oba sklepy odpowiadają zwykle **403** — blokują
  adresy IP centrów danych, nie sam kod. Dlatego harmonogram w Actions jest
  wyłączony, a workflow zostaje tylko do ręcznego uruchamiania.
- Cron działa tylko, gdy komputer jest włączony (i nie uśpiony) — przerwy
  będą widoczne jako luki na wykresie.
- Media Expert stoi za Cloudflare, a Euro za Akamai Bot Manager. Oba oceniają
  nie tylko odcisk TLS/HTTP2, ale też wykonują w przeglądarce JavaScript,
  dlatego scraper używa prawdziwego Google Chrome (Playwright + Xvfb) zamiast
  zwykłych zapytań HTTP ani headless Chromium, które Akamai rozpoznaje. Zbyt częste odpytywanie i tak kończy się blokadą **adresu IP**
  (Euro: strona „RTV EURO AGD - Blokada” z Twoim IP) — wtedy nie pomaga
  żadna zmiana w kodzie, trzeba przeczekać. Scraper sam robi wtedy przerwy
  dla sklepu (1 h, 2 h, 4 h … do 12 h).
- Kolumna `shop` w CSV została dodana później — przy pierwszym uruchomieniu
  scraper sam przepisze istniejący `data/prices.csv` do nowego układu kolumn,
  uzupełniając sklep na podstawie URL-a.
- Dane w CSV rosną w nieskończoność. Przy bardzo długim monitorowaniu warto
  rozważyć okresowe archiwizowanie starszych wpisów.
