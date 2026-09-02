#!/usr/bin/env bash
set -euo pipefail

config=""
while (($#)); do
  case "$1" in
    --config) config=${2:?missing value for --config}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$config" ]]; then
  echo "usage: $0 --config FILE" >&2
  exit 2
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
config=$(realpath "$config")
audit_root=$(python -c 'import sys; from gb_dicom2bids.config import load_config; print(load_config(sys.argv[1]).paths.audit_root)' "$config")
launch_file="$audit_root/run.launch"
if [[ ! -f "$launch_file" ]]; then
  echo "no launch record found: $launch_file"
  exit 0
fi
pid=$(awk -F= '$1 == "PID" {print $2}' "$launch_file")
pgid=$(awk -F= '$1 == "PGID" {print $2}' "$launch_file")
recorded_cwd=$(awk -F= '$1 == "CWD" {sub(/^CWD=/, ""); print}' "$launch_file")
if [[ ! "$pid" =~ ^[0-9]+$ || ! "$pgid" =~ ^[0-9]+$ ]]; then
  echo "invalid PID/PGID in $launch_file" >&2
  exit 1
fi
if ! kill -0 "$pid" 2>/dev/null; then
  echo "recorded PID $pid is not running"
  exit 0
fi
actual_cwd=$(readlink -f "/proc/$pid/cwd")
command=$(tr '\0' ' ' <"/proc/$pid/cmdline")
if [[ "$recorded_cwd" != "$repo_root" || "$actual_cwd" != "$repo_root" ]]; then
  echo "refusing to stop PID $pid: working directory does not match this repository" >&2
  exit 1
fi
if [[ "$command" != *"gb-dicom2bids"* ]]; then
  echo "refusing to stop PID $pid: command does not match this pipeline" >&2
  exit 1
fi

kill -TERM -- "-$pgid"
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    python -c 'import sys; from gb_dicom2bids.config import load_config; from gb_dicom2bids.runtime import update_run_state; update_run_state(load_config(sys.argv[1]), "stopped")' "$config"
    echo "stopped process group $pgid with TERM"
    exit 0
  fi
  sleep 1
done
echo "process group $pgid did not exit after 60 seconds; no KILL was sent" >&2
exit 1
