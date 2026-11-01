#!/usr/bin/env bash
# S2 spike: foot under headless sway (WLR_BACKENDS=headless), wlrctl for the pointer, grim for the PNG.
# Usage: foot_sway.sh <earthwave> <fixture> <outdir>
set -uo pipefail
EW=$1 FIX=$2 OUT=$3
TERMNAME=foot
COLS=${COLS:-130} ROWS=${ROWS:-32}
CLICK_ROW=${CLICK_ROW:-8} SEL_ROW=${SEL_ROW:-2} LIST_COLS=${LIST_COLS:-70}
mkdir -p "$OUT"
log() { echo "[$TERMNAME] $*" | tee -a "$OUT/$TERMNAME.log"; }
export XDG_RUNTIME_DIR="$OUT/run" LANG=C.UTF-8 TERM=xterm-256color
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
cat >"$OUT/sway.conf" <<'CONF'
output HEADLESS-1 resolution 1600x900 position 0 0
for_window [app_id="foot"] floating enable, move position 0 0
default_border none
CONF
sway -c "$OUT/sway.conf" >"$OUT/sway.log" 2>&1 &
SWAY=$!
trap 'kill $SWAY 2>/dev/null' EXIT
for _ in $(seq 50); do ls "$XDG_RUNTIME_DIR"/wayland-* >/dev/null 2>&1 && break; sleep 0.2; done
export SWAYSOCK=$(ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock 2>/dev/null | head -1)
export WAYLAND_DISPLAY=$(basename "$(ls "$XDG_RUNTIME_DIR"/wayland-* | grep -v lock | head -1)")
log "sway socket $WAYLAND_DISPLAY"

mkdir -p "$OUT/home"; export HOME="$OUT/home"
foot -W "${COLS}x${ROWS}" "$EW" --no-config --replay "$FIX" --hours 720 --min-mag 2.5 --glyphs unicode >"$OUT/foot.stdout" 2>&1 &
TPID=$!
rect=""
for _ in $(seq 40); do
  rect=$(swaymsg -t get_tree | jq -r '.. | objects | select(.app_id? == "foot") | "\(.rect.x) \(.rect.y) \(.rect.width) \(.rect.height)"' | head -1)
  [ -n "$rect" ] && break; sleep 0.5
done
[ -z "$rect" ] && { log "NO WINDOW"; tail -20 "$OUT/foot.stdout"; exit 1; }
read -r gx gy gw gh <<<"$rect"
log "window ${gw}x${gh}+${gx}+${gy}"
prev=""; stable=0
for _ in $(seq 90); do
  grim "$OUT/probe.png" 2>/dev/null
  h=$(convert "$OUT/probe.png" -resize 200x -depth 4 txt:- 2>/dev/null | md5sum | cut -c1-12)
  colors=$(convert "$OUT/probe.png" -format %k info:)
  if [ "$h" = "$prev" ] && [ "$colors" -gt 40 ]; then stable=$((stable + 1)); else stable=0; fi
  prev=$h; [ "$stable" -ge 4 ] && break; sleep 1
done
log "stable (colours=$colors)"
grim "$OUT/$TERMNAME-before.png"
cw=$((gw / COLS)); ch=$((gh / ROWS))
cx=$((gx + 12 * cw)); cy=$((gy + CLICK_ROW * ch + ch / 2))
log "cell ${cw}x${ch} click at $cx,$cy"
. "$(dirname "$0")/verify.sh"
wlrctl --help 2>&1 | head -20 | tee -a "$OUT/wlrctl-help.txt" >/dev/null
swaymsg -t get_seats | tee "$OUT/seats.json" >/dev/null
wlrctl pointer move -5000 -5000; sleep 0.2
wlrctl pointer move "$((cx - 30))" "$cy"; sleep 0.2
method=none
try() { # try <label> <command...>: run a click variant, screenshot, verify
  local label=$1; shift
  "$@" >>"$OUT/click.log" 2>&1; sleep 1.5
  grim "$OUT/$TERMNAME-after.png"; log "variant $label"
  if verify "$TERMNAME" "$OUT"; then method=$label; return 0; fi; return 1
}
seat=$(swaymsg -t get_seats | jq -r '.[0].name')
try wlrctl-move-click-chained wlrctl pointer move 30 0 click left ||
try wlrctl-move-move-click-chained wlrctl pointer move 1 0 move -1 0 click left ||
try wlrctl-click-only wlrctl pointer click left ||
try swaymsg-seat-cursor bash -c "swaymsg 'seat $seat cursor set $cx $cy'; sleep 0.3; swaymsg 'seat $seat cursor press button1'; sleep 0.1; swaymsg 'seat $seat cursor release button1'" || true
echo "method=$method $VERDICT_NUMBERS" >>"$OUT/verdict"
kill "$TPID" 2>/dev/null; true
