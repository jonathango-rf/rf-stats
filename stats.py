#!/usr/bin/env python3
import argparse
import base64
import csv
import curses
import glob
import json
import os
import re
import socket
import struct
import subprocess
import sys
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

# The station type each run mode lands in on the tracker side. A UR machine does
# both, so the mode -- not the hostname -- is what tells the two apart.
CODE_MODES = ("gello", "inference")


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


def build_code_payload(stats, target_date, host, mode):
    """The tracker's import envelope, carrying this one station's run.

    Shaped as a list of stations even though a run only ever produces one, so a
    single code and a combined multi-station code decode through the same path.
    """
    return {
        "v": 1,
        "type": "shift-import",
        "date": (target_date or date.today()).isoformat(),
        "stations": [{"mode": mode, "host": host, "ops": code_ops(stats)}],
    }


def encode_code(payload):
    """Encode a payload as one copy-pasteable line: RF1: + unpadded base64url."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def code_summary(payload):
    """One-line human check of what a code holds, so it can be confirmed before pasting."""
    station = payload["stations"][0]
    return (f"{station['host']}  {station['mode']}  {payload['date']}  "
            f"{len(station['ops'])} operator(s)")


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
    parser.add_argument("--name", metavar="NAME", default=None,
                         help=f"Station name carried in --code (default: this machine's hostname, "
                              f"{socket.gethostname()!r}). Set it to the label the Shift Report uses "
                              "for this station so the code lands on it automatically.")
    parser.add_argument("--mode", choices=CODE_MODES, default="gello",
                         help="Which station block --code targets: 'gello' (default) or 'inference'.")
    parser.add_argument("-i", dest="mode", action="store_const", const="inference",
                         help="Shorthand for --mode inference.")
    args = parser.parse_args()
    level = min(args.verbose, 2)

    dirs = dirs_for(args.dir)
    # A code stands for one day's station report, so it defaults to today rather
    # than to the all-time scan a bare run does.
    days_ago = 0 if args.code and args.days_ago is None else args.days_ago
    target_date = date.today() - timedelta(days=days_ago) if days_ago is not None else None

    # A code carries one row per operator, so it always gathers at level 0 --
    # -v/-vv only shape the printed table.
    stats = gather_stats(dirs, target_date, 0 if args.code else level)

    if args.code:
        payload = build_code_payload(stats, target_date,
                                     args.name or socket.gethostname(), args.mode)
        # Summary to stderr, code alone to stdout, so `stats.py --code | pbcopy` stays clean.
        print(code_summary(payload), file=sys.stderr)
        print(encode_code(payload))
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


def tui_code(stdscr, scope, days_ago, code_state):
    """Show the pasteable Shift Report code for the current filters and offer to copy it.

    code_state carries the station name and run mode between visits, so they are
    set once per session rather than on every look. The scan runs at level 0 --
    one row per operator, matching what a station block holds.
    """
    stdscr.erase()
    _addstr(stdscr, 0, 2, "Building code...", curses.A_DIM)
    stdscr.refresh()

    target_date = date.today() - timedelta(days=days_ago) if days_ago is not None else None
    stats = gather_stats(dirs_for(scope), target_date, 0)
    status = ""

    while True:
        payload = build_code_payload(stats, target_date, code_state["name"], code_state["mode"])
        code = encode_code(payload)

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
        _addstr(stdscr, h - 1, 2, "c copy   n name   i toggle mode   q back"[:width], curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        ch = key_char(key)
        if ch == "c":
            tool = copy_to_clipboard(code)
            status = (f"Copied to the clipboard with {tool}." if tool else
                      "No clipboard tool found -- select the code above to copy it.")
        elif ch == "n":
            code_state["name"] = curses_prompt_str(
                stdscr, "Station name (as labelled in the Shift Report)",
                default=code_state["name"])
            status = ""
        elif ch == "i":
            code_state["mode"] = "inference" if code_state["mode"] == "gello" else "gello"
            status = ""
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
    code_state = {"name": socket.gethostname(), "mode": "gello"}
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
    wizard_result = tui_wizard(stdscr)
    if wizard_result is None:
        return True  # cancelled before ever showing a report
    scope, days_ago = wizard_result
    tui_results(stdscr, scope, days_ago)
    return False


def tui_main():
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
