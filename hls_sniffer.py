#!/usr/bin/env python3
"""
HLS Sniffer - Estrae flussi HLS (m3u8) da pagine web
Supporta pagine con contenuto dinamico (JavaScript, iframe, player video).
"""

import re
import sys
from urllib.parse import unquote, urljoin, urlparse

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ─── Pattern per trovare URL m3u8 ────────────────────────────────────────────

M3U8_PATTERN = re.compile(
    r'https?://[^\s\'"<>\\]+\.m3u8(?:[^\s\'"<>\\]*)?',
    re.IGNORECASE
)

STREAM_ATTR_PATTERN = re.compile(
    r'(?:src|file|source|url|stream|hls)["\s]*[:=]["\s]*["\']?(https?://[^\s\'"<>\\]+\.m3u8[^\s\'"<>\\]*)',
    re.IGNORECASE
)

# Pattern per URL relativi / ofuscati
RELATIVE_M3U8_PATTERN = re.compile(
    r'["\']([^"\']+\.m3u8(?:[^\s\'"<>\\]*)?)["\']',
    re.IGNORECASE
)

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/122.0.0.0 Safari/537.36'
)


# ─── Utility ──────────────────────────────────────────────────────────────────

def find_m3u8_in_text(text, base_url=None):
    """Trova tutti gli URL m3u8 in un testo (HTML, JS, ecc.)."""
    found = set()

    # URL assoluti diretti
    found.update(M3U8_PATTERN.findall(text))

    # Attributi src/file/source/url/stream
    for m in STREAM_ATTR_PATTERN.finditer(text):
        found.add(m.group(1))

    # URL relativi (es. "/live/stream.m3u8")
    if base_url:
        for m in RELATIVE_M3U8_PATTERN.finditer(text):
            candidate = m.group(1)
            if not candidate.startswith('http'):
                full = urljoin(base_url, candidate)
                found.add(full)

    # Rimuove eventuali artefatti di escape
    cleaned = set()
    for url in found:
        url = url.replace('\\/', '/').rstrip('\\')
        cleaned.add(url)

    return cleaned


def print_section(title):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print('─' * 50)


def _default_referrer(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _build_headers(referrer=None):
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': '*/*',
        'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8',
    }
    if referrer:
        headers['Referer'] = referrer
    return headers


def _looks_like_hls_url(url):
    lowered = unquote(url.lower())
    return '.m3u8' in lowered or 'm3u8' in lowered


def _is_http_url(url):
    lowered = url.lower()
    return lowered.startswith('http://') or lowered.startswith('https://')


SCRIPT_SRC_PATTERN = re.compile(
    r'<script[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE
)

IFRAME_SRC_PATTERN = re.compile(
    r'<iframe[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE
)


def sniff_with_requests(url, referrer=None):
    """
    Scan HTTP puro: scarica HTML + JS + iframe e cerca URL m3u8 nel testo.
    Non esegue JavaScript, ma spesso trova stream presenti nei sorgenti.
    """
    found = set()

    if not REQUESTS_AVAILABLE:
        print("  ✗ requests non installato.")
        print("    Installa con: pip install requests")
        return found

    if not referrer:
        referrer = _default_referrer(url)

    headers = _build_headers(referrer)
    session = requests.Session()
    visited = set()

    def fetch_text(resource_url):
        if resource_url in visited:
            return ''
        visited.add(resource_url)

        try:
            resp = session.get(resource_url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', '').lower()
            if not any(t in content_type for t in ('text', 'javascript', 'json', 'xml', 'm3u8')):
                return ''
            return resp.text
        except Exception:
            return ''

    print("  → Scan HTTP (requests): HTML + script + iframe")

    html = fetch_text(url)
    if not html:
        return found

    found.update(find_m3u8_in_text(html, base_url=url))

    for m in SCRIPT_SRC_PATTERN.finditer(html):
        script_url = urljoin(url, m.group(1))
        js_text = fetch_text(script_url)
        if js_text:
            found.update(find_m3u8_in_text(js_text, base_url=script_url))

    for m in IFRAME_SRC_PATTERN.finditer(html):
        iframe_url = urljoin(url, m.group(1))
        iframe_html = fetch_text(iframe_url)
        if iframe_html:
            found.update(find_m3u8_in_text(iframe_html, base_url=iframe_url))

    return found


# ─── Browser headless (Playwright) ───────────────────────────────────────────

def _make_context(browser):
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8'},
    )
    return ctx


STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['it-IT','it','en-US','en']});
window.chrome = {runtime: {}};
"""


def sniff_with_playwright(url, referrer=None):
    """
    Usa Playwright (Chromium headless) per intercettare le richieste di rete
    mentre la pagina si carica — come fa un'estensione Chrome.
    """
    found = set()

    if not PLAYWRIGHT_AVAILABLE:
        print("  ✗ Playwright non installato.")
        print("    Installa con:  pip install playwright && playwright install chromium")
        return found

    # Usa la homepage del dominio come referer se non specificato
    if not referrer:
        referrer = _default_referrer(url)

    print(f"  → Avvio Chromium headless...")
    print(f"  → Referer: {referrer}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        ctx = _make_context(browser)
        page = ctx.new_page()
        page.add_init_script(STEALTH_SCRIPT)

        def on_request(req):
            if _is_http_url(req.url) and _looks_like_hls_url(req.url):
                print(f"  ★ {req.url}")
                found.add(req.url)

        def on_response(resp):
            content_type = resp.headers.get('content-type', '').lower()
            is_hls_type = (
                'application/vnd.apple.mpegurl' in content_type
                or 'application/x-mpegurl' in content_type
            )

            if _is_http_url(resp.url) and (_looks_like_hls_url(resp.url) or is_hls_type):
                found.add(resp.url)
                return

            # Alcuni CDN non usano estensione .m3u8 nell'URL: riconosci il manifest dal body.
            if 'text' in content_type or 'json' in content_type or not content_type:
                try:
                    body = resp.text()
                    if '#EXTM3U' in body and _is_http_url(resp.url):
                        print(f"  ★ {resp.url}  [manifest detected]")
                        found.add(resp.url)
                except Exception:
                    pass

        page.on('request', on_request)
        page.on('response', on_response)

        try:
            page.goto(url, wait_until='networkidle', timeout=30000, referer=referrer)
        except Exception:
            pass  # timeout networkidle — continua comunque

        # Scansiona anche l'HTML renderizzato
        try:
            for u in find_m3u8_in_text(page.content(), url):
                found.add(u)
        except Exception:
            pass

        # Attesa extra per player lenti
        if not found:
            print("  → Attendo 10s per player lenti...")
            try:
                page.wait_for_timeout(10000)
                for u in find_m3u8_in_text(page.content(), url):
                    found.add(u)
            except Exception:
                pass

        ctx.close()
        browser.close()

    return found


# ─── Funzione principale ───────────────────────────────────────────────────────

def sniff(url, referrer=None):
    print(f"\n  URL: {url}")
    if referrer:
        print(f"  Referer: {referrer}")

    print_section("Scan HTTP (requests)")
    streams_requests = sniff_with_requests(url, referrer=referrer)
    if streams_requests:
        print(f"\n  ✓ requests: trovati {len(streams_requests)} stream(s).")
    else:
        print("\n  requests: nessuno stream trovato.")

    print_section("Scan browser (Playwright/Chromium)")
    streams_browser = sniff_with_playwright(url, referrer=referrer)
    if streams_browser:
        print(f"\n  ✓ Playwright: trovati {len(streams_browser)} stream(s).")
    else:
        print("\n  Playwright: nessuno stream trovato.")

    streams = streams_requests | streams_browser
    if streams:
        print(f"\n  ✓ Totale unificato: {len(streams)} stream(s).")
    else:
        print("\n  Nessuno stream trovato.")

    return streams


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Utilizzo:")
        print("  python hls_sniffer.py <url>")
        print("  python hls_sniffer.py <url> --referer <referer_url>")
        print("\nEsempio:")
        print("  python hls_sniffer.py https://dlstreams.top/player/stream-576.php")
        sys.exit(1)

    target_url = sys.argv[1]
    referrer = None

    # Opzione --referer
    if '--referer' in sys.argv:
        idx = sys.argv.index('--referer')
        if idx + 1 < len(sys.argv):
            referrer = sys.argv[idx + 1]

    print("=" * 50)
    print("          HLS SNIFFER")
    print("=" * 50)

    streams = sniff(target_url, referrer=referrer)

    print("\n" + "=" * 50)
    if streams:
        print(f"  FLUSSI HLS TROVATI ({len(streams)})")
        print("=" * 50)
        for i, stream_url in enumerate(sorted(streams), 1):
            print(f"  {i}. {stream_url}")
    else:
        print("  NESSUN FLUSSO HLS TROVATO")
        print("=" * 50)
        print("\n  Possibili motivi:")
        print("  • Il flusso è caricato tramite WebSocket (non intercettabile con questo tool)")
        print("  • Il player usa DRM / cifratura")
        print("  • La pagina richiede un referer specifico  →  usa --referer <url>")
        print("  • Il player si avvia solo dopo un click    →  prova aprirlo manualmente in Chrome")
        print("    e cattura le richieste di rete con DevTools → Network → filtra 'm3u8'")

    print("=" * 50)


if __name__ == '__main__':
    main()
