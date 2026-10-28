#!/usr/bin/env bash
#
# get-serials.sh - collect serial numbers of all connected hardware for inventory
#
# Reports serials for:
#   - This PC (host machine)          via DMI (/sys/class/dmi or dmidecode)
#   - UR5E arms + control boxes       via Dashboard Server (TCP 29999)
#   - gen.ai grippers                 via gen_con_sdk camera_cmd.py MCUID
#   - Intel RealSense camera(s)        via rs-enumerate-devices
#
# Usage:
#   ./get-serials.sh            print a human-readable report
#   ./get-serials.sh -c         print CSV (device,label,field,value)
#   ./get-serials.sh -h         show this help
#
# Notes:
#   - Designed to run ON the Linux PC the hardware is attached to.
#   - Unreachable devices are reported as UNREACHABLE, never fatal.
#   - Some PC serials require root; run with sudo for complete DMI info.
#   - Requires: bash 4+, python3 (grippers), rs-enumerate-devices (RealSense).

set -uo pipefail

# ============================ CONFIG ============================
# Edit these to match the cell. name|ip  (label is free text)
ROBOTS=(
  "left|192.168.12.98"
  "right|192.168.12.99"
)

# gen.ai grippers: label|sdk-arg (the {left/right} passed to camera_cmd.py)
GRIPPERS=(
  "left|left"
  "right|right"
)

GRIPPER_CMD="python3 $HOME/Workspace/gen_con_sdk_python_release/scripts/camera_cmd.py"

UR_DASHBOARD_PORT=29999
NET_TIMEOUT=3          # seconds per network read
# ================================================================

# coreutils `timeout` (Linux) or `gtimeout` (mac w/ coreutils); optional.
if command -v timeout >/dev/null 2>&1; then TIMEOUT_CMD=timeout
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_CMD=gtimeout
else TIMEOUT_CMD=""; fi

CSV=false

# ---- parse flags ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--csv) CSV=true; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ---- output helpers ----
section() { $CSV || { echo; echo "== $1 =="; }; }
# emit DEVICE LABEL FIELD VALUE
emit() {
  local dev="$1" label="$2" field="$3" value="$4"
  if $CSV; then
    printf '%s,%s,%s,%s\n' "$dev" "$label" "$field" "${value//,/;}"
  else
    printf '  %-14s %-18s %s\n' "$label" "$field" "$value"
  fi
}

# ---- send one command to a UR Dashboard Server and echo the reply ----
# The server sends a welcome banner on connect, then answers each line.
# Wrapped in `timeout` so a powered-off/offline robot can't hang the script.
ur_dashboard() {
  UR_IP="$1" UR_CMD="$2" UR_PORT="$UR_DASHBOARD_PORT" NET_TIMEOUT="$NET_TIMEOUT" \
    ${TIMEOUT_CMD:+$TIMEOUT_CMD "$NET_TIMEOUT"} bash -c '
      exec 3<>"/dev/tcp/$UR_IP/$UR_PORT" 2>/dev/null || exit 1
      read -r -t "$NET_TIMEOUT" _banner <&3   # discard "Connected: ..." banner
      printf "%s\n" "$UR_CMD" >&3
      read -r -t "$NET_TIMEOUT" reply <&3 || exit 1
      printf "%s" "$reply"
    ' 2>/dev/null | tr -d '\r'
}

# ============================ PC ============================
report_pc() {
  section "PC / host"
  local host serial product vendor board dmi_sudo=""
  host=$(hostname 2>/dev/null || echo "unknown")
  emit PC "$host" hostname "$host"

  # product_serial in sysfs is root-only. If we're not root but can sudo to
  # dmidecode, prime the sudo prompt once so it doesn't break up the report.
  if [[ $EUID -ne 0 && ! -r /sys/class/dmi/id/product_serial ]] \
     && command -v dmidecode >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    dmi_sudo="sudo"
    if ! sudo -n true 2>/dev/null; then
      echo "  (system serial needs root — enter password for dmidecode)" >&2
      sudo -v || dmi_sudo=""   # give up gracefully if auth fails
    fi
  fi

  # Prefer sysfs; fall back to (optionally sudo) dmidecode.
  read_dmi() {  # $1 = sysfs file, $2 = dmidecode keyword
    local v=""
    [[ -r "/sys/class/dmi/id/$1" ]] && v=$(<"/sys/class/dmi/id/$1")
    if [[ -z "$v" || "$v" == "None" ]] && command -v dmidecode >/dev/null 2>&1; then
      v=$($dmi_sudo dmidecode -s "$2" 2>/dev/null | head -n1)
    fi
    printf '%s' "${v:-N/A}"
  }
  vendor=$(read_dmi sys_vendor system-manufacturer)
  product=$(read_dmi product_name system-product-name)
  serial=$(read_dmi product_serial system-serial-number)
  board=$(read_dmi board_serial baseboard-serial-number)

  emit PC "$host" manufacturer "$vendor"
  emit PC "$host" model "$product"
  emit PC "$host" system_serial "$serial"
  emit PC "$host" board_serial "$board"
}

# ============================ UR5E ============================
report_robots() {
  section "UR5E arms"
  local entry label ip serial model version
  for entry in "${ROBOTS[@]}"; do
    label="${entry%%|*}"; ip="${entry##*|}"
    if ! serial=$(ur_dashboard "$ip" "get serial number"); then
      emit UR5E "$label" status "UNREACHABLE ($ip)"
      continue
    fi
    model=$(ur_dashboard "$ip" "get robot model" || echo "")
    version=$(ur_dashboard "$ip" "PolyscopeVersion" || echo "")
    # Replies look like "Serial Number: 20195500000" / "Robot Model: UR5e"
    emit UR5E "$label" ip "$ip"
    emit UR5E "$label" robot_serial "${serial#*: }"
    [[ -n "$model" ]]   && emit UR5E "$label" model "${model#*: }"
    [[ -n "$version" ]] && emit UR5E "$label" polyscope "$version"
  done
  $CSV || echo "  note: UR exposes one serial via Dashboard (arm/controller pair);"
  $CSV || echo "        the control-box-only serial is on its physical label."
}

# ============================ Grippers ============================
report_grippers() {
  section "gen.ai grippers"
  local entry label arg out mcuid
  for entry in "${GRIPPERS[@]}"; do
    label="${entry%%|*}"; arg="${entry##*|}"
    if ! out=$($GRIPPER_CMD "$arg" MCUID 2>&1); then
      emit gripper "$label" status "ERROR: $(echo "$out" | tail -n1)"
      continue
    fi
    # The SDK prints a log then a "MCUID: <hex>" line; grab that hex value.
    mcuid=$(echo "$out" | grep -m1 -E '^MCUID:' | sed -E 's/^MCUID:[[:space:]]*//')
    if [[ -n "$mcuid" ]]; then
      emit gripper "$label" mcuid "$mcuid"
    else
      emit gripper "$label" status "no MCUID in output"
    fi
  done
}

# ============================ RealSense ============================
report_realsense() {
  section "Intel RealSense camera(s)"
  if ! command -v rs-enumerate-devices >/dev/null 2>&1; then
    emit realsense - status "rs-enumerate-devices not installed"
    return
  fi
  # Parse the labeled full output ("Name : ..." / "Serial Number : ..."), which
  # keeps multi-word device names intact and ignores stray log lines. The
  # "^Serial Number" anchor deliberately skips "Asic Serial Number".
  local parsed found=false name serial
  parsed=$(rs-enumerate-devices 2>/dev/null | awk -F':' '
    /^[[:space:]]*Name[[:space:]]*:/          { v=$2; gsub(/^[[:space:]]+|[[:space:]]+$/,"",v); name=v }
    /^[[:space:]]*Serial Number[[:space:]]*:/ { v=$2; gsub(/^[[:space:]]+|[[:space:]]+$/,"",v); print name "\t" v }
  ')
  while IFS=$'\t' read -r name serial; do
    [[ -z "$serial" ]] && continue
    found=true
    emit realsense "${name:-camera}" serial "$serial"
  done <<<"$parsed"
  $found || emit realsense - status "no RealSense device detected"
}

# ============================ main ============================
$CSV && echo "device,label,field,value"
$CSV || echo "Hardware serial inventory  ($(date '+%Y-%m-%d %H:%M:%S'))"
report_pc
report_robots
report_grippers
report_realsense
$CSV || echo
