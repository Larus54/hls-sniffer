#!/usr/bin/env python3
"""
FastAPI endpoint per HLS Sniffer.
Pensato sia per esecuzione locale (uvicorn) sia per deploy serverless (es. Vercel/Railway).
"""

import sys
import os
from typing import List, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# Vercel esegue il file da una directory diversa: assicura che la root del progetto
# sia nel sys.path così 'hls_sniffer' viene trovato sempre.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hls_sniffer import sniff_with_playwright, sniff_with_requests, PLAYWRIGHT_AVAILABLE, USER_AGENT

app = FastAPI(title="HLS Sniffer API", version="1.0.0")


class SniffRequest(BaseModel):
    url: str = Field(..., description="URL della pagina player")
    referer: Optional[str] = Field(default=None, description="Referer opzionale")
    mode: Literal["browser", "requests", "both"] = Field(
        default="browser",
        description="browser=Playwright, requests=HTTP statico, both=unione",
    )


class SniffResponse(BaseModel):
    streams: List[str]
    mode: str
    stream_details: List[dict]


def _origin_from_referrer(referer: Optional[str]) -> Optional[str]:
    if not referer:
        return None
    parsed = urlparse(referer)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _merge_detail(detail_map: dict, stream_url: str, *, referer: Optional[str], origin: Optional[str], user_agent: Optional[str], source: str) -> None:
    current = detail_map.setdefault(
        stream_url,
        {
            "url": stream_url,
            "referer": None,
            "origin": None,
            "user_agent": None,
            "source": source,
        },
    )
    if not current.get("referer") and referer:
        current["referer"] = referer
    if not current.get("origin") and origin:
        current["origin"] = origin
    if not current.get("user_agent") and user_agent:
        current["user_agent"] = user_agent


@app.get("/")
def root() -> dict:
    return {
        "name": "HLS Sniffer API",
        "status": "ok",
        "endpoints": ["/health", "/sniff", "/docs"],
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _run_sniff(url: str, referer: Optional[str], mode: Literal["browser", "requests", "both"]) -> SniffResponse:
    url = url.strip()
    referer = referer.strip() if referer else None

    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL non valido")

    streams = set()
    stream_details = {}

    if mode in ("browser", "both") and not PLAYWRIGHT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail=(
                "Playwright/Chromium non disponibile in questo ambiente. "
                "Usa mode='requests' oppure esegui su Railway/Render "
                "dove Chromium è installabile."
            ),
        )

    try:
        if mode in ("requests", "both"):
            requests_streams = sniff_with_requests(url, referrer=referer)
            streams |= requests_streams
            origin = _origin_from_referrer(referer)
            for stream_url in requests_streams:
                _merge_detail(
                    stream_details,
                    stream_url,
                    referer=referer,
                    origin=origin,
                    user_agent=USER_AGENT,
                    source="requests",
                )

        if mode in ("browser", "both"):
            browser_streams, browser_meta = sniff_with_playwright(
                url,
                referrer=referer,
                include_metadata=True,
            )
            streams |= browser_streams
            for stream_url in browser_streams:
                meta = browser_meta.get(stream_url, {})
                _merge_detail(
                    stream_details,
                    stream_url,
                    referer=meta.get("referer") or referer,
                    origin=meta.get("origin") or _origin_from_referrer(meta.get("referer") or referer),
                    user_agent=meta.get("user_agent") or USER_AGENT,
                    source="playwright",
                )

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    sorted_streams = sorted(streams, key=lambda u: (0 if "index.m3u8" in u else 1, u))
    ordered_details = [stream_details.get(stream_url, {"url": stream_url}) for stream_url in sorted_streams]
    return SniffResponse(streams=sorted_streams, mode=mode, stream_details=ordered_details)


@app.get("/sniff", response_model=SniffResponse)
def sniff_get(
    url: str = Query(..., description="URL della pagina player"),
    referer: Optional[str] = Query(default=None, description="Referer opzionale"),
    mode: Literal["browser", "requests", "both"] = Query(default="browser"),
) -> SniffResponse:
    return _run_sniff(url=url, referer=referer, mode=mode)


@app.post("/sniff", response_model=SniffResponse)
def sniff_endpoint(payload: SniffRequest) -> SniffResponse:
    return _run_sniff(url=payload.url, referer=payload.referer, mode=payload.mode)
