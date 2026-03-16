#!/usr/bin/env python3
"""
midi_bridge.py — Bandmate Cloud → Ableton MIDI Bridge
Run this on your LOCAL machine (Mac/PC/Pi), NOT on the server.

Connects to the cloud Bandmate server via WebSocket.
Receives drum hit events and forwards them as MIDI note-on messages
to a virtual MIDI port that Ableton Live (or any DAW) can see.

Usage:
  python midi_bridge.py --server https://your-app.railway.app
  python midi_bridge.py --server https://your-app.railway.app --port "Bandmate Drums"

Setup:
  Mac:     IAC Driver is built-in. Enable it in Audio MIDI Setup → MIDI Studio.
           The bridge will create a port named "Bandmate Drums" automatically.
  Windows: Install loopMIDI (free) — https://www.tobias-erichsen.de/software/loopmidi.html
           Create a port named "Bandmate Drums" in loopMIDI, then run this script.
  Linux:   Uses ALSA virtual port. Run: modprobe snd-virmidi first if needed.

Ableton setup:
  Preferences → Link/Tempo/MIDI → MIDI Ports
  Enable "Track" on "Bandmate Drums" Input
  Drop a MIDI instrument (e.g. Drum Rack) on a track
  Set track MIDI input to "Bandmate Drums" / All Channels
  Hit play on the Bandmate UI — drums appear in Ableton on channel 10

Install:
  pip install python-socketio[client] python-rtmidi websocket-client
"""

import argparse
import time
import sys

try:
    import socketio as sio_client
except ImportError:
    print("❌  python-socketio not installed.")
    print("    pip install 'python-socketio[client]' websocket-client")
    sys.exit(1)

try:
    import rtmidi
except ImportError:
    print("❌  python-rtmidi not installed.")
    print("    pip install python-rtmidi")
    sys.exit(1)


# ── MIDI note-off delay (ms) — how long a drum note rings ──────────────────────
NOTE_OFF_DELAY = 0.05   # 50ms — short enough not to interfere with next hit

# ── GM drum note map (mirrors server) ────────────────────────────────────────
MIDI_NOTES = {
    "kick":       36,
    "snare":      38,
    "snare_rim":  37,
    "snare_alt":  40,
    "hat_closed": 42,
    "hat_open":   46,
    "hat_pedal":  44,
    "ride":       51,
    "ride_bell":  53,
    "crash":      49,
    "crash_alt":  57,
    "tom_high":   50,
    "tom_high2":  48,
    "tom_mid":    47,
    "tom_low":    43,
    "bass_drop":  35,
    "perc":       56,
}


class MidiBridge:
    def __init__(self, server_url, port_name="Bandmate Drums"):
        self.server_url = server_url.rstrip("/")
        self.port_name  = port_name
        self.midi_out   = None
        self.sio        = sio_client.Client(reconnection=True, reconnection_attempts=0,
                                             reconnection_delay=2)
        self._setup_midi()
        self._setup_socket()

    def _setup_midi(self):
        """Open a virtual MIDI output port."""
        self.midi_out = rtmidi.MidiOut()
        available = self.midi_out.get_ports()
        print(f"\n  Available MIDI ports: {available or ['(none)']}")

        # Try to find an existing port matching our name
        for i, name in enumerate(available):
            if self.port_name.lower() in name.lower():
                self.midi_out.open_port(i)
                print(f"  ✅ Opened existing MIDI port: {name}")
                return

        # Create a virtual port (Mac/Linux only — Windows needs loopMIDI)
        try:
            self.midi_out.open_virtual_port(self.port_name)
            print(f"  ✅ Created virtual MIDI port: '{self.port_name}'")
        except Exception as e:
            print(f"  ⚠️  Could not create virtual port: {e}")
            print(f"      On Windows: create a port named '{self.port_name}' in loopMIDI first.")
            if available:
                print(f"      Falling back to first available port: {available[0]}")
                self.midi_out.open_port(0)
            else:
                print("  ❌  No MIDI ports available. Install loopMIDI (Windows) or enable IAC Driver (Mac).")
                sys.exit(1)

    def _note_on(self, note, velocity, channel=9):
        """Send MIDI note-on on channel 10 (9 zero-indexed = drums)."""
        if self.midi_out:
            self.midi_out.send_message([0x90 | channel, note, velocity])

    def _note_off(self, note, channel=9):
        if self.midi_out:
            self.midi_out.send_message([0x80 | channel, note, 0])

    def _hit(self, note, velocity, channel=9):
        """Note-on then schedule note-off."""
        import threading
        self._note_on(note, velocity, channel)
        threading.Timer(NOTE_OFF_DELAY, self._note_off, args=(note, channel)).start()

    def _setup_socket(self):
        sio = self.sio

        @sio.event
        def connect():
            print(f"  ✅ Connected to {self.server_url}")

        @sio.event
        def disconnect():
            print("  ⚠️  Disconnected — will reconnect...")

        @sio.on("midi_hit")
        def on_midi_hit(data):
            note     = data.get("note")
            velocity = data.get("velocity", 100)
            channel  = data.get("channel", 9)
            voice    = data.get("voice", "?")
            if note is not None:
                self._hit(int(note), int(velocity), int(channel))
                print(f"  🥁  {voice:<14} note={note:<4} vel={velocity}", end="\r")

        @sio.on("status")
        def on_status(data):
            if data.get("playing"):
                section = data.get("section", "")
                bpm     = data.get("bpm", 0)
                genre   = data.get("genre", "")
                # Print on a new line so it doesn't stomp hit display
                print(f"\n  ▶  {genre.upper()} | {section.upper()} | {bpm:.0f} BPM        ")

    def run(self):
        print(f"\n╔══════════════════════════════════════════════╗")
        print(f"║     BANDMATE → ABLETON MIDI BRIDGE  🎹       ║")
        print(f"╚══════════════════════════════════════════════╝")
        print(f"\n  Server     : {self.server_url}")
        print(f"  MIDI port  : {self.port_name}")
        print(f"  Channel    : 10 (GM drums)")
        print(f"\n  Ableton: Preferences → MIDI → enable '{self.port_name}' as Input")
        print(f"  Drop a Drum Rack on a track, set input to '{self.port_name}'")
        print(f"\n  Ctrl+C to stop\n")

        try:
            self.sio.connect(self.server_url, transports=["websocket", "polling"])
            self.sio.wait()
        except KeyboardInterrupt:
            print("\n\n  Bridge stopped.\n")
        finally:
            if self.midi_out:
                del self.midi_out


def main():
    ap = argparse.ArgumentParser(description="Bandmate Cloud → Ableton MIDI Bridge")
    ap.add_argument("--server", required=True,
                    help="Cloud server URL e.g. https://bandmate.railway.app")
    ap.add_argument("--port",   default="Bandmate Drums",
                    help="Virtual MIDI port name (default: 'Bandmate Drums')")
    ap.add_argument("--channel", type=int, default=9,
                    help="MIDI channel (0-indexed, default 9 = channel 10 = GM drums)")
    args = ap.parse_args()

    bridge = MidiBridge(server_url=args.server, port_name=args.port)
    bridge.run()


if __name__ == "__main__":
    main()
