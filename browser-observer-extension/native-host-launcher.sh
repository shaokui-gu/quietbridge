#!/bin/sh
umask 077
log=""
if [ -n "$HOME" ] && [ -d "$HOME/Library/Logs" ]; then
  log="$HOME/Library/Logs/WorkbuddyExtension-native-host.log"
fi
if [ -z "$log" ] && [ -n "$TMPDIR" ] && [ -d "$TMPDIR" ]; then
  log="$TMPDIR/workbuddy-extension-native-host.log"
fi
if [ -n "$log" ] && { exec 3>> "$log"; } 2>/dev/null; then :; else exec 3>/dev/null; fi
printf "%s launch pid=%s argc=%s arg1=%s home=%s\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$#" "${1:-none}" "${HOME:-unset}" >&3
script_dir=$(CDPATH= cd -P "$(/usr/bin/dirname "$0")" && pwd -P) || {
  printf "%s script-dir-failed\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" >&3
  exit 1
}
host="$script_dir"/native_host.py
if [ ! -f "$host" ]; then
  printf "%s host-missing path=%s\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$host" >&3
  printf "%s\n" "Native host missing beside launcher: $host" >&2
  exit 1
fi
printf "%s host-start pid=%s python=%s\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" /usr/bin/python3 >&3
/usr/bin/python3 -B "$host" "$@" 2>&3
status=$?
printf "%s host-exit pid=%s code=%s\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$status" >&3
exit "$status"
