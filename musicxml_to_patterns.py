#!/usr/bin/env python3
"""
musicxml_to_patterns.py — MusicXML / MSCZ Drum Parser for Pi Bandmate

Parses drum parts from MusicXML or MuseScore (.mscz) files and adds
the patterns to pattern_library.json in the exact format the groove
engine expects.

Supports:
  - .musicxml / .xml files (direct MusicXML)
  - .mscz files (MuseScore native — just a zip containing MusicXML)
  - Time signatures: 4/4, 3/4, 5/4, 6/8, 7/4, 7/8, 9/8, 12/8
  - All GM drum note numbers
  - MuseScore display-step/octave fallback mapping
  - Multi-measure section detection (verse/chorus heuristic)
  - Merge mode: adds patterns to existing library without overwriting

Usage:
  # Parse a file and add to pattern_library.json
  python3 musicxml_to_patterns.py song.musicxml --genre rock --section verse

  # Auto-detect section from rehearsal marks in the score
  python3 musicxml_to_patterns.py song.musicxml --genre rock --auto-sections

  # Dry run — print patterns without saving
  python3 musicxml_to_patterns.py song.musicxml --genre rock --dry-run

  # Parse .mscz directly
  python3 musicxml_to_patterns.py song.mscz --genre funk --auto-sections

  # Specify custom pattern library path
  python3 musicxml_to_patterns.py song.musicxml --genre rock \\
      --library ~/mpc_ai/pattern_library.json

Install:
  pip install lxml --break-system-packages   (or: pip install lxml)
  No other dependencies beyond Python stdlib.
"""

import argparse
import json
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET


# ── GM Drum Map: MIDI note number → Bandmate voice name ──────────────────────
GM_DRUM_MAP = {
    35: "kick",       # Acoustic Bass Drum
    36: "kick",       # Bass Drum 1
    37: "snare_rim",  # Side Stick
    38: "snare",      # Acoustic Snare
    39: "snare_rim",  # Hand Clap → rim
    40: "snare",      # Electric Snare
    41: "tom_low",    # Low Floor Tom
    42: "hat_closed", # Closed Hi-Hat
    43: "tom_low",    # High Floor Tom
    44: "hat_pedal",  # Pedal Hi-Hat → closed hat
    45: "tom_mid",    # Low Tom
    46: "hat_open",   # Open Hi-Hat
    47: "tom_mid",    # Low-Mid Tom
    48: "tom_high",   # Hi-Mid Tom
    49: "crash",      # Crash Cymbal 1
    50: "tom_high",   # High Tom
    51: "ride",       # Ride Cymbal 1
    52: "crash",      # Chinese Cymbal → crash
    53: "ride_bell",  # Ride Bell → ride
    54: "hat_open",   # Tambourine → open hat
    55: "crash",      # Splash → crash
    56: "perc",       # Cowbell → perc
    57: "crash",      # Crash Cymbal 2
    58: "tom_low",    # Vibraslap → tom
    59: "ride",       # Ride Cymbal 2
    60: "perc",       # Hi Bongo
    61: "perc",       # Low Bongo
    62: "perc",       # Mute Hi Conga
    63: "perc",       # Open Hi Conga
    64: "perc",       # Low Conga
    65: "perc",       # Hi Timbale
    66: "perc",       # Low Timbale
}

# ── MuseScore display-step/octave → MIDI note (fallback when no MIDI tag) ─────
# Based on MuseScore 4's default drum input map
DISPLAY_TO_MIDI = {
    ("B", 4): 35,   # Acoustic Bass Drum
    ("C", 5): 36,   # Bass Drum 1  (most common kick position)
    ("C#",5): 37,   # Side Stick
    ("D", 5): 38,   # Acoustic Snare
    ("D#",5): 39,   # Hand Clap
    ("E", 5): 40,   # Electric Snare
    ("F", 5): 42,   # Closed Hi-Hat  ← MuseScore default drum input position for hat
    ("F#",5): 41,   # Low Floor Tom
    ("G", 5): 43,   # High Floor Tom
    ("G#",5): 44,   # Pedal Hi-Hat
    ("A", 5): 45,   # Low Tom
    ("A#",5): 46,   # Open Hi-Hat
    ("B", 5): 47,   # Low-Mid Tom
    ("C", 6): 48,   # Hi-Mid Tom
    ("C#",6): 49,   # Crash Cymbal 1
    ("D", 6): 50,   # High Tom
    ("D#",6): 51,   # Ride Cymbal 1
    ("E", 6): 52,   # Chinese Cymbal
    ("F", 6): 53,   # Ride Bell
    ("F#",6): 54,   # Tambourine
    ("G", 6): 55,   # Splash Cymbal
    ("G#",6): 56,   # Cowbell
    ("A", 6): 57,   # Crash Cymbal 2
    ("A#",6): 58,   # Vibraslap
    ("B", 6): 59,   # Ride Cymbal 2
    # Some MuseScore versions use different octave placements for cymbals
    ("A", 4): 42,   # Alt closed hi-hat position
    ("G", 4): 42,   # Alt closed hi-hat
    ("C", 7): 49,   # Alt crash position
    ("D", 7): 51,   # Alt ride position
}

# ── Voice → Bandmate pattern track key ────────────────────────────────────────
VOICE_TO_KEY = {
    "kick":       "K",
    "snare":      "S",
    "snare_rim":  "SR",
    "hat_closed": "H",
    "hat_open":   "OH",
    "hat_pedal":  "H",   # pedal hat → closed hat track
    "ride":       "R",
    "ride_bell":  "R",
    "crash":      "C",
    "tom_high":   "T",
    "tom_high2":  "T",
    "tom_mid":    "T",
    "tom_low":    "T",
    "bass_drop":  "B",
    "perc":       "G",
}

# ── Supported time signature → steps per bar ──────────────────────────────────
TIME_SIG_STEPS = {
    (4, 4): 16,
    (3, 4): 12,
    (5, 4): 20,
    (6, 8): 12,
    (7, 4): 28,
    (7, 8): 14,
    (9, 8): 18,
    (12,8): 24,
    (2, 4): 8,
    (2, 2): 8,
}

# ── Section name normaliser ────────────────────────────────────────────────────
SECTION_ALIASES = {
    "intro":       "intro",
    "introduction":"intro",
    "pre-chorus":  "verse",
    "pre chorus":  "verse",
    "prechorus":   "verse",
    "verse":       "verse",
    "v":           "verse",
    "chorus":      "chorus",
    "ch":          "chorus",
    "chrs":        "chorus",
    "refrain":     "chorus",
    "bridge":      "bridge",
    "br":          "bridge",
    "breakdown":   "bridge",
    "fill":        "fill",
    "outro":       "outro",
    "coda":        "outro",
    "ending":      "outro",
    "solo":        "verse",   # solos usually over verse changes
    "interlude":   "bridge",
}

VALID_SECTIONS = {"intro", "verse", "chorus", "bridge", "fill", "outro"}


def normalise_section(text: str) -> str | None:
    """Map a rehearsal mark / text to a valid section name."""
    t = text.strip().lower()
    # Direct match
    if t in SECTION_ALIASES:
        return SECTION_ALIASES[t]
    # Prefix match
    for alias, section in SECTION_ALIASES.items():
        if t.startswith(alias):
            return section
    return None


# ── XML namespace helper ───────────────────────────────────────────────────────
def strip_ns(tag: str) -> str:
    """Remove namespace prefix from an XML tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def iter_no_ns(element, tag: str):
    """Iterate children matching tag, ignoring namespaces."""
    for child in element:
        if strip_ns(child.tag) == tag:
            yield child


def find_no_ns(element, tag: str):
    """Find first child matching tag, ignoring namespaces."""
    for child in element:
        if strip_ns(child.tag) == tag:
            return child
    return None


# ── MSCZ loader ───────────────────────────────────────────────────────────────
def load_xml_from_file(path: Path) -> ET.Element:
    """Load MusicXML from .musicxml/.xml or .mscz (zip) file."""
    if path.suffix.lower() == ".mscz":
        with zipfile.ZipFile(path, "r") as z:
            # Find the .mscx file inside
            mscx_files = [n for n in z.namelist() if n.endswith(".mscx")]
            if not mscx_files:
                raise ValueError(f"No .mscx file found inside {path}")
            with z.open(mscx_files[0]) as f:
                return ET.parse(f).getroot()
    else:
        return ET.parse(path).getroot()


# ── Percussion part detector ───────────────────────────────────────────────────
def find_drum_part_ids(root: ET.Element) -> list[str]:
    """Return part IDs that are percussion/drum parts."""
    drum_ids = []

    # Check score-part entries in part-list
    part_list = find_no_ns(root, "part-list")
    if part_list is None:
        # MuseScore .mscx uses different structure — return all part ids
        return [p.get("id", "") for p in root.iter() if strip_ns(p.tag) == "Part"]

    for score_part in iter_no_ns(part_list, "score-part"):
        part_id = score_part.get("id", "")
        is_drum = False

        # Check part name
        name_el = find_no_ns(score_part, "part-name")
        if name_el is not None and name_el.text:
            name_lower = name_el.text.lower()
            if any(w in name_lower for w in ("drum", "perc", "kit", "snare", "bass drum", "cymbal")):
                is_drum = True

        # Check midi-channel = 10 (9 zero-indexed) or midi-unpitched
        for midi_inst in score_part.iter():
            tag = strip_ns(midi_inst.tag)
            if tag == "midi-channel":
                if midi_inst.text and int(midi_inst.text) in (9, 10):
                    is_drum = True
            if tag == "midi-unpitched":
                is_drum = True

        if is_drum:
            drum_ids.append(part_id)

    # Fallback: if no drum parts found by name/channel, check first measure
    # for a percussion clef
    if not drum_ids:
        for part in iter_no_ns(root, "part"):
            for measure in iter_no_ns(part, "measure"):
                for attrs in iter_no_ns(measure, "attributes"):
                    for clef in iter_no_ns(attrs, "clef"):
                        sign = find_no_ns(clef, "sign")
                        if sign is not None and sign.text == "percussion":
                            drum_ids.append(part.get("id", ""))
                break

    return drum_ids


# ── Core parser ───────────────────────────────────────────────────────────────
class MusicXMLDrumParser:
    def __init__(self, path: Path, verbose: bool = False):
        self.path    = path
        self.verbose = verbose
        self.root    = load_xml_from_file(path)
        self.title   = self._get_title()

    def _get_title(self) -> str:
        for el in self.root.iter():
            if strip_ns(el.tag) in ("work-title", "movement-title", "title"):
                if el.text and el.text.strip():
                    return el.text.strip()
        return self.path.stem

    def _note_to_midi(self, note_el: ET.Element) -> int | None:
        """Extract MIDI note number from a <note> element."""

        # Method 1: explicit midi-instrument unpitched value
        # <notations><technical><fret>36</fret>... (some exporters)
        for tech in note_el.iter():
            if strip_ns(tech.tag) == "fret" and tech.text:
                try:
                    return int(tech.text)
                except ValueError:
                    pass

        # Method 2: midi-unpitched in score-instrument (rare, but valid)
        # Usually set at the part level; we handle it via instrument ID lookup
        # Skip here — handled in _build_instrument_map

        # Method 3: display-step + display-octave → MIDI via lookup table
        unpitched = find_no_ns(note_el, "unpitched")
        if unpitched is not None:
            step_el  = find_no_ns(unpitched, "display-step")
            oct_el   = find_no_ns(unpitched, "display-octave")
            if step_el is not None and oct_el is not None:
                step = step_el.text.strip().upper() if step_el.text else ""
                try:
                    octave = int(oct_el.text)
                    midi = DISPLAY_TO_MIDI.get((step, octave))
                    if midi:
                        return midi
                except (ValueError, TypeError):
                    pass

        # Method 4: pitch + instrument id (MuseScore .mscx format uses <pitch>)
        pitch_el = find_no_ns(note_el, "pitch")
        if pitch_el is not None:
            step_el = find_no_ns(pitch_el, "step")
            oct_el  = find_no_ns(pitch_el, "octave")
            alt_el  = find_no_ns(pitch_el, "alter")
            if step_el is not None and oct_el is not None:
                step = step_el.text.strip().upper() if step_el.text else ""
                if alt_el is not None and alt_el.text:
                    try:
                        alter = int(float(alt_el.text))
                        if alter == 1:
                            step += "#"
                        elif alter == -1:
                            step += "b"
                    except ValueError:
                        pass
                try:
                    octave = int(oct_el.text)
                    midi = DISPLAY_TO_MIDI.get((step, octave))
                    if midi:
                        return midi
                    # Try without accidental
                    base_step = step.replace("#", "").replace("b", "")
                    midi = DISPLAY_TO_MIDI.get((base_step, octave))
                    if midi:
                        return midi
                except (ValueError, TypeError):
                    pass

        return None

    def _build_instrument_map(self, part_el: ET.Element) -> dict[str, int]:
        """Build instrument-id → MIDI note map from score-instrument tags."""
        inst_map = {}
        # These live in part-list → score-part, but we scan the part element too
        for el in part_el.iter():
            if strip_ns(el.tag) == "midi-instrument":
                inst_id   = el.get("id", "")
                unpitched = find_no_ns(el, "midi-unpitched")
                if unpitched is not None and unpitched.text:
                    try:
                        inst_map[inst_id] = int(unpitched.text)
                    except ValueError:
                        pass
        return inst_map

    def parse(self, target_section: str = "verse", auto_sections: bool = False
              ) -> dict[str, list[dict]]:
        """
        Parse the drum part and return patterns grouped by section.

        Returns:
            dict mapping section name → list of pattern dicts
            Each pattern dict has keys like K, S, H, OH, R, C, SR, T, G
        """
        drum_ids = find_drum_part_ids(self.root)
        if not drum_ids:
            raise ValueError(
                "No percussion/drum part found in this file.\n"
                "Make sure the score has a drum track with a percussion clef\n"
                "or MIDI channel 10."
            )

        if self.verbose:
            print(f"  Found drum part(s): {drum_ids}")

        results: dict[str, list[dict]] = defaultdict(list)

        for part in iter_no_ns(self.root, "part"):
            part_id = part.get("id", "")
            if drum_ids and part_id not in drum_ids:
                continue

            inst_map    = self._build_instrument_map(part)
            time_sig    = (4, 4)
            divisions   = 4       # MusicXML divisions = ticks per quarter note
            current_section = target_section
            measure_count   = 0

            # Collect (section, pattern) tuples, one pattern per measure
            measure_patterns: list[tuple[str, dict]] = []

            for measure in iter_no_ns(part, "measure"):
                measure_count += 1

                # ── Update time signature ──────────────────────────────────────
                for attrs in iter_no_ns(measure, "attributes"):
                    div_el = find_no_ns(attrs, "divisions")
                    if div_el is not None and div_el.text:
                        try:
                            divisions = int(div_el.text)
                        except ValueError:
                            pass
                    for time_el in iter_no_ns(attrs, "time"):
                        beats_el = find_no_ns(time_el, "beats")
                        btype_el = find_no_ns(time_el, "beat-type")
                        if beats_el is not None and btype_el is not None:
                            try:
                                time_sig = (int(beats_el.text), int(btype_el.text))
                            except (ValueError, TypeError):
                                pass

                steps = TIME_SIG_STEPS.get(time_sig, 16)
                ticks_per_bar = divisions * time_sig[0] * (4 / time_sig[1])
                ticks_per_step = ticks_per_bar / steps

                # ── Detect section from rehearsal mark / text ──────────────────
                if auto_sections:
                    for direction in iter_no_ns(measure, "direction"):
                        for direction_type in iter_no_ns(direction, "direction-type"):
                            for el in direction_type:
                                tag = strip_ns(el.tag)
                                if tag in ("rehearsal", "words") and el.text:
                                    mapped = normalise_section(el.text)
                                    if mapped:
                                        current_section = mapped
                                        if self.verbose:
                                            print(f"  Measure {measure_count}: section → {mapped} (from '{el.text}')")
                    # Also check measure text in MuseScore .mscx
                    for text_el in measure.iter():
                        if strip_ns(text_el.tag) == "text" and text_el.text:
                            mapped = normalise_section(text_el.text)
                            if mapped:
                                current_section = mapped

                # ── Parse notes in this measure ────────────────────────────────
                pattern = {key: [0] * steps for key in ("K", "S", "SR", "H", "OH", "R", "C", "T", "G", "B")}
                current_tick = 0
                has_hits = False

                for child in measure:
                    tag = strip_ns(child.tag)

                    if tag == "backup":
                        dur_el = find_no_ns(child, "duration")
                        if dur_el is not None and dur_el.text:
                            try:
                                current_tick -= int(dur_el.text)
                            except ValueError:
                                pass
                        continue

                    if tag == "forward":
                        dur_el = find_no_ns(child, "duration")
                        if dur_el is not None and dur_el.text:
                            try:
                                current_tick += int(dur_el.text)
                            except ValueError:
                                pass
                        continue

                    if tag != "note":
                        continue

                    # Skip rests
                    if find_no_ns(child, "rest") is not None:
                        dur_el = find_no_ns(child, "duration")
                        if dur_el is not None and dur_el.text:
                            try:
                                current_tick += int(dur_el.text)
                            except ValueError:
                                pass
                        continue

                    # Check chord — if this note is a chord member, don't advance tick
                    is_chord = find_no_ns(child, "chord") is not None

                    # Get duration
                    dur_el = find_no_ns(child, "duration")
                    duration = 0
                    if dur_el is not None and dur_el.text:
                        try:
                            duration = int(dur_el.text)
                        except ValueError:
                            pass

                    # Get MIDI note
                    midi_note = self._note_to_midi(child)

                    # Try instrument id lookup
                    if midi_note is None:
                        inst_el = find_no_ns(child, "instrument")
                        if inst_el is not None:
                            inst_id  = inst_el.get("id", "")
                            midi_note = inst_map.get(inst_id)

                    if midi_note is not None:
                        voice = GM_DRUM_MAP.get(midi_note)
                        if voice:
                            track_key = VOICE_TO_KEY.get(voice)
                            if track_key:
                                # Convert tick position to 16th-note step
                                step_idx = int(round(current_tick / ticks_per_step))
                                step_idx = max(0, min(steps - 1, step_idx))
                                pattern[track_key][step_idx] = 1
                                has_hits = True
                                if self.verbose:
                                    print(f"    measure {measure_count} tick {current_tick} "
                                          f"step {step_idx}/{steps} midi={midi_note} "
                                          f"voice={voice} → {track_key}")

                    if not is_chord:
                        current_tick += duration

                # ── Trim empty tracks, store measure if it has hits ────────────
                if has_hits:
                    trimmed = {k: v for k, v in pattern.items() if any(v)}
                    measure_patterns.append((current_section, trimmed))

            # ── Deduplicate identical consecutive patterns ─────────────────────
            deduped: list[tuple[str, dict]] = []
            for section, pat in measure_patterns:
                if deduped and deduped[-1][0] == section and deduped[-1][1] == pat:
                    continue
                deduped.append((section, pat))

            # ── Group by section ──────────────────────────────────────────────
            for section, pat in deduped:
                if section not in VALID_SECTIONS:
                    section = target_section
                if pat not in results[section]:  # avoid exact duplicates
                    results[section].append(pat)

        return dict(results)


# ── Pattern library merge ──────────────────────────────────────────────────────
def merge_into_library(
    library_path: Path,
    genre: str,
    new_patterns: dict[str, list[dict]],
    replace: bool = False,
) -> dict:
    """
    Merge new_patterns into pattern_library.json.
    replace=True overwrites existing patterns for this genre/section.
    replace=False appends (default).
    """
    library = {}
    if library_path.exists():
        try:
            library = json.loads(library_path.read_text())
        except json.JSONDecodeError:
            print(f"  ⚠️  Could not parse existing library — starting fresh.")

    if genre not in library:
        library[genre] = {}

    for section, patterns in new_patterns.items():
        if section not in library[genre] or replace:
            library[genre][section] = patterns
        else:
            # Append, avoiding exact duplicates
            existing = library[genre][section]
            for p in patterns:
                if p not in existing:
                    existing.append(p)

    return library


# ── Pretty-print a pattern ────────────────────────────────────────────────────
def print_pattern(section: str, pat: dict, steps: int = 16):
    print(f"\n  [{section.upper()}]")
    for key in ("K", "S", "SR", "H", "OH", "R", "C", "T", "G", "B"):
        if key in pat:
            bar = "".join("█" if v else "·" for v in pat[key])
            # Add bar dividers every 4 steps
            bar_str = " ".join(bar[i:i+4] for i in range(0, len(bar), 4))
            print(f"    {key:<3} {bar_str}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Parse MusicXML / MSCZ drum parts → Bandmate pattern_library.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 musicxml_to_patterns.py beat.musicxml --genre rock
  python3 musicxml_to_patterns.py song.mscz --genre funk --auto-sections
  python3 musicxml_to_patterns.py beat.xml --genre jazz --section chorus --dry-run
  python3 musicxml_to_patterns.py beat.musicxml --genre metal --replace
        """
    )
    ap.add_argument("file",
                    help="MusicXML (.musicxml / .xml) or MuseScore (.mscz) file")
    ap.add_argument("--genre", required=True,
                    choices=["blues","dub","drill","funk","hiphop","house","jazz",
                             "metal","pop","reggae","rock","surf","trap"],
                    help="Genre to store patterns under")
    ap.add_argument("--section", default="verse",
                    choices=list(VALID_SECTIONS),
                    help="Section to assign patterns to (used when --auto-sections is off)")
    ap.add_argument("--auto-sections", action="store_true",
                    help="Auto-detect sections from rehearsal marks in the score")
    ap.add_argument("--library", default=None,
                    help="Path to pattern_library.json (default: ~/mpc_ai/pattern_library.json)")
    ap.add_argument("--replace", action="store_true",
                    help="Replace existing patterns for this genre/section instead of appending")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and display patterns without saving")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Show detailed parsing info")
    ap.add_argument("--max-patterns", type=int, default=8,
                    help="Max patterns to import per section (default: 8)")
    args = ap.parse_args()

    # ── Resolve paths ──────────────────────────────────────────────────────────
    input_path = Path(args.file)
    if not input_path.exists():
        print(f"❌  File not found: {input_path}")
        sys.exit(1)

    library_path = Path(args.library) if args.library else \
                   Path.home() / "mpc_ai" / "pattern_library.json"

    # ── Parse ─────────────────────────────────────────────────────────────────
    print(f"\n  Pi Bandmate — MusicXML Drum Parser")
    print(f"  {'─'*40}")
    print(f"  File    : {input_path.name}")
    print(f"  Genre   : {args.genre}")
    print(f"  Sections: {'auto-detect' if args.auto_sections else args.section}")
    print()

    try:
        parser = MusicXMLDrumParser(input_path, verbose=args.verbose)
        print(f"  Title   : {parser.title}")
        patterns = parser.parse(
            target_section=args.section,
            auto_sections=args.auto_sections,
        )
    except Exception as e:
        print(f"\n❌  Parse error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    if not patterns:
        print("\n⚠️  No drum patterns found in this file.")
        print("   Check that the file has a drum/percussion track.")
        sys.exit(1)

    # ── Trim to max_patterns ───────────────────────────────────────────────────
    trimmed = {}
    total_found = 0
    for section, pats in patterns.items():
        total_found += len(pats)
        trimmed[section] = pats[:args.max_patterns]

    # ── Display ───────────────────────────────────────────────────────────────
    total_kept = sum(len(v) for v in trimmed.values())
    print(f"\n  Found {total_found} unique pattern(s) across {len(trimmed)} section(s)")
    if total_found > total_kept:
        print(f"  Keeping {total_kept} (use --max-patterns to adjust)")

    for section, pats in trimmed.items():
        for i, pat in enumerate(pats):
            print_pattern(f"{section} #{i+1}", pat)

    print()

    # ── Save ─────────────────────────────────────────────────────────────────
    if args.dry_run:
        print("  ℹ️  Dry run — nothing saved.")
        return

    library = merge_into_library(library_path, args.genre, trimmed, replace=args.replace)
    library_path.parent.mkdir(parents=True, exist_ok=True)
    library_path.write_text(json.dumps(library, indent=2))

    total_in_library = sum(
        len(pats)
        for sections in library.get(args.genre, {}).values()
        for pats in [sections] if isinstance(sections, list)
    )
    print(f"  ✅ Saved to {library_path}")
    print(f"     Genre '{args.genre}' now has patterns in: "
          f"{', '.join(library.get(args.genre, {}).keys())}")
    print()


# ── Flask route (add to bandmate_server.py / bandmate_server_cloud.py) ────────
def make_flask_route(library_path: Path):
    """
    Returns a Flask route function for /api/patterns/import
    Add to your server with: app.add_url_rule('/api/patterns/import', ...)

    Usage from UI:
      POST /api/patterns/import
      Content-Type: multipart/form-data
      Fields: file (musicxml/mscz), genre, section (optional), auto_sections (bool)
    """
    def api_import_patterns():
        from flask import request, jsonify
        import tempfile

        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file uploaded"}), 400

        genre = request.form.get("genre", "rock")
        section = request.form.get("section", "verse")
        auto_sections = request.form.get("auto_sections", "false").lower() == "true"
        replace = request.form.get("replace", "false").lower() == "true"

        suffix = Path(f.filename).suffix.lower() if f.filename else ".musicxml"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = Path(tmp.name)

        try:
            parser = MusicXMLDrumParser(tmp_path)
            patterns = parser.parse(target_section=section, auto_sections=auto_sections)
            if not patterns:
                return jsonify({"error": "No drum patterns found in file"}), 400

            library = merge_into_library(library_path, genre, patterns, replace=replace)
            library_path.parent.mkdir(parents=True, exist_ok=True)
            library_path.write_text(json.dumps(library, indent=2))

            total = sum(len(v) for v in patterns.values())
            return jsonify({
                "ok":       True,
                "title":    parser.title,
                "genre":    genre,
                "sections": {k: len(v) for k, v in patterns.items()},
                "total_patterns": total,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            tmp_path.unlink(missing_ok=True)

    return api_import_patterns


if __name__ == "__main__":
    main()
