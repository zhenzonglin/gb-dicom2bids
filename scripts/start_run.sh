#!/usr/bin/env bash
set -euo pipefail

config=""
mode=""
while (($#)); do
  case "$1" in
    --config) config=${2:?missing value for --config}; shift 2 ;;
    --mode) mode=${2:?missing value for --mode}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$config" || -z "$mode" ]]; then
  echo "usage: $0 --config FILE --mode inventory-pilot|full" >&2
  exit 2
fi
if [[ "$mode" != "inventory-pilot" && "$mode" != "full" ]]; then
  echo "unsupported mode: $mode" >&2
  exit 2
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
config=$(realpath "$config")
audit_root=$(python -c 'import sys; from gb_dicom2bids.config import load_config; print(load_config(sys.argv[1]).paths.audit_root)' "$config")
mkdir -p "$audit_root/logs"
launch_file="$audit_root/run.launch"
if [[ -f "$launch_file" ]]; then
  prior_pid=$(awk -F= '$1 == "PID" {print $2}' "$launch_file")
  if [[ "$prior_pid" =~ ^[0-9]+$ ]] && kill -0 "$prior_pid" 2>/dev/null; then
    echo "run already active with PID $prior_pid" >&2
    exit 1
  fi
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
log_file="$audit_root/logs/run_${mode}_${timestamp}.log"
if [[ "$mode" == "inventory-pilot" ]]; then
  command=(gb-dicom2bids run --config "$config" --mode inventory-pilot)
else
  command=(gb-dicom2bids convert --config "$config" --resume)
fi

cd "$repo_root"
nohup setsid "${command[@]}" >"$log_file" 2>&1 < /dev/null &
pid=$!
sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  echo "run failed to stay active; inspect $log_file" >&2
  exit 1
fi
pgid=$(ps -o pgid= -p "$pid" | tr -d ' ')
{
  printf 'PID=%s\n' "$pid"
  printf 'PGID=%s\n' "$pgid"
  printf 'MODE=%s\n' "$mode"
  printf 'CWD=%s\n' "$repo_root"
  printf 'CONFIG=%s\n' "$config"
  printf 'LOG=%s\n' "$log_file"
  printf 'STARTED=%s\n' "$timestamp"
} >"$launch_file"
printf 'started PID=%s PGID=%s log=%s\n' "$pid" "$pgid" "$log_file"
