# rf-stats

`stats.py` — per-operator session counts and times for the RoboForce capture
stations. It runs **on the station PC**, reading sessions straight out of
`/opt/roboforce-core-apps` (`captures*`, `upload`, `successfully_uploaded`).

```
python3 stats.py                  # interactive menu (no arguments)
python3 stats.py -t 0             # today, per operator
python3 stats.py -t 0 -v          # split by task type, with average scores
python3 stats.py -t 0 -vv         # ... and by event object
python3 stats.py -d local         # scan the dev-machine copy of core-apps
```

Stdlib only — no install, no virtualenv. Durations prefer MCAP timestamps,
falling back to `realsense_log.csv` and then head-image count, so a session
missing one source still reports.

The interactive TUI stays open and re-scans on Enter, which makes it usable as a
live progress board while a station records.

## Handing numbers to the Shift Report

`--code` prints the day as one pasteable line instead of a table, so numbers
never have to be retyped into the rf-admin Shift Report:

```
python3 stats.py --code --name UR1   # names this station, and remembers it
python3 stats.py --code              # every run after — today's numbers
python3 stats.py --code -t 1         # yesterday
python3 stats.py --code | pbcopy     # straight to the clipboard
python3 stats.py --qr                # ... and draw it for a phone to scan
python3 stats.py --qr --email lead@example.com   # ... already addressed
```

The code goes to stdout and a one-line check — station, date, operator count —
to stderr, so piping stays clean. It covers **today** unless `-t` says otherwise,
since a code stands for one day's report.

The TUI has the same thing on `c`: `n` sets the station name, `c` copies via
`pbcopy`, `wl-copy`, `xclip`, or `xsel`, and `p` fills the screen with the QR code.

### Getting the code onto a phone

The clipboard only helps on the station PC. `--qr` draws the same code as a QR
code so a phone camera can pick it up off the screen and carry it anywhere —
whoever needs the numbers is rarely sitting at the station.

Scanning it opens the phone's mail app on a draft with the code already in the
body and the station and date in the subject, ready to be addressed and sent.
`--email ADDRESS` fills the recipient in too.

That wrapping is not decoration. A bare code scans as plain text, which iOS
decodes and then throws away — the camera says "no usable data" and leaves
nothing to copy. Phone cameras only act on payloads they recognise, so the code
travels as a mail link:

```
mailto:?subject=UR1%202026-09-09%20station%20stats&body=RF2%3AAg0pAQAD...
```

The QR code is always drawn dark-on-light, whatever the terminal's colours are:
inverted, most phones will not read it. A twelve-operator day needs a window
about 70 columns by 36 rows; below that the TUI says so rather than showing a
clipped code that would scan as nothing.

### Naming a station once

`--name` writes the name to `~/.rf-station`, so it only has to be given the first
time on a machine. Set it to the label the Shift Report uses for that station and
every later code routes itself to the right block.

The name is resolved most-explicit-first:

1. `--name NAME` on the command line — and this is what saves it
2. `$RF_STATION` — a one-off override that does **not** overwrite the saved name
3. `~/.rf-station` — what was saved earlier
4. the machine's hostname, as a last resort

Setting the name in the TUI (`c` then `n`) saves it the same way. If the file
can't be written, the name still applies to the run in hand.

The paste box on the other end lives in the rf-admin repo, documented in
`docs/station-stats-code.md`.

### Code format

Codes are `RF2:` followed by base64url over a packed binary body. The earlier
`RF1:` codes spelled the same numbers out as JSON, which ran past a thousand
characters on a busy day — far too dense to scan. RF2 is about five times
shorter and carries exactly the same numbers.

**A station on RF2 needs a Shift Report that understands it**, so deploy the
tracker first. The tracker still accepts `RF1:` codes, so stations can be updated
one at a time.

## Fixtures

`data/` holds sample sessions for trying the script out without a station:

```
python3 stats.py -d data
```

It is gigabytes of recordings, so it is deliberately **not** tracked — clone
this repo and the folder simply will not be there.
