#!/usr/bin/env python3
import argparse
import base64
import csv
import curses
import glob
import json
import locale
import os
import re
import socket
import struct
import subprocess
import sys
import urllib.parse
from datetime import datetime, date, timedelta
from collections import defaultdict

FPS = 30

CORE_APPS = "/opt/roboforce-core-apps"
LOCAL_CORE_APPS = os.path.expanduser("~/Workspace/roboforce-core-apps")

MCAP_MAGIC = b"\x89MCAP0\r\n"
MCAP_OP_FOOTER = 0x02
MCAP_OP_MESSAGE_INDEX = 0x07
MCAP_OP_CHUNK_INDEX = 0x08
MCAP_OP_STATISTICS = 0x0B
MCAP_FOOTER_SIZE = 9 + 20  # opcode + length prefix + payload

# Publishers occasionally stamp their first message from a monotonic clock instead of
# system time, leaving a stray ~1e15 log_time (a few days of uptime) among real epoch
# nanoseconds. Such a stamp poisons the summary's message_start_time. Anything below
# this floor (~2001-09-09) cannot be an epoch-ns stamp for a recording made today.
MCAP_CLOCK_FLOOR_NS = 10**18


def mcap_message_index_min(f, offset, floor):
    """Return the smallest log_time >= floor in the MessageIndex record at offset, or None."""
    f.seek(offset)
    op, length = struct.unpack("<BQ", f.read(9))
    if op != MCAP_OP_MESSAGE_INDEX:
        return None
    payload = f.read(length)
    # uint16 channel_id, then a uint32-byte-length-prefixed array of (uint64 log_time, uint64 offset)
    (array_bytes,) = struct.unpack("<I", payload[2:6])
    smallest = None
    for i in range(6, min(6 + array_bytes, len(payload) - 15), 16):
        (log_time,) = struct.unpack("<Q", payload[i:i + 8])
        if log_time >= floor and (smallest is None or log_time < smallest):
            smallest = log_time
    return smallest


def mcap_span(path):
    """Return (start_ns, end_ns) covering an MCAP file's messages, or None.

    Reads only the footer, summary section, and -- when a chunk's start stamp is
    implausible -- that chunk's message indexes, so cost is independent of file size.
    Returns None for any file that is not a complete, non-empty MCAP; callers fall
    back to another duration source. Recordings still being written have no footer.
    """
    try:
        size = os.path.getsize(path)
        if size < 2 * len(MCAP_MAGIC) + MCAP_FOOTER_SIZE:
            return None
        with open(path, "rb") as f:
            if f.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
                return None
            f.seek(-len(MCAP_MAGIC), os.SEEK_END)
            if f.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
                return None

            records_end = size - len(MCAP_MAGIC) - MCAP_FOOTER_SIZE
            f.seek(records_end)
            op, length = struct.unpack("<BQ", f.read(9))
            if op != MCAP_OP_FOOTER or length != 20:
                return None
            summary_start = struct.unpack("<Q", f.read(8))[0]
            if not len(MCAP_MAGIC) <= summary_start < records_end:
                return None

            stats = None
            chunks = []
            f.seek(summary_start)
            while f.tell() < records_end:
                header = f.read(9)
                if len(header) < 9:
                    return None
                op, length = struct.unpack("<BQ", header)
                if op == MCAP_OP_STATISTICS:
                    if length < 42:
                        return None
                    # uint64 message_count, uint16 schema_count, 4x uint32 counts,
                    # then uint64 message_start_time, uint64 message_end_time
                    stats = struct.unpack("<QQ", f.read(42)[26:42])
                    f.seek(length - 42, os.SEEK_CUR)
                elif op == MCAP_OP_CHUNK_INDEX:
                    if length < 36:
                        return None
                    payload = f.read(length)
                    start, end = struct.unpack("<QQ", payload[0:16])
                    # skip chunk_start_offset and chunk_length, then the
                    # uint32-byte-length-prefixed map of channel_id -> message index offset
                    (map_bytes,) = struct.unpack("<I", payload[32:36])
                    index_offsets = []
                    for i in range(36, min(36 + map_bytes, len(payload) - 9), 10):
                        index_offsets.append(struct.unpack("<HQ", payload[i:i + 10])[1])
                    chunks.append((start, end, index_offsets))
                else:
                    f.seek(length, os.SEEK_CUR)

            if stats is None and not chunks:
                return None

            ends = [end for _, end, _ in chunks]
            if stats is not None:
                ends.append(stats[1])
            end = max(ends)

            # Only distrust small stamps when the file is clearly in the epoch domain;
            # a recording stamped entirely from a monotonic clock is self-consistent.
            floor = MCAP_CLOCK_FLOOR_NS if end >= MCAP_CLOCK_FLOOR_NS else 0

            starts = []
            if stats is not None and stats[0] >= floor:
                starts.append(stats[0])
            for start, _, index_offsets in chunks:
                if start >= floor:
                    starts.append(start)
                    continue
                # This chunk holds the bad stamp. Its own message indexes still carry
                # every real log_time, so recover the true start without decompressing.
                recovered = [m for m in (mcap_message_index_min(f, o, floor) for o in index_offsets)
                             if m is not None]
                if recovered:
                    starts.append(min(recovered))

            if not starts:
                return None
            start = min(starts)
            return (start, end) if end > start else None
    except (OSError, struct.error):
        return None

def mcap_duration(session_dir):
    """Return session duration in seconds spanning every MCAP file in the directory, or None."""
    spans = [s for s in (mcap_span(p) for p in sorted(glob.glob(os.path.join(session_dir, "*.mcap")))) if s]
    if not spans:
        return None
    return (max(e for _, e in spans) - min(s for s, _ in spans)) / 1e9

def realsense_duration(session_dir):
    """Return session duration in seconds from realsense_log.csv timestamps, or None."""
    log_path = os.path.join(session_dir, "realsense_log.csv")
    if not os.path.exists(log_path):
        return None
    try:
        with open(log_path) as f:
            ns_values = [int(row["monotonic_ns"]) for row in csv.DictReader(f) if row.get("monotonic_ns")]
        if len(ns_values) >= 2:
            return (max(ns_values) - min(ns_values)) / 1e9
    except (KeyError, ValueError, OSError):
        pass
    return None

def session_duration(session_dir):
    """Return session duration in seconds, preferring MCAP, then realsense_log.csv, then head image count."""
    for duration in (mcap_duration(session_dir), realsense_duration(session_dir)):
        if duration is not None:
            return duration
    return head_duration(session_dir)

def head_count(session_dir):
    """Return the number of head images in a session directory."""
    return len(glob.glob(os.path.join(session_dir, "images", "head", "*")))

def head_duration(session_dir):
    """Return session duration in seconds from head image count / FPS."""
    count = head_count(session_dir)
    return count / FPS if count > 0 else None

def session_score(session_dir):
    """Return the session's numeric score from session_score.json, or None if absent/unreadable."""
    try:
        with open(os.path.join(session_dir, "session_score.json")) as f:
            score = json.load(f).get("score")
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)

def largest_inference_dir(parent):
    """Return the inference* dir with the largest trailing number under parent, or None."""
    largest = None
    for d in glob.glob(os.path.join(parent, "inference*")):
        m = re.search(r"(\d+)$", os.path.basename(d))
        if m:
            num = int(m.group(1))
            if largest is None or num > largest[0]:
                largest = (num, d)
    return largest[1] if largest is not None else None

def all_inference_dirs(parent):
    """Return every inference* dir under parent, sorted."""
    return sorted(glob.glob(os.path.join(parent, "inference*")))

def all_teleop_dirs(parent):
    """Return every teleop* dir under parent, sorted."""
    return sorted(glob.glob(os.path.join(parent, "teleop*")))

def inference_duration(session_dir):
    """Return inference duration in seconds, summing the inference session and its
    sibling teleop* dirs. Each dir prefers MCAP, falling back to head image count."""
    dirs = [session_dir] + all_teleop_dirs(os.path.dirname(session_dir))

    total = None
    for d in dirs:
        duration = mcap_duration(d)
        if duration is None:
            duration = head_duration(d)
        if duration is not None:
            total = duration if total is None else total + duration
    return total

def fmt_seconds(seconds):
    return f"{seconds:.2f}s"

def fmt_minutes(seconds):
    return f"{seconds / 60:.2f}m"

def classify_session(parent):
    """Return [(session_meta.json path, duration)] for one top-level session dir.

    Auto-detects which of four shapes `parent` is, from its own contents:
      - normal: a session_meta.json directly under it.
      - inference: inference* dirs alongside teleop* dirs -- only the latest
        (highest-numbered) inference* run is counted, its duration summed
        with the teleop* siblings (a demo + a graded test of it).
      - correction: inference* dirs with no teleop* siblings -- every
        inference* dir counts as its own session, own duration only.
      - teleop-only: a lone teleop* dir with no inference* siblings -- a plain
        demo, counted as its own session. A teleop-only parent holding more
        than one teleop* dir is ambiguous and is skipped.
    """
    direct_meta = os.path.join(parent, "session_meta.json")
    if os.path.exists(direct_meta):
        return [(direct_meta, session_duration(parent))]

    inference_dirs = all_inference_dirs(parent)
    teleop_dirs = all_teleop_dirs(parent)

    if not inference_dirs:
        if len(teleop_dirs) != 1:
            return []
        meta = os.path.join(teleop_dirs[0], "session_meta.json")
        return [(meta, session_duration(teleop_dirs[0]))] if os.path.exists(meta) else []

    if teleop_dirs:
        inf_dir = largest_inference_dir(parent)
        meta = os.path.join(inf_dir, "session_meta.json")
        return [(meta, inference_duration(inf_dir))] if os.path.exists(meta) else []

    return [(os.path.join(d, "session_meta.json"), session_duration(d))
            for d in inference_dirs if os.path.exists(os.path.join(d, "session_meta.json"))]


def event_label(data):
    """Return session_meta.json's event_object as a display label, or None if absent.

    event_object is normally a list of part/task ids (e.g. ["E10"]), sometimes null.
    """
    ev = data.get("event_object")
    if isinstance(ev, list):
        ev = [str(x) for x in ev if x not in (None, "")]
        return "+".join(ev) if ev else None
    if isinstance(ev, str) and ev:
        return ev
    return None


def iter_sessions(dirs, target_date):
    """Yield (operator, task_type_or_None, event_label_or_None, duration, score_or_None)
    for every matching session. score is None unless the session has a session_score.json."""
    for d in dirs:
        for parent in sorted(glob.glob(os.path.join(d, "*"))):
            for path, duration in classify_session(parent):
                if os.path.basename(os.path.dirname(path)).endswith("_mpeg"):
                    continue
                if target_date is not None:
                    session_date = date.fromtimestamp(os.path.getmtime(path))
                    if session_date != target_date:
                        continue
                try:
                    with open(path) as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"Skipping {path}: {e}")
                    continue

                yield (data.get("operator_name", "UNKNOWN"), data.get("task_type") or None,
                       event_label(data), duration, session_score(os.path.dirname(path)))


def gather_stats(dirs, target_date, level=0):
    """Tally session stats at one of three breakdown levels:

      0 -- by operator
      1 -- by operator + task type
      2 -- by operator + task type + event object

    "-" stands in for a task type or event object a session doesn't carry. Rows group
    case-insensitively, so "Jonathan" and "jonathan" -- or "Tool_Box" and "tool_box" --
    land in the same row; the first spelling seen becomes that row's display label.

    Only sessions that actually have a score contribute to "scores" -- an unscored session
    is left out of the list entirely rather than counted as a zero."""
    stats = defaultdict(lambda: {"total": 0, "durations": [], "scores": [], "label": None})

    for operator, task_type, event, duration, score in iter_sessions(dirs, target_date):
        label = (operator,)
        if level >= 1:
            label += (task_type or "-",)
        if level >= 2:
            label += (event or "-",)

        s = stats[tuple(p.lower() for p in label)]
        if s["label"] is None:
            s["label"] = label
        s["total"] += 1
        if duration is not None:
            s["durations"].append(duration)
        if score is not None:
            s["scores"].append(score)

    return stats


def fmt_score(scores):
    """Average of the scores that exist. Sessions with no score never reach this list,
    so they are excluded from the average rather than pulling it toward zero."""
    return f"{sum(scores) / len(scores):.2f}" if scores else "-"


def format_report(stats, filter_desc=None, level=0):
    """Render a stats report as a list of lines, for printing or for curses.

    level mirrors gather_stats: 0 shows operators only, 1 adds a Task column, 2 adds
    Event as well. Avg Score rides along from level 1 up. Rows sort by their grouping
    key (already lowercased), so ordering ignores case too.
    """
    lines = [f"Report generated: {datetime.now():%Y-%m-%d %H:%M:%S}"]
    if filter_desc:
        lines.append(f"Filtered to: {filter_desc}")
    lines.append("")

    if not stats:
        lines.append("No session_meta.json files found.")
        return lines

    labels = [s["label"] for s in stats.values()]
    name_w = max(len("Operator"), len("TOTAL"), *(len(l[0]) for l in labels))
    task_w = max(len("Task"), *(len(l[1]) for l in labels)) if level >= 1 else 0
    event_w = max(len("Event"), *(len(l[2]) for l in labels)) if level >= 2 else 0

    avg_w = max(len("Avg Time"), 10)
    data_w = max(len("Total Data Time"), 15)
    score_w = len("Avg Score")

    def row(name, task, event, total, avg, data, score):
        cols = f"{name:<{name_w}}"
        if level >= 1:
            cols += f"  {task:<{task_w}}"
        if level >= 2:
            cols += f"  {event:<{event_w}}"
        cols += f"  {total:>6}  {avg:>{avg_w}}  {data:>{data_w}}"
        if level >= 1:
            cols += f"  {score:>{score_w}}"
        return cols

    header = row("Operator", "Task", "Event", "Total", "Avg Time", "Total Data Time", "Avg Score")
    lines.append(header)
    lines.append("-" * len(header))

    for key in sorted(stats):
        s = stats[key]
        durations = s["durations"]
        if durations:
            avg_str = fmt_seconds(sum(durations) / len(durations))
            data_str = fmt_minutes(sum(durations))
        else:
            avg_str = data_str = "-"

        name, task, event = (s["label"] + ("", ""))[:3]
        lines.append(row(name, task, event, s["total"], avg_str, data_str, fmt_score(s["scores"])))

    all_durations = [d for s in stats.values() for d in s["durations"]]
    if all_durations:
        total_avg_str = fmt_seconds(sum(all_durations) / len(all_durations))
        total_data_str = fmt_minutes(sum(all_durations))
    else:
        total_avg_str = total_data_str = "-"
    all_scores = [x for s in stats.values() for x in s["scores"]]

    lines.append("-" * len(header))
    lines.append(row("TOTAL", "", "", sum(s["total"] for s in stats.values()),
                     total_avg_str, total_data_str, fmt_score(all_scores)))
    return lines


def print_report(stats, filter_desc=None, level=0):
    for line in format_report(stats, filter_desc, level):
        print(line)


# ---- Shift Report interchange code ----
#
# The Shift Report tracker (rf-admin) takes a station's numbers as one pasteable
# line instead of hand-typed cells. `--code` prints that line; the tracker's
# "Paste stats code" box decodes it and fills the station in.

CODE_PREFIX = "RF1:"

# The station type a code lands on in the tracker. Only GELLO runs are emitted for
# now; the field stays in the payload because the tracker routes on it.
CODE_MODE = "gello"

# A station's name is typed once and remembered here, so later runs -- and the
# people running them -- never have to know it.
STATION_FILE = os.path.expanduser("~/.rf-station")


def read_station_name():
    """The station name saved on this machine, or None if never set."""
    try:
        with open(STATION_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def save_station_name(name):
    """Remember a station name for later runs. Returns the path written, or None.

    A read-only home or full disk is not worth failing a report over -- the name
    still applies to the run in hand, it just will not be there next time.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        with open(STATION_FILE, "w") as f:
            f.write(name + "\n")
    except OSError:
        return None
    return STATION_FILE


def station_name(override=None):
    """Resolve the station name, most explicit source first.

    --name beats RF_STATION, which beats the saved file, which beats the hostname.
    The hostname is only ever a last resort: it is stable but rarely the label the
    Shift Report knows the station by.
    """
    for candidate in (override, os.environ.get("RF_STATION"), read_station_name()):
        if candidate and candidate.strip():
            return candidate.strip()
    return socket.gethostname()


def code_ops(stats):
    """Per-operator rows for a code payload, from level-0 stats.

    Durations are optional: an operator whose sessions all lack a readable
    duration gets a session count and empty time fields rather than a zero,
    matching how the report renders "-" for them.
    """
    ops = []
    for key in sorted(stats):
        s = stats[key]
        durations = s["durations"]
        op = {"n": s["label"][0], "sessions": s["total"]}
        if durations:
            op["avgSec"] = round(sum(durations) / len(durations), 2)
            op["totalMin"] = round(sum(durations) / 60, 2)
        ops.append(op)
    return ops


def build_code_payload(stats, target_date, host):
    """The tracker's import envelope, carrying this one station's run.

    Shaped as a list of stations even though a run only ever produces one, so a
    single code and a combined multi-station code decode through the same path.
    """
    return {
        "v": 1,
        "type": "shift-import",
        "date": (target_date or date.today()).isoformat(),
        "stations": [{"mode": CODE_MODE, "host": host, "ops": code_ops(stats)}],
    }


def encode_code(payload):
    """Encode a payload as one copy-pasteable line: RF1: + unpadded base64url."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# A code goes on screen as a QR for phones to scan, and RF1's JSON is far too long to
# make a scannable one -- a busy day runs past 1000 characters. RF2 carries the same
# payload packed as binary, about five times shorter, because a JSON code is mostly
# field names repeated once per operator and numbers spelled out as text.
CODE_PREFIX_V2 = "RF2:"
RF2_VERSION = 2

# Dates pack into 16 bits as (year - 2020, month, day). Years run to 2147.
RF2_EPOCH_YEAR = 2020

# Station modes in the order the mode byte numbers them. The tracker routes on this,
# so the order is part of the format -- append to it, never reorder.
CODE_MODES = ["gello", "inference"]


def pack_varint(n):
    """LEB128: seven bits per byte, high bit set while more bytes follow."""
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pack_text(s):
    """A UTF-8 string behind a one-byte length.

    Operator and station names are short, but the length is one byte, so an
    absurd name is truncated rather than corrupting every field after it. The cut
    backs off a byte at a time so it never lands mid-character.
    """
    raw = s.encode("utf-8")[:255]
    while raw:
        try:
            raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    return bytes([len(raw)]) + raw


def encode_code_rf2(payload):
    """Encode a payload as RF2: + unpadded base64url over a packed binary body.

    Takes exactly what encode_code takes. Nothing is dropped or rounded away:
    avgSec and totalMin are both carried, in hundredths, so the tracker shows the
    same numbers the table does.
    """
    year, month, day = (int(part) for part in payload["date"].split("-"))
    out = bytearray([RF2_VERSION])
    out += ((max(year - RF2_EPOCH_YEAR, 0) << 9) | (month << 5) | day).to_bytes(2, "big")
    out.append(len(payload["stations"]))
    for station in payload["stations"]:
        mode = station.get("mode", CODE_MODE)
        out.append(CODE_MODES.index(mode) if mode in CODE_MODES else 0)
        out += pack_text(str(station.get("host", "")))
        ops = station["ops"]
        out += pack_varint(len(ops))
        for op in ops:
            out += pack_text(str(op["n"]))
            out += pack_varint(op["sessions"])
            # A zero avgSec marks an operator whose sessions had no readable duration.
            # A session that has one can never average zero, so no flag byte is needed.
            if "avgSec" in op:
                out += pack_varint(round(op["avgSec"] * 100))
                out += pack_varint(round(op["totalMin"] * 100))
            else:
                out += pack_varint(0)
    return CODE_PREFIX_V2 + base64.urlsafe_b64encode(bytes(out)).decode("ascii").rstrip("=")


def code_summary(payload):
    """One-line human check of what a code holds, so it can be confirmed before pasting."""
    station = payload["stations"][0]
    return f"{station['host']}  {payload['date']}  {len(station['ops'])} operator(s)"


CLIPBOARD_COMMANDS = [
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
]


def copy_to_clipboard(text):
    """Put text on the system clipboard, returning the tool that took it or None.

    Station PCs vary (Wayland, X11, macOS) and may have none of these installed,
    so every failure is soft -- the code is always shown on screen as well.
    """
    for cmd in CLIPBOARD_COMMANDS:
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, ValueError):
            continue
        if proc.returncode == 0:
            return cmd[0]
    return None


# ---- QR codes (byte mode, error correction level M) ----
#
# Enough of ISO/IEC 18004 to put a code on screen for a phone to scan, and no more:
# byte mode only, one correction level, versions 1-15. That holds 412 characters,
# well past the longest day an RF2 code describes. Stdlib only, so a station PC
# needs nothing installed.

QR_MAX_VERSION = 15
QR_ECC_FORMAT_BITS = 0b00  # level M, as the format information spells it

# Per version: error correction codewords per block, then (block count, data
# codewords per block) for each group.
QR_BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
    11: (30, [(1, 50), (4, 51)]),
    12: (22, [(6, 36), (2, 37)]),
    13: (22, [(8, 37), (1, 38)]),
    14: (24, [(4, 40), (5, 41)]),
    15: (24, [(5, 41), (5, 42)]),
}

# Row/column centres of the alignment patterns. Every pairing is used except the
# three that would sit on a finder pattern.
QR_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
    11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
    15: [6, 26, 48, 70],
}

# log/antilog tables for GF(256) with primitive polynomial 0x11D, which is what
# Reed-Solomon coding over QR codewords is defined on.
QR_EXP = [0] * 512
QR_LOG = [0] * 256


def _qr_build_tables():
    x = 1
    for i in range(255):
        QR_EXP[i] = x
        QR_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        QR_EXP[i] = QR_EXP[i - 255]


_qr_build_tables()


def qr_mul(a, b):
    """Multiply two GF(256) elements."""
    if a == 0 or b == 0:
        return 0
    return QR_EXP[QR_LOG[a] + QR_LOG[b]]


def qr_generator_poly(degree):
    """The Reed-Solomon generator polynomial, highest power first."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= qr_mul(coef, QR_EXP[i])
        poly = nxt
    return poly


def qr_ec_codewords(data, degree):
    """The error correction codewords for one block: polynomial division remainder."""
    gen = qr_generator_poly(degree)
    rem = list(data) + [0] * degree
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= qr_mul(g, coef)
    return rem[len(data):]


def qr_count_bits(version):
    """Width of the character count field. Byte mode widens it past version 9."""
    return 8 if version < 10 else 16


def qr_data_codewords(version):
    return sum(count * size for count, size in QR_BLOCKS[version][1])


def qr_capacity(version):
    """How many bytes a version holds in byte mode, after mode and count fields."""
    return (qr_data_codewords(version) * 8 - 4 - qr_count_bits(version)) // 8


def qr_smallest_version(length):
    """The smallest version that fits length bytes, or None if nothing does."""
    for version in range(1, QR_MAX_VERSION + 1):
        if length <= qr_capacity(version):
            return version
    return None


def qr_payload_codewords(raw, version):
    """Mode, length, data, terminator and padding, as data codewords."""
    bits = []

    def put(value, width):
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(raw), qr_count_bits(version))
    for byte in raw:
        put(byte, 8)

    total = qr_data_codewords(version)
    put(0, min(4, total * 8 - len(bits)))  # terminator, truncated if it will not fit
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    # The spec's fixed filler, alternating until the version is full.
    pad, i = (0xEC, 0x11), 0
    while len(codewords) < total:
        codewords.append(pad[i % 2])
        i += 1
    return codewords


def qr_final_codewords(raw, version):
    """Data and error correction codewords, interleaved the way the spec orders them."""
    ec_per_block, groups = QR_BLOCKS[version]
    data = qr_payload_codewords(raw, version)

    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[pos:pos + size])
            pos += size
    ec_blocks = [qr_ec_codewords(block, ec_per_block) for block in blocks]

    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_per_block):
        out += [b[i] for b in ec_blocks]
    return out


# The eight mask patterns, by number. A mask is XORed over the data modules only;
# whichever scores best under qr_penalty wins, and its number goes in the format bits.
QR_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def qr_function_patterns(version):
    """The fixed patterns for a version: (modules, reserved), both size x size.

    reserved marks every module the data must skip -- finders, timing, alignment,
    the dark module, and the areas format and version information land in later.
    """
    size = version * 4 + 17
    modules = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def block(top, left, height, width, fill=None):
        for r in range(top, top + height):
            for c in range(left, left + width):
                if 0 <= r < size and 0 <= c < size:
                    reserved[r][c] = True
                    if fill is not None:
                        modules[r][c] = fill(r - top, c - left)

    def finder(top, left):
        ring = lambda r, c: int(r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
        block(top, left, 7, 7, ring)
        # Separators: the one-module light border around each finder.
        for r in range(top - 1, top + 8):
            for c in range(left - 1, left + 8):
                if 0 <= r < size and 0 <= c < size and not reserved[r][c]:
                    reserved[r][c] = True
                    modules[r][c] = 0

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # Timing patterns, running between the finders on row 6 and column 6.
    for i in range(8, size - 8):
        modules[6][i] = modules[i][6] = int(i % 2 == 0)
        reserved[6][i] = reserved[i][6] = True

    # Alignment patterns, at every pairing of centres that clears the finders.
    centres = QR_ALIGNMENT[version]
    for r in centres:
        for c in centres:
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            block(r - 2, c - 2, 5, 5,
                  lambda dr, dc: int(dr in (0, 4) or dc in (0, 4) or (dr == 2 and dc == 2)))

    # The dark module, always set, always in the same place.
    modules[size - 8][8] = 1
    reserved[size - 8][8] = True

    # Format information, in two copies. Reserved now, written once a mask is chosen.
    for i in range(9):
        for r, c in ((8, i), (i, 8)):
            if 0 <= r < size and 0 <= c < size:
                reserved[r][c] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    # Version information, from version 7 up: two 6x3 blocks by the top-right and
    # bottom-left finders.
    if version >= 7:
        bits = qr_version_bits(version)
        for i in range(18):
            bit = (bits >> i) & 1
            r, c = i // 3, size - 11 + i % 3
            modules[r][c] = bit
            reserved[r][c] = True
            modules[c][r] = bit
            reserved[c][r] = True

    return modules, reserved


def qr_version_bits(version):
    """18-bit version information: the version number plus its BCH(18,6) remainder."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ (0x1F25 * ((rem >> 11) & 1))
    return (version << 12) | rem


def qr_format_bits(mask):
    """15-bit format information: level and mask, BCH(15,5) coded, then masked."""
    data = (QR_ECC_FORMAT_BITS << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (0x537 * ((rem >> 9) & 1))
    return ((data << 10) | rem) ^ 0x5412


def qr_place_data(modules, reserved, codewords):
    """Walk the codeword bits into the matrix: column pairs, right to left, zigzagging."""
    size = len(modules)
    bits = [(cw >> i) & 1 for cw in codewords for i in range(7, -1, -1)]
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # the vertical timing pattern is not a data column
            col -= 1
        for row in (range(size - 1, -1, -1) if upward else range(size)):
            for c in (col, col - 1):
                if not reserved[row][c]:
                    modules[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


def qr_place_format(modules, mask):
    """Write both copies of the format information for a chosen mask."""
    size = len(modules)
    bits = qr_format_bits(mask)
    for i in range(15):
        # Format information reads most significant bit first, unlike the version
        # information below it, which reads least significant bit first.
        bit = (bits >> (14 - i)) & 1
        if i < 6:
            modules[8][i] = bit
        elif i < 8:
            modules[8][i + 1] = bit
        elif i == 8:
            modules[7][8] = bit
        else:
            modules[14 - i][8] = bit
        if i < 7:
            modules[size - 1 - i][8] = bit
        else:
            modules[8][size - 15 + i] = bit


def qr_penalty(modules):
    """Score a masked matrix by the spec's four rules. Lower is better."""
    size = len(modules)
    score = 0

    # Rule 1: runs of five or more same-coloured modules in a row or column.
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    # Rule 2: every 2x2 block of one colour.
    for r in range(size - 1):
        for c in range(size - 1):
            if modules[r][c] == modules[r][c + 1] == modules[r + 1][c] == modules[r + 1][c + 1]:
                score += 3

    # Rule 3: the finder-like 1:1:3:1:1 pattern with four light modules beside it,
    # which a scanner could mistake for a real finder.
    a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        for i in range(size - 10):
            if line[i:i + 11] in (a, b):
                score += 40

    # Rule 4: how far the dark proportion strays from half.
    dark = sum(sum(row) for row in modules)
    score += 10 * int(abs(dark * 100 / (size * size) - 50) / 5)
    return score


def qr_matrix(text):
    """Build the finished module matrix for text, as rows of 0/1. 1 is dark.

    Raises ValueError when the text is longer than version 15 holds.
    """
    raw = text.encode("utf-8")
    version = qr_smallest_version(len(raw))
    if version is None:
        raise ValueError(f"{len(raw)} bytes is too long for a version {QR_MAX_VERSION} QR code "
                         f"(limit {qr_capacity(QR_MAX_VERSION)})")
    codewords = qr_final_codewords(raw, version)

    best = None
    for mask in range(8):
        modules, reserved = qr_function_patterns(version)
        qr_place_data(modules, reserved, codewords)
        for r in range(len(modules)):
            for c in range(len(modules)):
                if not reserved[r][c] and QR_MASKS[mask](r, c):
                    modules[r][c] ^= 1
        qr_place_format(modules, mask)
        penalty = qr_penalty(modules)
        if best is None or penalty < best[0]:
            best = (penalty, modules)
    return best[1]


# Two module rows per text row, so modules come out square instead of twice as tall
# as they are wide. Index is (top dark, bottom dark).
QR_HALF_BLOCKS = {(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}

QR_QUIET_ZONE = 4


def qr_lines(text):
    """Render text as QR code lines, dark modules drawn in the foreground colour.

    The caller supplies the colours. They must be dark-on-light whatever the
    terminal's own theme is: an inverted QR code is one many phones will not read.
    """
    matrix = qr_matrix(text)
    size = len(matrix)
    width = size + 2 * QR_QUIET_ZONE
    blank = [0] * width
    rows = ([blank] * QR_QUIET_ZONE
            + [[0] * QR_QUIET_ZONE + row + [0] * QR_QUIET_ZONE for row in matrix]
            + [blank] * QR_QUIET_ZONE)
    if len(rows) % 2:
        rows.append(blank)
    return ["".join(QR_HALF_BLOCKS[(rows[i][c], rows[i + 1][c])] for c in range(width))
            for i in range(0, len(rows), 2)]


# A QR code must be dark-on-light to scan, whatever the terminal's own theme is:
# on a dark background the code comes out inverted and most phones refuse to read it.
QR_ANSI_DARK_ON_LIGHT = "\x1b[30;47m"
QR_ANSI_RESET = "\x1b[0m"


def sms_uri(code, number=""):
    """A message-composing link, for a QR code a phone camera will actually act on.

    A bare code scans as plain text, which iOS decodes and then discards -- the camera
    says "no usable data" and there is nothing to copy. Wrapped like this it opens the
    messaging app with the code already in the body instead.

    The `?&` is deliberate: iOS reads the body from `&body=`, Android from `?body=`,
    and this one spelling satisfies both. The number is optional -- without it the
    message opens with no recipient, to be chosen on the phone.
    """
    return f"sms:{number}?&body={urllib.parse.quote(code, safe='')}"


def print_qr(text, stream=sys.stdout):
    """Draw text as a QR code, forcing its own colours rather than the terminal's."""
    for line in qr_lines(text):
        print(f"{QR_ANSI_DARK_ON_LIGHT}{line}{QR_ANSI_RESET}", file=stream)


def capture_dirs(root):
    """Every captures* dir under root -- captures, captures2, captures_old, ..."""
    return sorted(d for d in glob.glob(os.path.join(root, "captures*")) if os.path.isdir(d))


def upload_dirs(root):
    return [os.path.join(root, "upload"), os.path.join(root, "successfully_uploaded")]


def all_dirs(root):
    return capture_dirs(root) + upload_dirs(root)


def dirs_for(scope):
    """Resolve a -d/--dir value to a list of root dirs to scan.

    None (the default) scans everything under CORE_APPS; "captures" or "upload"
    select one part of it; "local" scans everything under the dev-machine copy at
    LOCAL_CORE_APPS; anything else is treated as a custom folder path. Roots that
    don't exist are harmless -- they simply contribute no sessions.
    """
    if scope is None:
        return all_dirs(CORE_APPS)
    if scope == "captures":
        return capture_dirs(CORE_APPS)
    if scope == "upload":
        return upload_dirs(CORE_APPS)
    if scope == "local":
        return all_dirs(LOCAL_CORE_APPS)
    return [scope]


def main():
    parser = argparse.ArgumentParser(
        epilog="Run with no flags in a terminal to get an interactive menu instead.")
    parser.add_argument("-d", "--dir", metavar="SCOPE", default=None,
                         help="Which data to scan: 'captures' (every captures* folder), "
                              "'upload' (upload + successfully_uploaded), 'local' (all of the above under "
                              f"{LOCAL_CORE_APPS}), or a custom folder path. "
                              "Default: captures* + upload + successfully_uploaded.")
    parser.add_argument("-t", "--days-ago", type=int, metavar="N", help="Filter sessions by date: 0=today, 1=yesterday, etc.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                         help="Break sessions down further than operator, and show each row's average "
                              "session score. -v splits by task type; -vv also splits by event object "
                              "(task/part). Sessions missing a task type or event object show '-' for it.")
    parser.add_argument("--code", "--hash", dest="code", action="store_true",
                         help="Print one pasteable line for the rf-admin Shift Report instead of the "
                              "table, so a station's numbers never have to be retyped. Covers today "
                              "unless -t says otherwise.")
    parser.add_argument("--qr", action="store_true",
                         help="Draw the code as a QR code as well, to scan off the station "
                              "screen with a phone instead of copying it off the PC. Implies "
                              "--code.")
    parser.add_argument("--sms", metavar="NUMBER", default=None,
                         help="Address the QR code's message to a number, so scanning it "
                              "opens a message already written and addressed. Without it the "
                              "recipient is chosen on the phone.")
    parser.add_argument("--name", metavar="NAME", default=None,
                         help="Station name carried in --code, and remembered in "
                              f"{STATION_FILE} for later runs -- set it once per station, to the "
                              "label the Shift Report uses, and the code routes itself. Without it: "
                              f"$RF_STATION, then the saved name, then the hostname "
                              f"({socket.gethostname()!r}).")
    args = parser.parse_args()
    level = min(args.verbose, 2)
    # A QR code is just another way of handing over the same code, so it brings the
    # rest of code mode with it -- today by default, one row per operator.
    code = args.code or args.qr

    dirs = dirs_for(args.dir)
    # A code stands for one day's station report, so it defaults to today rather
    # than to the all-time scan a bare run does.
    days_ago = 0 if code and args.days_ago is None else args.days_ago
    target_date = date.today() - timedelta(days=days_ago) if days_ago is not None else None

    # A code carries one row per operator, so it always gathers at level 0 --
    # -v/-vv only shape the printed table.
    stats = gather_stats(dirs, target_date, 0 if code else level)

    if code:
        # Naming a station is how you set one up, so the name sticks rather than
        # having to be repeated on every run.
        if args.name and args.name.strip() != (read_station_name() or ""):
            if save_station_name(args.name):
                print(f"Saved station name {args.name.strip()!r} to {STATION_FILE}", file=sys.stderr)

        payload = build_code_payload(stats, target_date, station_name(args.name))
        text = encode_code_rf2(payload)
        # Summary to stderr, code alone to stdout, so `stats.py --code | pbcopy` stays clean.
        print(code_summary(payload), file=sys.stderr)
        if args.qr:
            try:
                print_qr(sms_uri(text, args.sms or ""))
            except ValueError as e:
                print(f"Too many operators to fit a QR code ({e}) -- copy the line instead.",
                      file=sys.stderr)
        print(text)
        return

    filter_desc = f"{target_date} ({days_ago} day(s) ago)" if target_date is not None else None
    print_report(stats, filter_desc, level)


# ---- interactive TUI (stdlib curses, no install required) ----

def _addstr(stdscr, y, x, s, attr=curses.A_NORMAL):
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass  # window too small for this line; skip rather than crash


def key_char(key):
    """Lowercased character for a keypress, or "" for keys that aren't characters.
    Lets V/Q/S/J/K work the same as v/q/s/j/k, e.g. with caps lock on."""
    try:
        return chr(key).lower()
    except ValueError:
        return ""


def curses_menu(stdscr, title, options, subtitle=None, default_index=0):
    """Arrow-key menu. Returns the selected index, or None if the user quit (q/Esc)."""
    curses.curs_set(0)
    idx = default_index if 0 <= default_index < len(options) else 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        _addstr(stdscr, 0, 2, title, curses.A_BOLD)
        base_row = 2
        if subtitle:
            max_w = max(w - 4, 0)
            # Long subtitles (e.g. a deep folder path) are trimmed from the left so the
            # most relevant part -- the tail -- stays visible instead of the root.
            sub = "..." + subtitle[-(max_w - 3):] if len(subtitle) > max_w > 3 else subtitle
            _addstr(stdscr, 1, 2, sub[:max_w], curses.A_DIM)
            base_row = 3
        for i, opt in enumerate(options):
            row = base_row + i
            if row >= h - 2:
                break
            marker = "> " if i == idx else "  "
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            _addstr(stdscr, row, 2, (marker + opt)[:max(w - 4, 0)], attr)
        _addstr(stdscr, h - 1, 2, "up/down move   enter select   q cancel"[:max(w - 4, 0)], curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        ch = key_char(key)
        if key == curses.KEY_UP or ch == "k":
            idx = (idx - 1) % len(options)
        elif key == curses.KEY_DOWN or ch == "j":
            idx = (idx + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13):
            return idx
        elif key == 27 or ch == "q":
            return None
        elif "1" <= ch <= "9" and int(ch) - 1 < len(options):
            return int(ch) - 1


def curses_prompt_str(stdscr, prompt, default="", width=200):
    """Ask for a line of text. Returns default on empty input."""
    curses.echo()
    curses.curs_set(1)
    stdscr.erase()
    _addstr(stdscr, 2, 2, prompt)
    _addstr(stdscr, 4, 2, "> ")
    stdscr.refresh()
    try:
        raw = stdscr.getstr(4, 4, width).decode().strip()
    except (curses.error, UnicodeDecodeError):
        raw = ""
    curses.noecho()
    curses.curs_set(0)
    return raw or default


def curses_prompt_int(stdscr, prompt, default=0):
    """Ask for a whole number. Returns default on empty/invalid input."""
    raw = curses_prompt_str(stdscr, prompt)
    try:
        return int(raw)
    except ValueError:
        return default


def curses_browse_dir(stdscr, start):
    """Arrow-key folder browser rooted at `start`. Returns the chosen absolute
    path, or None if the user cancelled (q/Esc) at any point."""
    current = os.path.abspath(start) if os.path.isdir(start) else os.path.abspath(os.sep)
    while True:
        try:
            subdirs = sorted(e.name for e in os.scandir(current)
                              if e.is_dir(follow_symlinks=True) and not e.name.startswith("."))
        except OSError:
            subdirs = []

        options = ["[ use this folder ]"]
        if os.path.dirname(current) != current:
            options.append("..")
        options.extend(subdirs)
        options.append("Type a path manually...")

        choice = curses_menu(stdscr, "robo-stats", options, subtitle=f"Folder: {current}")
        if choice is None:
            return None
        picked = options[choice]

        if picked == "[ use this folder ]":
            return current
        if picked == "Type a path manually...":
            typed = curses_prompt_str(stdscr, "Folder path:", default=current)
            if os.path.isdir(typed):
                current = os.path.abspath(typed)
            # invalid path: redisplay the browser at the same spot
        elif picked == "..":
            current = os.path.dirname(current)
        else:
            current = os.path.join(current, picked)


def default_browse_start():
    """Where the folder browser opens: the deployed root, else the local dev copy, else cwd."""
    for root in (CORE_APPS, LOCAL_CORE_APPS):
        if os.path.isdir(root):
            return root
    return "."


def tui_wizard(stdscr, initial_scope=None, initial_days_ago=0):
    """Ask When?/Which data? and return (scope, days_ago), or None if cancelled.

    initial_scope/initial_days_ago preselect the current settings when this is
    reopened from the results screen (the "s" settings shortcut) rather than
    always starting fresh at Today/Everything.
    """
    curses.curs_set(0)

    day_index = {0: 0, 1: 1, None: 2}.get(initial_days_ago, 3)
    choice = curses_menu(
        stdscr, "robo-stats", subtitle="When?",
        options=["Today", "Yesterday", "All time", "Enter a specific number of days ago..."],
        default_index=day_index)
    if choice is None:
        return None
    if choice == 3:
        default_days = initial_days_ago if day_index == 3 else 0
        days_ago = curses_prompt_int(stdscr, "How many days ago? (0 = today)", default=default_days)
    else:
        days_ago = [0, 1, None][choice]

    scope_index = {None: 0, "captures": 1, "upload": 2, "local": 3}.get(initial_scope, 4)
    choice = curses_menu(
        stdscr, "robo-stats", subtitle="Which data?",
        options=["Everything (captures + uploads)", "Captures only", "Uploads only",
                 f"Everything from local ({LOCAL_CORE_APPS})", "Choose a specific folder..."],
        default_index=scope_index)
    if choice is None:
        return None
    if choice == 4:
        browse_start = initial_scope if scope_index == 4 else default_browse_start()
        scope = curses_browse_dir(stdscr, browse_start)
        if scope is None:
            return None
    else:
        scope = [None, "captures", "upload", "local"][choice]

    return scope, days_ago


BREAKDOWN_HINTS = {
    0: "v split by task type",
    1: "v add event breakdown",
    2: "v hide breakdown",
}


def render_report(stdscr, lines, level):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    for i, line in enumerate(lines):
        if i >= h - 2:
            break
        _addstr(stdscr, i, 2, line[:max(w - 4, 0)])
    footer = f"enter refresh   {BREAKDOWN_HINTS[level]}   c code   s settings   q quit"
    _addstr(stdscr, h - 1, 2, footer[:max(w - 4, 0)], curses.A_DIM)
    stdscr.refresh()


QR_COLOR_PAIR = 1


def tui_qr(stdscr, code):
    """Fill the screen with the code as a QR code, for a phone to scan off the station.

    Drawn in its own black-on-white pair rather than the terminal's colours, since an
    inverted QR code is one most phones will not read. A window too small for the whole
    code says so instead of showing a clipped one, which would scan as nothing.
    """
    try:
        lines = qr_lines(sms_uri(code))
    except ValueError:
        lines = None

    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if lines is None:
        _addstr(stdscr, 0, 2,
                "Too many operators to fit a QR code -- copy the code instead."[:max(w - 4, 0)])
    elif len(lines) + 1 > h or len(lines[0]) > w:
        _addstr(stdscr, 0, 2, "Window too small for the QR code."[:max(w - 4, 0)])
        _addstr(stdscr, 1, 2, f"Needs {len(lines[0])} x {len(lines) + 1}, "
                              f"this window is {w} x {h}."[:max(w - 4, 0)], curses.A_DIM)
    else:
        attr = curses.color_pair(QR_COLOR_PAIR) if curses.has_colors() else curses.A_REVERSE
        left = (w - len(lines[0])) // 2
        top = (h - 1 - len(lines)) // 2
        for i, line in enumerate(lines):
            _addstr(stdscr, top + i, left, line, attr)
    _addstr(stdscr, h - 1, 2, "any key back"[:max(w - 4, 0)], curses.A_DIM)
    stdscr.refresh()
    stdscr.getch()


def tui_code(stdscr, scope, days_ago, code_state):
    """Show the pasteable Shift Report code for the current filters and offer to copy it.

    The station name is asked for once and written to ~/.rf-station, so the next
    run on this machine already knows it. The scan runs at level 0 -- one row per
    operator, matching what a station block holds.
    """
    stdscr.erase()
    _addstr(stdscr, 0, 2, "Building code...", curses.A_DIM)
    stdscr.refresh()

    target_date = date.today() - timedelta(days=days_ago) if days_ago is not None else None
    stats = gather_stats(dirs_for(scope), target_date, 0)
    status = ""

    while True:
        payload = build_code_payload(stats, target_date, code_state["name"])
        code = encode_code_rf2(payload)

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        width = max(w - 4, 20)
        _addstr(stdscr, 0, 2, "Shift Report code", curses.A_BOLD)
        _addstr(stdscr, 1, 2, code_summary(payload)[:width], curses.A_DIM)
        row = 3
        for i in range(0, len(code), width):
            if row >= h - 3:
                break
            _addstr(stdscr, row, 2, code[i:i + width])
            row += 1
        if status:
            _addstr(stdscr, min(row + 1, h - 3), 2, status[:width], curses.A_DIM)
        _addstr(stdscr, h - 1, 2, "c copy   p QR for phone   n name   q back"[:width], curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        ch = key_char(key)
        if ch == "p":
            tui_qr(stdscr, code)
        elif ch == "c":
            tool = copy_to_clipboard(code)
            status = (f"Copied to the clipboard with {tool}." if tool else
                      "No clipboard tool found -- select the code above to copy it.")
        elif ch == "n":
            name = curses_prompt_str(stdscr, "Station name (as labelled in the Shift Report)",
                                     default=code_state["name"])
            code_state["name"] = name
            status = (f"Saved as the station name for this machine ({STATION_FILE})."
                      if save_station_name(name) else
                      f"Using {name} for now -- {STATION_FILE} could not be written.")
        else:
            return


def tui_results(stdscr, scope, days_ago):
    """Show the report and keep the TUI open, re-scanning every time Enter is pressed --
    so an operator can leave it up while a session records and check progress live.
    Press v to cycle the breakdown: operator only -> + task type -> + event object
    (task/part) -> back to operator only. Levels above the first also show each row's
    average session score.
    Press s to change the When?/Which data? settings without restarting.
    Press c for the pasteable Shift Report code covering the same filters."""
    level = 0
    code_state = {"name": station_name()}
    while True:
        stdscr.erase()
        _addstr(stdscr, 0, 2, "Refreshing...", curses.A_DIM)
        stdscr.refresh()

        dirs = dirs_for(scope)
        target_date = date.today() - timedelta(days=days_ago) if days_ago is not None else None
        stats = gather_stats(dirs, target_date, level)
        filter_desc = f"{target_date} ({days_ago} day(s) ago)" if target_date is not None else None
        lines = format_report(stats, filter_desc, level)
        lines += ["", f"Last checked: {datetime.now():%H:%M:%S}"]

        while True:
            render_report(stdscr, lines, level)
            key = stdscr.getch()
            ch = key_char(key)
            if key in (curses.KEY_ENTER, 10, 13):
                break  # refresh
            if ch == "v":
                level = (level + 1) % len(BREAKDOWN_HINTS)
                break  # re-gather at the new breakdown level
            if ch == "c":
                tui_code(stdscr, scope, days_ago, code_state)
                continue  # back to the report, unchanged
            if ch == "s":
                new_settings = tui_wizard(stdscr, initial_scope=scope, initial_days_ago=days_ago)
                if new_settings is not None:
                    scope, days_ago = new_settings
                break  # re-gather with the (possibly updated) settings
            if key == 27 or ch == "q":
                return  # quit


def tui_app(stdscr):
    curses.curs_set(0)
    if curses.has_colors():
        curses.init_pair(QR_COLOR_PAIR, curses.COLOR_BLACK, curses.COLOR_WHITE)
    wizard_result = tui_wizard(stdscr)
    if wizard_result is None:
        return True  # cancelled before ever showing a report
    scope, days_ago = wizard_result
    tui_results(stdscr, scope, days_ago)
    return False


def tui_main():
    # curses needs the terminal's own encoding to draw the QR code's block characters.
    locale.setlocale(locale.LC_ALL, "")
    try:
        cancelled = curses.wrapper(tui_app)
    except KeyboardInterrupt:
        print("Cancelled.")
        return
    if cancelled:
        print("Cancelled.")


if __name__ == "__main__":
    if len(sys.argv) == 1 and sys.stdout.isatty():
        tui_main()
    else:
        main()
