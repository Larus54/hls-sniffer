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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from hls_sniffer import sniff

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_INTERVAL_SECONDS = 30 * 60


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
    return datetime.now(timezone.utc).isoformat()


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
                }
            )
            continue

        raise ValueError(f"Target non valido in {path}: {item}")

    return targets


def _collect_local_snapshot(targets: List[Dict[str, Any]]) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []

    for idx, target in enumerate(targets, start=1):
        url = str(target["url"]).strip()
        referer = target.get("referer")
        if referer:
            referer = str(referer).strip()

        print(f"[{idx}/{len(targets)}] Scan: {url}")
        started_at = time.time()

        try:
            streams, metadata = sniff(
                url,
                referrer=referer,
                skip_requests=True,
                include_metadata=True,
            )
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
        "message": f"chore(hls): refresh streams {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
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
    print("\n" + "=" * 70)
    print(f"[{_now_iso()}] Inizio sync HLS")
    print("=" * 70)

    targets = _load_targets(config.monitor_urls_file)
    local_payload = _collect_local_snapshot(targets)

    remote_payload, remote_sha = _fetch_remote_file(config)

    local_cmp = _canonical_for_compare(local_payload)
    remote_cmp = _canonical_for_compare(remote_payload or {"results": []})

    if local_cmp == remote_cmp:
        print("Nessuna differenza rispetto al JSON su GitHub. Nessun push.")
        return

    print("Differenze trovate. Aggiorno il file su GitHub...")
    _upsert_remote_file(config, local_payload, remote_sha)
    print("Push completato.")


def main() -> None:
    config = _load_config()
    print("Servizio sync avviato.")
    print(f"Repo: {config.github_repo}")
    print(f"File: {config.github_json_path}")
    print(f"Intervallo: {config.interval_seconds}s")

    while True:
        cycle_start = time.time()

        try:
            _run_once(config)
        except Exception as exc:
            print(f"Errore ciclo sync: {exc}")

        elapsed = time.time() - cycle_start
        sleep_for = max(5, config.interval_seconds - int(elapsed))
        print(f"Prossimo ciclo tra {sleep_for}s")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
