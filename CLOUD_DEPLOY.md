# Bandmate Cloud — Railway Deployment Guide

## What this is
A cloud-hosted version of Pi Bandmate. The drum brain (Flask + Claude API + groove engine) 
runs on Railway. Users connect via browser or phone — no Pi required.

Audio reaches the user two ways:
- **Browser synth** — synthesized drums play directly in the browser via Web Audio API. Works on all devices including iOS Safari. No setup needed.
- **Ableton MIDI** — run `midi_bridge.py` on your local machine. It connects to the cloud server and forwards drum hits to a virtual MIDI port. Ableton sees it as a controller.

---

## Files

| File | Where it runs | Purpose |
|---|---|---|
| `bandmate_server_cloud.py` | Railway (cloud) | Flask server + in-process groove engine + WebSocket MIDI emit |
| `requirements.txt` | Railway | Python dependencies |
| `Procfile` | Railway | Start command |
| `railway.toml` | Railway | Railway config |
| `midi_bridge.py` | **Your local machine** | Receives WebSocket MIDI → virtual port → Ableton |
| `bandmate_cloud_client.js` | Browser (add to index.html) | Web Audio synth + Web MIDI routing |

---

## Railway Deployment (step by step)

### 1. Create the Railway project

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Push these cloud files to a GitHub repo, or use **Deploy from local** with the Railway CLI

### 2. Set environment variables in Railway

In your Railway project → **Variables** tab, add:

```
ANTHROPIC_API_KEY=sk-ant-...your key here...
SECRET_KEY=some-random-string-here
```

That's it. No other env vars needed.

### 3. Copy your web/index.html

The cloud server serves the same `web/index.html` as the Pi.
Make sure `web/index.html` is in your repo alongside `bandmate_server_cloud.py`.

Add this line near the bottom of `index.html` (before `</body>`):
```html
<script src="/static/bandmate_cloud_client.js"></script>
```

And copy `bandmate_cloud_client.js` to a `static/` folder.

Or inline the entire contents of `bandmate_cloud_client.js` in a `<script>` tag.

### 4. Deploy

Railway auto-deploys on push to main. First deploy takes ~2 minutes.
Your app will be at: `https://your-app-name.railway.app`

### 5. Test it

Open the URL on your phone. The UI should load and you should be able to:
- Search for a song → it hits Claude Haiku API → returns pattern
- Hit Play → drums fire via Web Audio in your browser

---

## Ableton MIDI Setup

### Mac

1. Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
2. Window → **Show MIDI Studio**
3. Double-click **IAC Driver** → check **Device is online** → Add a port named `Bandmate Drums`
4. On your local machine, install the bridge:
   ```bash
   pip install "python-socketio[client]" python-rtmidi websocket-client
   ```
5. Run the bridge:
   ```bash
   python midi_bridge.py --server https://your-app.railway.app
   ```
6. In Ableton: **Preferences → Link/Tempo/MIDI → MIDI Ports**
   - Enable **Track** on IAC Driver / Bandmate Drums (Input)
7. Drop a **Drum Rack** on an empty MIDI track
8. Set track MIDI input to **IAC Driver** / All Channels
9. Arm the track → hit play on Bandmate UI → drums appear in Ableton

### Windows

1. Download and install [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) (free)
2. In loopMIDI, create a port named **Bandmate Drums** and click the `+` button
3. Install and run the bridge (same as Mac above)
4. In Ableton: enable loopMIDI Bandmate Drums as a MIDI input
5. Same steps 7-9 as Mac

### Chrome/Edge (no bridge needed)

Chrome has built-in Web MIDI support. If you have IAC Driver or loopMIDI set up:
1. Open the Bandmate URL in Chrome
2. Chrome will ask for MIDI permission — click Allow
3. `bandmate_cloud_client.js` automatically finds the virtual port and routes hits there
4. No `midi_bridge.py` needed — the browser does it directly

---

## Architecture

```
Railway server
  └─ bandmate_server_cloud.py
       ├─ Flask HTTP API (same routes as Pi)
       ├─ In-process groove engine (no pygame — no audio)
       ├─ Claude Haiku API calls (song lookup / artist browse)
       └─ Socket.IO WebSocket server
            └─ emits: midi_hit {note, velocity, voice, channel, bytes}
            └─ emits: status   {playing, section, bar, bpm, genre, ...}

User's browser
  └─ index.html + bandmate_cloud_client.js
       ├─ Socket.IO client → receives midi_hit
       ├─ Web Audio API → synthesized drums (always)
       └─ Web MIDI API  → real MIDI port → Ableton (Chrome/Edge only)

User's local machine (optional, for Ableton via non-Chrome)
  └─ midi_bridge.py
       ├─ Socket.IO client → receives midi_hit
       └─ python-rtmidi  → virtual MIDI port → Ableton
```

---

## Differences from Pi version

| Feature | Pi | Cloud |
|---|---|---|
| Drum audio | BFD WAV samples (iRig) | Web Audio synth OR Ableton via MIDI |
| Groove engine | Subprocess (groove_engine_wav.py) | In-process thread |
| Song cache | ~/.mpc_ai/song_cache.json | In-memory (resets on restart) |
| Guitar looper | ✅ (sounddevice/iRig) | ❌ (requires local audio hardware) |
| Gesture control | ✅ (Hailo AI HAT + camera) | ❌ (sidelined) |
| Thermal monitor | ✅ | ❌ |
| Multiple users | 1 (Pi is local) | ✅ (anyone with the URL) |
| Cost | Pi hardware one-time | ~$5/mo Railway Hobby plan |

---

## Next steps (roadmap)

1. **MuseScore tab parser** — parse MusicXML drum parts → pattern_library.json
2. **Ableton MIDI output from groove engine** — full MIDI file export of sessions
3. **Persistent cache** — add Railway Redis add-on so song cache survives restarts
4. **Multi-user sessions** — each user gets their own groove engine instance

---

## Troubleshooting

**"ANTHROPIC_API_KEY not set"** — Add it in Railway Variables tab

**Songs not playing audio in browser** — Click anywhere on the page first (browser autoplay policy requires a user gesture before Web Audio starts)

**Ableton not seeing MIDI** — Check that the bridge is running and connected (`✅ Connected` in terminal). Check Ableton MIDI preferences — the input port must have Track enabled.

**Railway deploy fails** — Check the deploy logs. Common issue: `eventlet` vs `gevent`. If eventlet fails, change `requirements.txt` to use `gevent` and update `socketio.run()` call to `async_mode="gevent"`.
