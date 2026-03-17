#!/usr/bin/env python3
"""
FastAPI endpoint per HLS Sniffer.
Pensato sia per esecuzione locale (uvicorn) sia per deploy serverless (es. Vercel/Railway).
"""

import sys
import os
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# Vercel esegue il file da una directory diversa: assicura che la root del progetto
# sia nel sys.path così 'hls_sniffer' viene trovato sempre.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hls_sniffer import sniff_with_playwright, sniff_with_requests, PLAYWRIGHT_AVAILABLE

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
            streams |= sniff_with_requests(url, referrer=referer)

        if mode in ("browser", "both"):
            streams |= sniff_with_playwright(url, referrer=referer)

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    sorted_streams = sorted(streams, key=lambda u: (0 if "index.m3u8" in u else 1, u))
    return SniffResponse(streams=sorted_streams, mode=mode)


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
