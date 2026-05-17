#!/usr/bin/env python3
"""
Servizio locale: estrae stream HLS a intervalli regolari e sincronizza un JSON su GitHub.

Workflow:
1) Legge una lista di pagine player da monitorare.
2) Esegue sniffing HLS (Playwright by default).
3) Confronta il JSON locale con quello remoto su GitHub.
4) Fa commit/push via GitHub API solo se ci sono differenze.
5) Ripete ogni N minuti.
"""

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from hls_sniffer import sniff

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_INTERVAL_SECONDS = 10 * 60

IFRAME_SRC_PATTERN = re.compile(
    r'<iframe[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass
class Config:
    github_token: str
    github_repo: str
    github_branch: str
    github_json_path: str
    monitor_urls_file: str
    interval_seconds: int
    request_timeout_seconds: int


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _now_human() -> str:
    return datetime.now().astimezone().strftime("%d/%m/%Y %H:%M:%S %Z (%z)")


def _log(message: str) -> None:
    print(f"[{_now_human()}] {message}", flush=True)


def _load_env_file(path: str) -> bool:
    if not os.path.exists(path):
        return False

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

    return True


def _load_config() -> Config:
    # Priorita: variabili gia esportate > .env > .env.example
    _load_env_file(".env")
    _load_env_file(".env.example")

    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    github_repo = os.getenv("GITHUB_REPO", "").strip()
    github_branch = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
    github_json_path = os.getenv("GITHUB_JSON_PATH", "data/hls_streams.json").strip() or "data/hls_streams.json"
    monitor_urls_file = os.getenv("MONITOR_URLS_FILE", "monitor_urls.json").strip() or "monitor_urls.json"

    interval_seconds_raw = os.getenv("SYNC_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)).strip()
    timeout_raw = os.getenv("GITHUB_REQUEST_TIMEOUT_SECONDS", "30").strip()

    try:
        interval_seconds = max(60, int(interval_seconds_raw))
    except ValueError:
        interval_seconds = DEFAULT_INTERVAL_SECONDS

    try:
        request_timeout_seconds = max(5, int(timeout_raw))
    except ValueError:
        request_timeout_seconds = 30

    if not github_token:
        raise ValueError("Variabile mancante: GITHUB_TOKEN")
    if github_token == "ghp_xxx":
        raise ValueError("GITHUB_TOKEN non configurato: valore placeholder rilevato (ghp_xxx)")
    if not github_repo or "/" not in github_repo:
        raise ValueError("Variabile non valida: GITHUB_REPO (formato owner/repo)")
    if github_repo == "owner/repo":
        raise ValueError("GITHUB_REPO non configurato: valore placeholder rilevato (owner/repo)")

    return Config(
        github_token=github_token,
        github_repo=github_repo,
        github_branch=github_branch,
        github_json_path=github_json_path,
        monitor_urls_file=monitor_urls_file,
        interval_seconds=interval_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


def _load_targets(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        raise ValueError(f"{path}: deve essere una lista JSON")

    targets: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            targets.append({"url": item, "referer": None})
            continue

        if isinstance(item, dict) and isinstance(item.get("url"), str):
            targets.append(
                {
                    "url": item["url"],
                    "referer": item.get("referer"),
                    "player_index": item.get("player_index"),
                }
            )
            continue

        raise ValueError(f"Target non valido in {path}: {item}")

    return targets


def _default_referrer(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _fetch_html(url: str, referer: Optional[str], request_timeout_seconds: int) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    resp = requests.get(url, headers=headers, timeout=request_timeout_seconds)
    resp.raise_for_status()
    return resp.text


def _extract_iframe_urls(html: str, base_url: str) -> List[str]:
    iframe_urls: List[str] = []
    for match in IFRAME_SRC_PATTERN.finditer(html):
        iframe_urls.append(urljoin(base_url, match.group(1)))
    return iframe_urls


def _dedupe_preserve_order(urls: List[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _resolve_player_url(target_url: str, referer: Optional[str], player_index: Optional[int], request_timeout_seconds: int) -> str:
    if not player_index or player_index <= 1:
        return target_url

    request_referer = referer or _default_referrer(target_url)

    try:
        html = _fetch_html(target_url, request_referer, request_timeout_seconds)
        iframe_urls = _extract_iframe_urls(html, target_url)
        if len(iframe_urls) >= player_index:
            return iframe_urls[player_index - 1]
    except Exception:
        pass

    if not PLAYWRIGHT_AVAILABLE:
        raise ValueError(f"Impossibile risolvere il player {player_index} da {target_url}: Playwright non disponibile")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=request_timeout_seconds * 1000, referer=request_referer)
            try:
                page.wait_for_load_state("networkidle", timeout=min(request_timeout_seconds * 1000, 8000))
            except Exception:
                pass
            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass

            iframe_urls: List[str] = []
            try:
                iframe_urls.extend(_extract_iframe_urls(page.content(), target_url))
            except Exception:
                pass

            try:
                frame_urls = [frame.url for frame in page.frames if frame.url and frame.url != target_url]
                iframe_urls.extend(frame_urls)
            except Exception:
                pass

            iframe_urls = _dedupe_preserve_order([urljoin(target_url, iframe_url) for iframe_url in iframe_urls])
            if len(iframe_urls) >= player_index:
                return urljoin(target_url, iframe_urls[player_index - 1])
        finally:
            browser.close()

    raise ValueError(f"Impossibile risolvere il player {player_index} da {target_url}")


def _collect_local_snapshot(targets: List[Dict[str, Any]], request_timeout_seconds: int) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []

    for idx, target in enumerate(targets, start=1):
        url = str(target["url"]).strip()
        referer = target.get("referer")
        if referer:
            referer = str(referer).strip()
        player_index_raw = target.get("player_index")
        player_index = None
        if player_index_raw is not None:
            try:
                player_index = int(player_index_raw)
            except (TypeError, ValueError):
                player_index = None

        _log(f"[{idx}/{len(targets)}] Scan: {url}")
        started_at = time.time()

        try:
            scan_candidates: List[Tuple[str, Optional[str]]] = []
            scan_candidates.append((url, referer))

            if player_index and player_index > 1:
                try:
                    resolved_url = _resolve_player_url(url, referer, player_index, request_timeout_seconds)
                    if resolved_url != url:
                        scan_candidates.append((resolved_url, url))
                except Exception as exc:
                    _log(f"  → Fallback player {player_index} non risolto: {exc}")

            streams = set()
            metadata: Dict[str, Dict[str, Any]] = {}
            resolved_url = url

            for candidate_url, candidate_referrer in scan_candidates:
                candidate_streams, candidate_metadata = sniff(
                    candidate_url,
                    referrer=candidate_referrer,
                    skip_requests=True,
                    include_metadata=True,
                )
                if candidate_streams:
                    streams = candidate_streams
                    metadata = candidate_metadata
                    resolved_url = candidate_url
                    break

                if not streams:
                    resolved_url = candidate_url

            duration_seconds = round(time.time() - started_at, 2)

            details = []
            for stream_url in sorted(streams):
                m = metadata.get(stream_url, {}) if isinstance(metadata, dict) else {}
                details.append(
                    {
                        "url": stream_url,
                        "referer": m.get("referer"),
                        "origin": m.get("origin"),
                        "user_agent": m.get("user_agent"),
                    }
                )

            records.append(
                {
                    "source_url": url,
                    "source_referer": referer,
                    "resolved_url": resolved_url,
                    "player_index": player_index,
                    "status": "ok",
                    "duration_seconds": duration_seconds,
                    "streams_count": len(details),
                    "streams": details,
                }
            )
        except Exception as exc:
            duration_seconds = round(time.time() - started_at, 2)
            records.append(
                {
                    "source_url": url,
                    "source_referer": referer,
                    "resolved_url": url,
                    "player_index": player_index,
                    "status": "error",
                    "duration_seconds": duration_seconds,
                    "error": str(exc),
                    "streams_count": 0,
                    "streams": [],
                }
            )

    return {
        "generated_at": _now_iso(),
        "total_sources": len(records),
        "results": records,
    }


def _canonical_for_compare(payload: Dict[str, Any]) -> Dict[str, Any]:
    canonical = {
        "total_sources": payload.get("total_sources", 0),
        "results": [],
    }

    for row in payload.get("results", []):
        streams = sorted(
            [
                {
                    "url": s.get("url"),
                    "referer": s.get("referer"),
                    "origin": s.get("origin"),
                    "user_agent": s.get("user_agent"),
                }
                for s in row.get("streams", [])
            ],
            key=lambda x: (x.get("url") or ""),
        )

        canonical["results"].append(
            {
                "source_url": row.get("source_url"),
                "source_referer": row.get("source_referer"),
                "status": row.get("status"),
                "streams": streams,
            }
        )

    canonical["results"] = sorted(canonical["results"], key=lambda r: r.get("source_url") or "")
    return canonical


def _count_streams(payload: Dict[str, Any]) -> int:
    total = 0
    for row in payload.get("results", []):
        streams = row.get("streams", [])
        if isinstance(streams, list):
            total += len(streams)
    return total


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_remote_file(config: Config) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{GITHUB_API_BASE}/repos/{config.github_repo}/contents/{config.github_json_path}"
    params = {"ref": config.github_branch}

    resp = requests.get(
        url,
        params=params,
        headers=_github_headers(config.github_token),
        timeout=config.request_timeout_seconds,
    )

    if resp.status_code == 404:
        return None, None

    resp.raise_for_status()
    data = resp.json()

    encoded = data.get("content", "")
    sha = data.get("sha")
    if not encoded:
        return None, sha

    decoded = base64.b64decode(encoded).decode("utf-8")
    return json.loads(decoded), sha


def _upsert_remote_file(config: Config, payload: Dict[str, Any], previous_sha: Optional[str]) -> None:
    url = f"{GITHUB_API_BASE}/repos/{config.github_repo}/contents/{config.github_json_path}"
    raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")

    body: Dict[str, Any] = {
        "message": f"chore(hls): refresh streams {_now_human()}",
        "content": encoded,
        "branch": config.github_branch,
    }
    if previous_sha:
        body["sha"] = previous_sha

    resp = requests.put(
        url,
        headers=_github_headers(config.github_token),
        json=body,
        timeout=config.request_timeout_seconds,
    )
    resp.raise_for_status()


def _run_once(config: Config) -> None:
    _log("\n" + "=" * 70)
    _log("Inizio sync HLS")
    _log("=" * 70)

    targets = _load_targets(config.monitor_urls_file)
    local_payload = _collect_local_snapshot(targets, config.request_timeout_seconds)

    remote_payload, remote_sha = _fetch_remote_file(config)

    local_cmp = _canonical_for_compare(local_payload)
    remote_cmp = _canonical_for_compare(remote_payload or {"results": []})
    local_streams_total = _count_streams(local_payload)

    _log("\n" + "=" * 70)
    _log("RIEPILOGO SYNC")
    _log("=" * 70)

    if local_streams_total == 0:
        _log("! Nessuno stream rilevato nel ciclo corrente: salto l'aggiornamento remoto per non svuotare il JSON.")
        _log("=" * 70)
        return

    if local_cmp == remote_cmp:
        _log("✓ Nessuna differenza trovata. Repository aggiornato.")
        _log("=" * 70)
        return

    _log("! Differenze trovate:")
    local_results = {r["source_url"]: r for r in local_payload.get("results", [])}
    remote_results = {r["source_url"]: r for r in (remote_payload or {}).get("results", [])}
    
    for source_url in local_results:
        local_r = local_results[source_url]
        remote_r = remote_results.get(source_url)
        
        streams_count = local_r.get("streams_count", 0)
        if not remote_r:
            _log(f"  [NUOVO] {source_url} → {streams_count} stream")
        else:
            local_streams = sorted([s.get("url") for s in local_r.get("streams", [])])
            remote_streams = sorted([s.get("url") for s in remote_r.get("streams", [])])
            if local_streams != remote_streams:
                _log(f"  [CAMBIATO] {source_url} → {streams_count} stream")
    
    _log("\nAggiorno il file su GitHub...")
    _upsert_remote_file(config, local_payload, remote_sha)
    _log("✓ Push completato.")
    _log("=" * 70)


def main() -> None:
    config = _load_config()
    _log("Servizio sync avviato.")
    _log(f"Repo: {config.github_repo}")
    _log(f"File: {config.github_json_path}")
    _log(f"Intervallo: {config.interval_seconds}s")

    while True:
        cycle_start = time.time()

        try:
            _run_once(config)
        except Exception as exc:
            _log(f"Errore ciclo sync: {exc}")

        elapsed = time.time() - cycle_start
        sleep_for = max(5, config.interval_seconds - int(elapsed))
        _log(f"Prossimo ciclo tra {sleep_for}s")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
