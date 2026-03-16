/**
 * bandmate_cloud_client.js
 * Drop this into the <head> of index.html (or inline at bottom of <body>).
 *
 * What it does:
 *   1. Connects to the cloud server via Socket.IO
 *   2. Receives midi_hit events
 *   3. If Web MIDI API is available → sends to a real MIDI output (no bridge needed on desktop Chrome)
 *   4. Falls back to Web Audio API synth (works on all browsers including iOS Safari)
 *
 * For Ableton users on desktop Chrome/Edge:
 *   - Enable "Experimental Web Platform features" flag OR use Chrome 101+
 *   - Chrome exposes Web MIDI directly — no midi_bridge.py needed
 *   - Create a virtual MIDI port (IAC / loopMIDI) named "Bandmate Drums"
 *   - Ableton sees it as a MIDI input
 *
 * For everyone else (Firefox, Safari, iOS):
 *   - Web Audio synth plays drum sounds directly in the browser
 *   - Sounds: synthesized kick/snare/hat — not the BFD samples, but usable
 */

(function () {
  "use strict";

  // ── Config ──────────────────────────────────────────────────────────────────
  const CLOUD_MODE       = window.location.hostname !== "localhost" &&
                           window.location.hostname !== "192.168.1.153" &&
                           window.location.hostname !== "192.168.4.1";
  const SOCKET_URL       = window.location.origin;   // same origin
  const MIDI_PORT_NAME   = "Bandmate Drums";
  const DRUM_CHANNEL     = 9;  // 0-indexed, = MIDI channel 10

  // ── Drum note → Web Audio synth params ──────────────────────────────────────
  const SYNTH_PARAMS = {
    36: { type: "kick"   },   // Bass Drum
    38: { type: "snare"  },   // Acoustic Snare
    40: { type: "snare"  },   // Electric Snare
    37: { type: "rim"    },   // Side Stick
    42: { type: "hat_c"  },   // Closed Hi-Hat
    46: { type: "hat_o"  },   // Open Hi-Hat
    44: { type: "hat_c"  },   // Pedal Hi-Hat
    51: { type: "ride"   },   // Ride
    53: { type: "ride"   },   // Ride Bell
    49: { type: "crash"  },   // Crash 1
    57: { type: "crash"  },   // Crash 2
    50: { type: "tom_h"  },   // High Tom
    48: { type: "tom_h"  },   // Hi-Mid Tom
    47: { type: "tom_m"  },   // Low-Mid Tom
    43: { type: "tom_l"  },   // Floor Tom
    35: { type: "kick"   },   // Acoustic Bass Drum
    56: { type: "rim"    },   // Cowbell → rim stand-in
  };

  // ── Web Audio Context ────────────────────────────────────────────────────────
  let _audioCtx = null;
  function getAudioCtx() {
    if (!_audioCtx) {
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    // Resume if suspended (browser autoplay policy)
    if (_audioCtx.state === "suspended") {
      _audioCtx.resume();
    }
    return _audioCtx;
  }

  // ── Synthesized drum sounds ──────────────────────────────────────────────────
  function synthDrum(type, velocity) {
    const ctx  = getAudioCtx();
    const gain = velocity / 127;
    const now  = ctx.currentTime;

    switch (type) {
      case "kick": {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        osc.connect(env); env.connect(ctx.destination);
        osc.frequency.setValueAtTime(150, now);
        osc.frequency.exponentialRampToValueAtTime(40, now + 0.08);
        env.gain.setValueAtTime(gain, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
        osc.start(now); osc.stop(now + 0.35);
        break;
      }
      case "snare": {
        // White noise + tonal body
        const bufSize = ctx.sampleRate * 0.1;
        const buf     = ctx.createBuffer(1, bufSize, ctx.sampleRate);
        const data    = buf.getChannelData(0);
        for (let i = 0; i < bufSize; i++) data[i] = Math.random() * 2 - 1;
        const noise   = ctx.createBufferSource();
        const noiseEnv = ctx.createGain();
        noise.buffer = buf;
        noise.connect(noiseEnv); noiseEnv.connect(ctx.destination);
        noiseEnv.gain.setValueAtTime(gain * 0.8, now);
        noiseEnv.gain.exponentialRampToValueAtTime(0.001, now + 0.18);
        noise.start(now);

        const osc = ctx.createOscillator();
        const oscEnv = ctx.createGain();
        osc.connect(oscEnv); oscEnv.connect(ctx.destination);
        osc.frequency.setValueAtTime(200, now);
        osc.frequency.exponentialRampToValueAtTime(100, now + 0.06);
        oscEnv.gain.setValueAtTime(gain * 0.5, now);
        oscEnv.gain.exponentialRampToValueAtTime(0.001, now + 0.1);
        osc.start(now); osc.stop(now + 0.18);
        break;
      }
      case "rim": {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        osc.type = "square";
        osc.connect(env); env.connect(ctx.destination);
        osc.frequency.value = 800;
        env.gain.setValueAtTime(gain * 0.6, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.04);
        osc.start(now); osc.stop(now + 0.04);
        break;
      }
      case "hat_c": {
        const bufSize = ctx.sampleRate * 0.05;
        const buf     = ctx.createBuffer(1, bufSize, ctx.sampleRate);
        const data    = buf.getChannelData(0);
        for (let i = 0; i < bufSize; i++) data[i] = Math.random() * 2 - 1;
        const src  = ctx.createBufferSource();
        const filt = ctx.createBiquadFilter();
        const env  = ctx.createGain();
        src.buffer = buf;
        filt.type = "highpass"; filt.frequency.value = 8000;
        src.connect(filt); filt.connect(env); env.connect(ctx.destination);
        env.gain.setValueAtTime(gain * 0.4, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.05);
        src.start(now);
        break;
      }
      case "hat_o": {
        const bufSize = ctx.sampleRate * 0.25;
        const buf     = ctx.createBuffer(1, bufSize, ctx.sampleRate);
        const data    = buf.getChannelData(0);
        for (let i = 0; i < bufSize; i++) data[i] = Math.random() * 2 - 1;
        const src  = ctx.createBufferSource();
        const filt = ctx.createBiquadFilter();
        const env  = ctx.createGain();
        src.buffer = buf;
        filt.type = "highpass"; filt.frequency.value = 7000;
        src.connect(filt); filt.connect(env); env.connect(ctx.destination);
        env.gain.setValueAtTime(gain * 0.35, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
        src.start(now);
        break;
      }
      case "ride": {
        const osc  = ctx.createOscillator();
        const env  = ctx.createGain();
        osc.type = "sine";
        osc.connect(env); env.connect(ctx.destination);
        osc.frequency.value = 3200;
        env.gain.setValueAtTime(gain * 0.3, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.4);
        osc.start(now); osc.stop(now + 0.4);
        break;
      }
      case "crash": {
        const bufSize = ctx.sampleRate * 0.8;
        const buf     = ctx.createBuffer(1, bufSize, ctx.sampleRate);
        const data    = buf.getChannelData(0);
        for (let i = 0; i < bufSize; i++) data[i] = Math.random() * 2 - 1;
        const src  = ctx.createBufferSource();
        const filt = ctx.createBiquadFilter();
        const env  = ctx.createGain();
        src.buffer = buf;
        filt.type = "bandpass"; filt.frequency.value = 5000; filt.Q.value = 0.5;
        src.connect(filt); filt.connect(env); env.connect(ctx.destination);
        env.gain.setValueAtTime(gain * 0.5, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.8);
        src.start(now);
        break;
      }
      case "tom_h": {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        osc.connect(env); env.connect(ctx.destination);
        osc.frequency.setValueAtTime(300, now);
        osc.frequency.exponentialRampToValueAtTime(160, now + 0.1);
        env.gain.setValueAtTime(gain, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.2);
        osc.start(now); osc.stop(now + 0.2);
        break;
      }
      case "tom_m": {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        osc.connect(env); env.connect(ctx.destination);
        osc.frequency.setValueAtTime(220, now);
        osc.frequency.exponentialRampToValueAtTime(100, now + 0.12);
        env.gain.setValueAtTime(gain, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
        osc.start(now); osc.stop(now + 0.25);
        break;
      }
      case "tom_l": {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        osc.connect(env); env.connect(ctx.destination);
        osc.frequency.setValueAtTime(150, now);
        osc.frequency.exponentialRampToValueAtTime(60, now + 0.15);
        env.gain.setValueAtTime(gain, now);
        env.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
        osc.start(now); osc.stop(now + 0.3);
        break;
      }
    }
  }

  // ── Web MIDI output (Chrome/Edge desktop) ────────────────────────────────────
  let _midiOutput = null;

  async function initWebMidi() {
    if (!navigator.requestMIDIAccess) return false;
    try {
      const access = await navigator.requestMIDIAccess({ sysex: false });
      for (const [, output] of access.outputs) {
        if (output.name.toLowerCase().includes("bandmate") ||
            output.name.toLowerCase().includes("iac") ||
            output.name.toLowerCase().includes("loopmidi")) {
          _midiOutput = output;
          console.log(`[Bandmate] Web MIDI output: ${output.name}`);
          return true;
        }
      }
      // Use first available if no named match
      const outputs = [...access.outputs.values()];
      if (outputs.length > 0) {
        _midiOutput = outputs[0];
        console.log(`[Bandmate] Web MIDI fallback: ${outputs[0].name}`);
        return true;
      }
    } catch (e) {
      console.warn("[Bandmate] Web MIDI not available:", e);
    }
    return false;
  }

  function sendMidiNote(note, velocity, channel) {
    if (_midiOutput) {
      _midiOutput.send([0x90 | channel, note, velocity]);
      setTimeout(() => _midiOutput.send([0x80 | channel, note, 0]), 50);
    }
  }

  // ── Socket.IO connection ────────────────────────────────────────────────────
  let _socket = null;
  let _webMidiReady = false;
  let _useWebAudio  = true;  // always true as fallback

  async function initCloudClient() {
    _webMidiReady = await initWebMidi();

    // Load Socket.IO client from CDN if not already loaded
    if (!window.io) {
      await new Promise((resolve) => {
        const script    = document.createElement("script");
        script.src      = "https://cdn.socket.io/4.7.5/socket.io.min.js";
        script.onload   = resolve;
        script.onerror  = resolve;
        document.head.appendChild(script);
      });
    }

    if (!window.io) {
      console.error("[Bandmate] Socket.IO failed to load");
      return;
    }

    _socket = io(SOCKET_URL, { transports: ["websocket", "polling"] });

    _socket.on("connect", () => {
      console.log("[Bandmate] Connected to cloud server");
      updateCloudIndicator(true);
    });

    _socket.on("disconnect", () => {
      console.log("[Bandmate] Disconnected");
      updateCloudIndicator(false);
    });

    _socket.on("midi_hit", (data) => {
      const note     = data.note;
      const velocity = data.velocity || 100;
      const channel  = data.channel  || 9;

      // Web MIDI → Ableton
      if (_webMidiReady) {
        sendMidiNote(note, velocity, channel);
      }

      // Web Audio synth (always plays as monitor / for non-Ableton users)
      const params = SYNTH_PARAMS[note];
      if (params && _useWebAudio) {
        synthDrum(params.type, velocity);
      }
    });

    _socket.on("status", (data) => {
      // Dispatch a custom event so the main UI can react
      window.dispatchEvent(new CustomEvent("bandmate_status", { detail: data }));
    });
  }

  function updateCloudIndicator(connected) {
    // Update any element with id="cloud-status"
    const el = document.getElementById("cloud-status");
    if (el) {
      el.textContent = connected ? "☁️ CLOUD" : "☁️ RECONNECTING...";
      el.style.color = connected ? "#00ff88" : "#ff8800";
    }
  }

  // ── Public API ──────────────────────────────────────────────────────────────
  window.BandmateCloud = {
    init:          initCloudClient,
    setWebAudio:   (v) => { _useWebAudio = v; },
    getSocket:     () => _socket,
    synthDrum,
    isMidiReady:   () => _webMidiReady,
    isConnected:   () => _socket && _socket.connected,
  };

  // ── Auto-init on DOM ready (cloud deployments only) ─────────────────────────
  if (CLOUD_MODE) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initCloudClient);
    } else {
      initCloudClient();
    }
  }

})();
