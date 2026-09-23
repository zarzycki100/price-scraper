# Monitor cen Media Expert i RTV Euro AGD

Automatyczny scraper cen produktów z mediaexpert.pl i euro.com.pl, uruchamiany co 15 minut
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
   pip install --user -r requirements.txt
   crontab -e
   # dopisz:
   */15 * * * * /sciezka/do/repo/scripts/cron_scrape.sh
   ```
   Skrypt pracuje na osobnym klonie repo (`~/.local/share/price-scraper-cron`),
   więc nie rusza Twojej kopii roboczej. Dopisuje wiersze do
   `data/prices.csv`, commituje i pushuje je do repo.
   Log: `~/.local/state/price-scraper/cron.log`.
   Strona na GitHub Pages odczytuje ten plik na żywo i ma dwa widoki:
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
scraper.py                     - skrypt scrapujący (curl_cffi + BeautifulSoup)
products.txt                   - lista URL-i produktów do monitorowania
requirements.txt               - zależności Pythona
data/prices.csv                - historia cen (tworzona automatycznie)
docs/index.html                - strona z wykresem (Chart.js) dla GitHub Pages
scripts/cron_scrape.sh         - uruchamianie z lokalnego crona (co 15 min)
.github/workflows/scrape.yml   - ręczne uruchomienie w GitHub Actions
```

## Uwagi

- Z runnerów GitHub Actions oba sklepy odpowiadają zwykle **403** — blokują
  adresy IP centrów danych, nie sam kod. Dlatego harmonogram w Actions jest
  wyłączony, a workflow zostaje tylko do ręcznego uruchamiania.
- Cron działa tylko, gdy komputer jest włączony (i nie uśpiony) — przerwy
  będą widoczne jako luki na wykresie.
- Media Expert stoi za Cloudflare, który zwykłe `requests` odrzuca kodem 403
  (`cf-mitigated: challenge`). Dlatego skrypt używa `curl_cffi`, który
  podszywa się pod przeglądarkę Chrome (odcisk TLS/HTTP2). Jeśli 403 wróci,
  można spróbować innej wartości `IMPERSONATE` w `scraper.py`
  (np. `"safari"`, `"firefox"`) albo zaktualizować `curl_cffi`.
- Kolumna `shop` w CSV została dodana później — przy pierwszym uruchomieniu
  scraper sam przepisze istniejący `data/prices.csv` do nowego układu kolumn,
  uzupełniając sklep na podstawie URL-a.
- Dane w CSV rosną w nieskończoność. Przy bardzo długim monitorowaniu warto
  rozważyć okresowe archiwizowanie starszych wpisów.
