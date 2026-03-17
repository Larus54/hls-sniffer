#!/usr/bin/env python3
"""
HLS Sniffer Web Server - Interfaccia web per trovare e riprodurre flussi HLS.
Avvia con: python3 server.py
Poi apri: http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template_string
from hls_sniffer import sniff

app = Flask(__name__)

HTML = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HLS Sniffer</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f0f0f;
      color: #e0e0e0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 16px;
    }

    h1 {
      font-size: 1.8rem;
      font-weight: 700;
      margin-bottom: 8px;
      color: #fff;
      letter-spacing: -0.5px;
    }

    .subtitle {
      color: #666;
      font-size: 0.9rem;
      margin-bottom: 36px;
    }

    .card {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 12px;
      padding: 24px;
      width: 100%;
      max-width: 720px;
    }

    .input-row {
      display: flex;
      gap: 10px;
    }

    input[type="text"] {
      flex: 1;
      background: #111;
      border: 1px solid #333;
      border-radius: 8px;
      color: #e0e0e0;
      font-size: 0.95rem;
      padding: 10px 14px;
      outline: none;
      transition: border-color 0.2s;
    }
    input[type="text"]:focus { border-color: #555; }
    input[type="text"]::placeholder { color: #444; }

    button {
      background: #e50914;
      border: none;
      border-radius: 8px;
      color: #fff;
      cursor: pointer;
      font-size: 0.95rem;
      font-weight: 600;
      padding: 10px 22px;
      transition: background 0.2s, opacity 0.2s;
      white-space: nowrap;
    }
    button:hover { background: #c4070f; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Status */
    #status {
      margin-top: 16px;
      font-size: 0.88rem;
      color: #888;
      min-height: 20px;
    }
    #status.error { color: #e05050; }
    #status.success { color: #50c878; }

    /* Spinner */
    .spinner {
      display: inline-block;
      width: 14px; height: 14px;
      border: 2px solid #444;
      border-top-color: #e50914;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      vertical-align: middle;
      margin-right: 6px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Streams list */
    #streams {
      margin-top: 20px;
      display: none;
    }
    #streams h3 {
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: #555;
      margin-bottom: 10px;
    }
    .stream-item {
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      padding: 10px 14px;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      gap: 10px;
      cursor: pointer;
      transition: border-color 0.2s;
    }
    .stream-item:hover { border-color: #e50914; }
    .stream-item.active { border-color: #e50914; background: #1f0a0a; }
    .stream-label {
      font-size: 0.8rem;
      font-weight: 600;
      color: #e50914;
      min-width: 50px;
    }
    .stream-url {
      font-size: 0.78rem;
      color: #888;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .copy-btn {
      margin-left: auto;
      background: #222;
      border: 1px solid #333;
      border-radius: 6px;
      color: #888;
      cursor: pointer;
      font-size: 0.75rem;
      padding: 4px 10px;
      flex-shrink: 0;
      transition: color 0.2s, border-color 0.2s;
    }
    .copy-btn:hover { color: #fff; border-color: #555; background: #222; }

    /* Player */
    #player-wrap {
      margin-top: 24px;
      width: 100%;
      max-width: 720px;
      display: none;
    }
    video {
      width: 100%;
      border-radius: 10px;
      background: #000;
      display: block;
    }
    #now-playing {
      margin-top: 8px;
      font-size: 0.78rem;
      color: #555;
      text-align: center;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  </style>
</head>
<body>
  <h1>📡 HLS Sniffer</h1>
  <p class="subtitle">Incolla il link del player per estrarre e riprodurre il flusso HLS</p>

  <div class="card">
    <div class="input-row">
      <input type="text" id="url-input"
             placeholder="https://dlstreams.top/player/stream-576.php"
             autocomplete="off" spellcheck="false" />
      <button id="sniff-btn" onclick="sniffStream()">Trova Stream</button>
    </div>
    <div id="status"></div>
    <div id="streams">
      <h3>Flussi trovati</h3>
      <div id="streams-list"></div>
    </div>
  </div>

  <div id="player-wrap">
    <video id="video" controls autoplay></video>
    <div id="now-playing"></div>
  </div>

  <script>
    let hls = null;

    async function sniffStream() {
      const urlInput = document.getElementById('url-input');
      const url = urlInput.value.trim();
      if (!url) return;

      const btn = document.getElementById('sniff-btn');
      const status = document.getElementById('status');
      const streamsDiv = document.getElementById('streams');
      const streamsList = document.getElementById('streams-list');

      btn.disabled = true;
      streamsDiv.style.display = 'none';
      streamsList.innerHTML = '';
      status.className = '';
      status.innerHTML = '<span class="spinner"></span> Avvio Chromium e intercettazione stream…';

      try {
        const res = await fetch('/api/sniff', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url}),
        });
        const data = await res.json();

        if (!res.ok || data.error) {
          status.className = 'error';
          status.textContent = '✗ ' + (data.error || 'Errore sconosciuto');
          return;
        }

        if (!data.streams || data.streams.length === 0) {
          status.className = 'error';
          status.textContent = '✗ Nessun flusso HLS trovato per questo link.';
          return;
        }

        status.className = 'success';
        status.textContent = `✓ ${data.streams.length} flusso/i trovato/i`;
        streamsDiv.style.display = 'block';

        // Trova la master playlist (index.m3u8) o usa il primo
        const master = data.streams.find(s => s.includes('index.m3u8')) || data.streams[0];

        data.streams.forEach((streamUrl, i) => {
          const isIndex = streamUrl.includes('index.m3u8');
          const item = document.createElement('div');
          item.className = 'stream-item' + (streamUrl === master ? ' active' : '');
          item.dataset.url = streamUrl;
          item.onclick = () => playStream(streamUrl, item);

          const label = document.createElement('span');
          label.className = 'stream-label';
          label.textContent = isIndex ? 'MASTER' : 'TRACK ' + i;

          const urlSpan = document.createElement('span');
          urlSpan.className = 'stream-url';
          urlSpan.textContent = streamUrl;

          const copyBtn = document.createElement('button');
          copyBtn.className = 'copy-btn';
          copyBtn.textContent = 'Copia';
          copyBtn.onclick = (e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(streamUrl);
            copyBtn.textContent = '✓';
            setTimeout(() => copyBtn.textContent = 'Copia', 1500);
          };

          item.appendChild(label);
          item.appendChild(urlSpan);
          item.appendChild(copyBtn);
          streamsList.appendChild(item);
        });

        // Avvia automaticamente la master playlist
        playStream(master, streamsList.querySelector('.stream-item.active'));

      } catch (err) {
        status.className = 'error';
        status.textContent = '✗ Errore di rete: ' + err.message;
      } finally {
        btn.disabled = false;
      }
    }

    function playStream(streamUrl, activeItem) {
      // Aggiorna active
      document.querySelectorAll('.stream-item').forEach(el => el.classList.remove('active'));
      if (activeItem) activeItem.classList.add('active');

      const playerWrap = document.getElementById('player-wrap');
      const video = document.getElementById('video');
      const nowPlaying = document.getElementById('now-playing');

      playerWrap.style.display = 'block';
      nowPlaying.textContent = streamUrl;

      if (hls) { hls.destroy(); hls = null; }

      if (Hls.isSupported()) {
        hls = new Hls();
        hls.loadSource(streamUrl);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
        hls.on(Hls.Events.ERROR, (_, data) => {
          if (data.fatal) {
            document.getElementById('status').className = 'error';
            document.getElementById('status').textContent = '✗ Errore player: ' + data.details;
          }
        });
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // Safari nativo
        video.src = streamUrl;
        video.play();
      }
    }

    // Invio con Enter
    document.getElementById('url-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') sniffStream();
    });
  </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/sniff', methods=['POST'])
def api_sniff():
    data = request.get_json(silent=True)
    if not data or not data.get('url'):
        return jsonify({'error': 'Parametro "url" mancante'}), 400

    url = data['url'].strip()
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'URL non valido (deve iniziare con http:// o https://)'}), 400

    try:
        streams = sniff(url)
        # Ordina: master playlist prima, poi le altre
        sorted_streams = sorted(streams, key=lambda u: (0 if 'index.m3u8' in u else 1))
        return jsonify({'streams': sorted_streams})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("=" * 50)
    print("  HLS Sniffer Web Server")
    print("  http://localhost:8080")
    print("=" * 50)
    app.run(host='127.0.0.1', port=8080, debug=False)
