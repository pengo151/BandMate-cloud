#!/usr/bin/env python3
"""
bandmate_server_cloud.py — Pi Bandmate Cloud Server (Railway)

Differences from Pi version:
  - No pygame / WAV audio (no local audio hardware)
  - No Unix sockets (uses in-process threading instead)
  - Groove engine runs IN-PROCESS, emits MIDI note events via WebSocket
  - Clients receive MIDI note-on/off events and either:
      (a) Route them to a virtual MIDI port → Ableton (via midi_bridge.js in browser or midi_bridge.py locally)
      (b) Play them in the browser via Web Audio API (built-in synth fallback)
  - Song cache stored in-memory + optional Redis (Railway add-on) for persistence
  - Looper disabled (requires local audio hardware)
  - All Claude API calls unchanged
"""

import json
import os
import threading
import time
import hashlib
import random
from pathlib import Path
from collections import deque

from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit

import anthropic

# ── Config ────────────────────────────────────────────────────────────────────
PORT        = int(os.environ.get("PORT", 8080))
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "bandmate-cloud-secret")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── In-memory store (replaces file-based cache on Pi) ─────────────────────────
_song_cache   = {}   # md5(song_name) → song dict
_favourites   = []
_setlist      = []
_setlist_idx  = -1

# ── MIDI note map — GM drum map (channel 10, note numbers) ────────────────────
MIDI_NOTES = {
    "kick":          36,   # Bass Drum 1
    "snare":         38,   # Acoustic Snare
    "snare_rim":     37,   # Side Stick
    "snare_alt":     40,   # Electric Snare
    "hat_closed":    42,   # Closed Hi-Hat
    "hat_open":      46,   # Open Hi-Hat
    "hat_pedal":     44,   # Pedal Hi-Hat
    "ride":          51,   # Ride Cymbal 1
    "ride_bell":     53,   # Ride Bell
    "crash":         49,   # Crash Cymbal 1
    "crash_alt":     57,   # Crash Cymbal 2
    "tom_high":      50,   # High Tom
    "tom_high2":     48,   # Hi-Mid Tom
    "tom_mid":       47,   # Low-Mid Tom
    "tom_low":       43,   # High Floor Tom
    "bass_drop":     35,   # Acoustic Bass Drum (808 sub)
    "perc":          56,   # Cowbell (stand-in for perc)
}

# ── Groove engine state ────────────────────────────────────────────────────────
_engine_thread  = None
_engine_stop    = threading.Event()
_engine_lock    = threading.Lock()

_status = {
    "playing":    False,
    "section":    "",
    "bar":        0,
    "bpm":        0,
    "genre":      "",
    "title":      "",
    "feel":       "",
    "time_sig":   "4/4",
    "setlist":    [],
    "setlist_idx": -1,
    "mode":       "cloud",   # lets UI know this is cloud version
}

# ── Genre definitions (mirrors groove_engine_wav.py GENRES dict) ──────────────
GENRES = {
    "rock":   {"bpm": 120, "swing": 0.0, "human_ms": 8,  "fill_every": 4},
    "metal":  {"bpm": 160, "swing": 0.0, "human_ms": 4,  "fill_every": 4},
    "funk":   {"bpm": 98,  "swing": 0.3, "human_ms": 12, "fill_every": 4},
    "jazz":   {"bpm": 130, "swing": 0.5, "human_ms": 18, "fill_every": 8},
    "blues":  {"bpm": 90,  "swing": 0.4, "human_ms": 15, "fill_every": 8},
    "hiphop": {"bpm": 90,  "swing": 0.2, "human_ms": 14, "fill_every": 8},
    "trap":   {"bpm": 140, "swing": 0.0, "human_ms": 6,  "fill_every": 8},
    "house":  {"bpm": 126, "swing": 0.0, "human_ms": 5,  "fill_every": 8},
    "pop":    {"bpm": 115, "swing": 0.0, "human_ms": 8,  "fill_every": 4},
    "reggae": {"bpm": 75,  "swing": 0.1, "human_ms": 20, "fill_every": 8},
    "dub":    {"bpm": 70,  "swing": 0.15,"human_ms": 22, "fill_every": 8},
    "drill":  {"bpm": 140, "swing": 0.0, "human_ms": 6,  "fill_every": 8},
    "surf":   {"bpm": 145, "swing": 0.0, "human_ms": 6,  "fill_every": 4},
}

# ── Default patterns (16-step 4/4) ────────────────────────────────────────────
DEFAULT_PATTERNS = {
    "rock": {
        "K": [1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "H": [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0],
    },
    "metal": {
        "K": [1,0,1,0, 0,0,1,0, 1,0,1,0, 0,0,1,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "H": [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1],
    },
    "funk": {
        "K": [1,0,0,1, 0,0,1,0, 0,1,0,0, 1,0,0,0],
        "S": [0,0,0,0, 1,0,0,1, 0,0,0,0, 1,0,0,0],
        "H": [1,1,0,1, 1,0,1,1, 1,1,0,1, 1,0,1,0],
    },
    "jazz": {
        "K": [1,0,0,0, 0,0,0,0, 0,0,1,0, 0,0,0,0],
        "S": [0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0],
        "R": [1,0,1,1, 0,1,1,0, 1,0,1,1, 0,1,1,0],
    },
    "hiphop": {
        "K": [1,0,0,0, 0,0,0,1, 0,0,1,0, 0,0,0,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "H": [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],
    },
    "trap": {
        "K": [1,0,0,0, 0,0,0,0, 0,0,0,1, 0,0,0,0],
        "S": [0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "H": [1,1,0,1, 1,0,1,0, 1,1,0,1, 1,0,1,0],
        "OH":[0,0,0,0, 0,1,0,0, 0,0,0,0, 0,1,0,0],
    },
    "house": {
        "K": [1,0,0,0, 1,0,0,0, 1,0,0,0, 1,0,0,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "H": [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],
        "OH":[0,1,0,1, 0,1,0,1, 0,1,0,1, 0,1,0,1],
    },
    "reggae": {
        "K": [0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "SR":[0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0],
        "H": [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1],
    },
    "dub": {
        "K": [0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "S": [0,0,0,0, 0,0,1,0, 0,0,0,0, 0,0,1,0],
        "H": [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0],
    },
    "blues": {
        "K": [1,0,0,0, 0,0,1,0, 0,1,0,0, 0,0,0,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "R": [1,0,1,1, 0,1,1,0, 1,0,1,1, 0,1,1,0],
    },
    "drill": {
        "K": [1,0,0,0, 0,0,0,1, 0,0,0,0, 1,0,0,0],
        "S": [0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "H": [1,0,1,0, 1,1,0,1, 1,0,1,0, 1,1,0,1],
    },
    "surf": {
        "K": [1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "H": [1,1,1,1, 1,1,1,1, 1,1,1,1, 1,1,1,1],
    },
    "pop": {
        "K": [1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        "S": [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],
        "H": [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0],
        "OH":[0,1,0,1, 0,1,0,1, 0,1,0,1, 0,1,0,1],
    },
}

# ── MIDI emit helper ──────────────────────────────────────────────────────────
def emit_midi_hit(voice, velocity):
    """Broadcast a MIDI note-on event to all connected WebSocket clients."""
    note = MIDI_NOTES.get(voice)
    if note is None:
        return
    # MIDI channel 10 (9 zero-indexed) = standard drums
    # note_on: status 0x99, note, velocity
    socketio.emit("midi_hit", {
        "note":     note,
        "velocity": int(velocity),
        "voice":    voice,
        "channel":  9,           # 0-indexed, channel 10
        # Raw MIDI bytes for direct port routing
        "bytes":    [0x99, note, int(velocity)],
    })

# ── Groove engine (in-process, no pygame) ─────────────────────────────────────
def _vel(rng, center, spread, lo=20, hi=127):
    return max(lo, min(hi, int(center + rng.gauss(0, spread))))

def _jitter(rng, human_ms):
    return max(0.0, rng.gauss(0, human_ms / 1000.0))

def run_groove_cloud(genre, bpm, time_sig="4/4", structure=None, song_title="", feel="", stop_event=None):
    """
    In-process groove engine. Fires emit_midi_hit() instead of pygame.mixer.
    Mirrors the step sequencer logic from groove_engine_wav.py.
    """
    global _status

    rng       = random.Random(7)
    G         = GENRES.get(genre, GENRES["rock"])
    swing     = G["swing"]
    human_ms  = G["human_ms"]
    fill_ev   = G["fill_every"]

    # Time signature parsing
    parts     = time_sig.split("/")
    numer, denom = int(parts[0]), int(parts[1])
    TIME_SIG_STEPS = {"4/4":16,"3/4":12,"5/4":20,"6/8":12,"7/4":28,"7/8":14,"9/8":18,"12/8":24}
    steps_per_bar = TIME_SIG_STEPS.get(time_sig, 16)
    step_len  = (60.0 / bpm) / 4.0 if denom != 8 else (60.0 / bpm) / 2.0

    pat       = DEFAULT_PATTERNS.get(genre, DEFAULT_PATTERNS["rock"])
    struct    = structure
    struct_idx = 0
    struct_bar = 0
    section    = "intro"
    bar        = 0

    # Find verse index in structure for looping
    verse_idx = 0
    if struct:
        for i, e in enumerate(struct):
            if e.get("section") == "verse":
                verse_idx = i
                break

    _status.update({
        "playing":  True,
        "genre":    genre,
        "bpm":      bpm,
        "time_sig": time_sig,
        "title":    song_title,
        "feel":     feel,
        "section":  "intro",
        "bar":      1,
    })
    socketio.emit("status", _status)

    live_bpm     = bpm
    live_section = "intro"

    while not (stop_event and stop_event.is_set()):
        bar += 1
        t0   = time.perf_counter()

        # Handle live BPM changes
        new_bpm = _status.get("_bpm_override")
        if new_bpm and new_bpm != live_bpm:
            live_bpm  = new_bpm
            step_len  = (60.0 / live_bpm) / 4.0 if denom != 8 else (60.0 / live_bpm) / 2.0
            _status.pop("_bpm_override", None)

        # Section / structure advancement
        if struct:
            entry   = struct[struct_idx]
            section = entry["section"]
            struct_bar += 1
            if struct_bar > entry.get("bars", 4):
                struct_bar  = 1
                struct_idx += 1
                if struct_idx >= len(struct):
                    struct_idx = verse_idx
                entry   = struct[struct_idx]
                section = entry["section"]

            active_pat = entry.get("pattern") or pat
        else:
            # Simple section cycle without structure
            jump = _status.get("_jump_section")
            if jump:
                section = jump
                _status.pop("_jump_section", None)
            elif bar % 32 == 0:
                section = "chorus"
            elif bar % 16 == 0:
                section = "verse"
            elif bar == 1:
                section = "intro"
            active_pat = pat

        _status.update({"section": section, "bar": bar})
        socketio.emit("status", _status)

        K   = active_pat.get("K",  [0]*steps_per_bar)
        S   = active_pat.get("S",  [0]*steps_per_bar)
        SR  = active_pat.get("SR", [0]*steps_per_bar)
        H   = active_pat.get("H",  [0]*steps_per_bar)
        OH  = active_pat.get("OH", [0]*steps_per_bar)
        R   = active_pat.get("R",  [0]*steps_per_bar)
        B   = active_pat.get("B",  [0]*steps_per_bar)
        G_a = active_pat.get("G",  [0]*steps_per_bar)
        T   = active_pat.get("T",  [0]*steps_per_bar)

        def _fit(lst): return (lst + [0]*steps_per_bar)[:steps_per_bar]
        K, S, SR, H, OH, R, B, G_a, T = [_fit(x) for x in [K, S, SR, H, OH, R, B, G_a, T]]

        for i in range(steps_per_bar):
            if stop_event and stop_event.is_set():
                break

            target = t0 + i * step_len
            # Tight wait
            while True:
                remaining = target - time.perf_counter()
                if remaining <= 0:
                    break
                if remaining > 0.002:
                    time.sleep(0.001)

            if _status.get("muted"):
                continue

            # Swing
            swing_off = (swing * step_len * 0.5) if (i % 2 == 1) else 0.0
            human_off = _jitter(rng, human_ms)
            extra = swing_off + human_off
            if extra > 0.001:
                time.sleep(extra)

            beat_steps = set(range(0, steps_per_bar, max(1, steps_per_bar // 4)))

            if K[i]:
                kv = _vel(rng, 105, 4, lo=80, hi=115) if i in beat_steps else _vel(rng, 92, 7, lo=65, hi=108)
                emit_midi_hit("kick", kv)
            if S[i]:
                emit_midi_hit("snare", _vel(rng, 105, 7, lo=85, hi=120))
            if SR[i]:
                emit_midi_hit("snare_rim", _vel(rng, 110, 5, lo=90, hi=127))
            if H[i]:
                hv = _vel(rng, 85, 8, lo=55, hi=110) if i in (0, 4, 8, 12) else _vel(rng, 68, 10, lo=45, hi=95)
                emit_midi_hit("hat_closed", hv)
            if OH[i]:
                emit_midi_hit("hat_open", _vel(rng, 65, 6, lo=35, hi=100))
            if R[i]:
                emit_midi_hit("ride", _vel(rng, 65, 5, lo=40, hi=100))
            if B[i]:
                emit_midi_hit("bass_drop", _vel(rng, 88, 5, lo=70, hi=105))
            if G_a[i] and rng.random() < 0.65:
                emit_midi_hit("snare", _vel(rng, 42, 8, lo=25, hi=68))
            if T[i]:
                toms = ["tom_high", "tom_mid", "tom_low"]
                emit_midi_hit(toms[i % 3], _vel(rng, 90, 8, lo=65, hi=120))

    _status.update({"playing": False, "section": "", "bar": 0})
    socketio.emit("status", _status)


# ── Engine lifecycle ──────────────────────────────────────────────────────────
def stop_engine():
    global _engine_thread, _engine_stop
    _engine_stop.set()
    if _engine_thread and _engine_thread.is_alive():
        _engine_thread.join(timeout=2)
    _engine_stop = threading.Event()
    _status["playing"] = False

def start_engine(genre, bpm, time_sig="4/4", structure=None, song_title="", feel=""):
    global _engine_thread
    stop_engine()
    _engine_thread = threading.Thread(
        target=run_groove_cloud,
        args=(genre, float(bpm), time_sig, structure, song_title, feel, _engine_stop),
        daemon=True,
    )
    _engine_thread.start()


# ── Song lookup (same logic as Pi, using in-memory cache) ─────────────────────
def _cache_key(song_name):
    return hashlib.md5(song_name.strip().lower().encode()).hexdigest()

def lookup_song_cached(song_name):
    return _song_cache.get(_cache_key(song_name))

def lookup_song_api(song_name):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""You are a drum machine AI assistant.
For the song "{song_name}", return ONLY a valid JSON object (no markdown, no preamble) with these exact keys:
  title (string), song_name (string, artist + title lowercase), bpm (integer 40-400),
  time_sig (e.g. "4/4"), genre (one of: blues dub drill funk hiphop house jazz metal pop reggae rock surf trap),
  feel (2-5 word description),
  structure (array of objects with keys: section, bars, pattern)
    where section is one of: intro verse chorus bridge fill outro
    and pattern uses keys K S H OH R C B G T SR (16-step arrays of 0/1)
Example structure entry: {{"section":"verse","bars":8,"pattern":{{"K":[1,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0],"S":[0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0],"H":[1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0]}}}}
Return ONLY the JSON object."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=6000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    _song_cache[_cache_key(song_name)] = data
    return data


# ── HTTP Routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # Serve the existing index.html — look in web/ subdir
    html_path = Path(__file__).parent / "web" / "index.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>Bandmate Cloud</h1><p>web/index.html not found.</p>", 404

@app.route("/api/status")
def api_status():
    return jsonify(_status)

@app.route("/api/song/lookup", methods=["POST"])
def api_lookup():
    data = request.json or {}
    song_name = data.get("song", "").strip()
    if not song_name:
        return jsonify({"error": "No song name"}), 400
    result = lookup_song_cached(song_name)
    if not result:
        try:
            result = lookup_song_api(song_name)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    if not result:
        return jsonify({"error": "Could not find song"}), 404
    return jsonify(result)

@app.route("/api/song/play", methods=["POST"])
def api_play():
    data = request.json or {}
    song_name = data.get("song", "").strip()
    if not song_name:
        return jsonify({"error": "No song name"}), 400
    cached = lookup_song_cached(song_name)
    if not cached:
        return jsonify({"error": "Song not in cache — look it up first"}), 404
    start_engine(
        genre      = cached.get("genre", "rock"),
        bpm        = cached.get("bpm", 100),
        time_sig   = cached.get("time_sig", "4/4"),
        structure  = cached.get("structure"),
        song_title = cached.get("title", ""),
        feel       = cached.get("feel", ""),
    )
    return jsonify({"ok": True, "song": cached})

@app.route("/api/song/bpm", methods=["POST"])
def api_bpm_override():
    data = request.json or {}
    bpm = data.get("bpm")
    if bpm:
        _status["_bpm_override"] = float(bpm)
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_engine()
    return jsonify({"ok": True})

@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    data = request.json or {}
    cmd  = data.get("cmd", "")
    val  = data.get("value")
    if cmd == "bpm_delta" and val:
        cur = _status.get("bpm", 100)
        _status["_bpm_override"] = max(40, min(400, cur + int(val)))
    elif cmd == "jump_section":
        _status["_jump_section"] = val
    elif cmd == "mute_toggle":
        _status["muted"] = not _status.get("muted", False)
    elif cmd == "stop":
        stop_engine()
    return jsonify({"ok": True})

@app.route("/api/genre", methods=["POST"])
def api_genre():
    data  = request.json or {}
    genre = data.get("genre", "rock")
    bpm   = float(data.get("bpm", GENRES.get(genre, {}).get("bpm", 100)))
    ts    = data.get("time_sig", "4/4")
    start_engine(genre=genre, bpm=bpm, time_sig=ts)
    return jsonify({"ok": True, "genre": genre, "bpm": bpm})

@app.route("/api/artist/browse", methods=["POST"])
def api_artist_browse():
    data   = request.json or {}
    artist = (data.get("artist") or "").strip()
    genre  = (data.get("genre") or "").strip()
    if not artist:
        return jsonify({"error": "No artist name"}), 400
    genre_hint = f" Focus on {genre} songs." if genre else ""
    prompt = f"""List 10 popular songs by {artist}.{genre_hint}
Return ONLY a JSON array, no markdown, no preamble.
Each item must have these exact keys:
  title, song_name (artist + title lowercase), bpm (integer 40-400),
  time_sig (e.g. "4/4"), genre (one of: blues dub drill funk hiphop house jazz metal pop reggae rock surf trap),
  feel (2-5 word description)"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        songs = json.loads(raw)
        for s in songs:
            sn = s.get("song_name", "").strip().lower()
            if sn:
                _song_cache[_cache_key(sn)] = s
        return jsonify({"songs": songs, "artist": artist})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Setlist routes (same as Pi) ───────────────────────────────────────────────
@app.route("/api/setlist")
def api_setlist_get():
    return jsonify({"setlist": _setlist, "idx": _setlist_idx})

@app.route("/api/setlist/add", methods=["POST"])
def api_setlist_add():
    song_name = (request.json or {}).get("song", "").strip()
    cached = lookup_song_cached(song_name)
    if cached:
        _setlist.append(cached)
    return jsonify({"ok": bool(cached), "count": len(_setlist)})

@app.route("/api/setlist/remove", methods=["POST"])
def api_setlist_remove():
    global _setlist
    idx = int((request.json or {}).get("idx", -1))
    if 0 <= idx < len(_setlist):
        _setlist.pop(idx)
    return jsonify({"ok": True, "count": len(_setlist)})

@app.route("/api/setlist/play", methods=["POST"])
def api_setlist_play():
    global _setlist_idx
    idx = int((request.json or {}).get("idx", 0))
    if 0 <= idx < len(_setlist):
        _setlist_idx = idx
        _status["setlist_idx"] = idx
        song = _setlist[idx]
        start_engine(
            genre=song.get("genre", "rock"),
            bpm=song.get("bpm", 100),
            time_sig=song.get("time_sig", "4/4"),
            structure=song.get("structure"),
            song_title=song.get("title", ""),
        )
    return jsonify({"ok": True})

@app.route("/api/setlist/next", methods=["POST"])
def api_setlist_next():
    global _setlist_idx
    _setlist_idx = min(_setlist_idx + 1, len(_setlist) - 1)
    if 0 <= _setlist_idx < len(_setlist):
        song = _setlist[_setlist_idx]
        start_engine(genre=song.get("genre","rock"), bpm=song.get("bpm",100))
    return jsonify({"ok": True, "idx": _setlist_idx})

@app.route("/api/setlist/prev", methods=["POST"])
def api_setlist_prev():
    global _setlist_idx
    _setlist_idx = max(_setlist_idx - 1, 0)
    if 0 <= _setlist_idx < len(_setlist):
        song = _setlist[_setlist_idx]
        start_engine(genre=song.get("genre","rock"), bpm=song.get("bpm",100))
    return jsonify({"ok": True, "idx": _setlist_idx})

# ── Favourites routes ─────────────────────────────────────────────────────────
@app.route("/api/favourites")
def api_favourites_get():
    return jsonify({"favourites": _favourites})

@app.route("/api/favourites/add", methods=["POST"])
def api_favourites_add():
    song_name = (request.json or {}).get("song", "").strip()
    cached = lookup_song_cached(song_name)
    if cached and not any(f.get("song_name") == song_name for f in _favourites):
        _favourites.append(cached)
    return jsonify({"ok": bool(cached), "count": len(_favourites)})

@app.route("/api/favourites/remove", methods=["POST"])
def api_favourites_remove():
    song_name = (request.json or {}).get("song", "").strip()
    _favourites[:] = [f for f in _favourites if f.get("song_name") != song_name]
    return jsonify({"ok": True})

@app.route("/api/cache/list")
def api_cache_list():
    return jsonify({"songs": list(_song_cache.values()), "count": len(_song_cache)})

# ── Cloud-specific: MIDI info endpoint (tells client what port to use) ─────────
@app.route("/api/midi/info")
def api_midi_info():
    return jsonify({
        "mode":        "websocket",
        "channel":     10,         # GM drum channel
        "socket_path": "/socket.io/",
        "event":       "midi_hit",
        "note_map":    MIDI_NOTES,
        "instructions": {
            "ableton":  "Use midi_bridge.py locally — connects to this server via WebSocket, outputs to a virtual MIDI port that Ableton sees",
            "browser":  "Web Audio synth is built into the UI — no setup needed",
            "ios":      "Use AUM or similar IAA host — connect midi_bridge to Ableton Link",
        }
    })

# ── PWA routes ────────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name":             "Bandmate Cloud",
        "short_name":       "Bandmate",
        "start_url":        "/",
        "display":          "standalone",
        "background_color": "#120e08",
        "theme_color":      "#120e08",
        "icons": [
            {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
        ]
    })

@app.route("/sw.js")
def sw():
    js = """
const CACHE = 'bandmate-cloud-v1';
const ASSETS = ['/'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
});
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/') || e.request.url.includes('/socket.io/')) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
"""
    return js, 200, {"Content-Type": "application/javascript"}

# ── WebSocket events ──────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("status", _status)
    emit("midi_info", {"note_map": MIDI_NOTES, "channel": 9})

@socketio.on("cmd")
def on_cmd(data):
    cmd = data.get("cmd", "")
    val = data.get("value")
    if cmd == "bpm_delta" and val:
        _status["_bpm_override"] = max(40, min(400, _status.get("bpm", 100) + int(val)))
    elif cmd == "jump_section":
        _status["_jump_section"] = val
    elif cmd == "mute_toggle":
        _status["muted"] = not _status.get("muted", False)
    elif cmd == "stop":
        stop_engine()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n╔══════════════════════════════════════════════╗")
    print(f"║   ☁️   BANDMATE CLOUD SERVER  (Railway)  🥁   ║")
    print(f"╚══════════════════════════════════════════════╝")
    print(f"\n  Port         : {PORT}")
    print(f"  Anthropic key: {'✅ set' if ANTHROPIC_KEY else '❌ NOT SET — set ANTHROPIC_API_KEY env var'}")
    print(f"  MIDI output  : WebSocket → clients")
    print(f"  Audio output : Browser Web Audio (no server-side audio)")
    print(f"\n  MIDI bridge  : python midi_bridge.py --server https://your-app.railway.app")
    print()
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False)
