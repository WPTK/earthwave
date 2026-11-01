#!/usr/bin/env bash
# S2 spike: run the released Linux earthwave in one real terminal under Xvfb, click a quake row with xdotool, and prove
# from ImageMagick screenshots that the selection changed. Usage: xvfb_terminal.sh <terminal> <earthwave> <fixture> <outdir>
# terminals: xterm-vt340 xterm gnome-terminal wezterm kitty alacritty
set -uo pipefail
TERMNAME=$1 EW=$2 FIX=$3 OUT=$4
THEME=${THEME:-dark}   # dark | light: the terminal's own background and foreground
if [ "$THEME" = light ]; then BG=ffffff FG=000000; else BG=000000 FG=e0e0e0; fi
COLS=${COLS:-130} ROWS=${ROWS:-32}
CLICK_ROW=${CLICK_ROW:-8}     # 0-based screen row of the quake to click (a list row below the first)
SEL_ROW=${SEL_ROW:-2}         # 0-based screen row of the quake selected at start
LIST_COLS=${LIST_COLS:-70}    # width in cells of the list pane
mkdir -p "$OUT"
export DISPLAY=:99 LANG=C.UTF-8 TERM=xterm-256color COLORTERM=truecolor
unset NO_COLOR
log() { echo "[$TERMNAME] $*" | tee -a "$OUT/$TERMNAME.log"; }

Xvfb :99 -screen 0 1600x900x24 +extension GLX +render -noreset >"$OUT/xvfb.log" 2>&1 &
XVFB=$!
trap 'kill $XVFB 2>/dev/null' EXIT
for _ in $(seq 50); do xdotool getdisplaygeometry >/dev/null 2>&1 && break; sleep 0.2; done

APP="$EW --no-config --replay $FIX --hours 720 --min-mag 2.5 --glyphs unicode"
mkdir -p "$OUT/home" ; export HOME="$OUT/home" XDG_CONFIG_HOME="$OUT/home/.config" XDG_RUNTIME_DIR="$OUT/run"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME"; chmod 700 "$XDG_RUNTIME_DIR"

case $TERMNAME in
  xterm-vt340) CMD=(xterm +lc -ti vt340 -geometry "${COLS}x${ROWS}" -fa Monospace -fs 11 -xrm 'XTerm*allowWindowOps: true' -bg "#$BG" -fg "#$FG" -e $APP) ;;
  xterm)       CMD=(xterm -geometry "${COLS}x${ROWS}" -fa Monospace -fs 11 -bg "#$BG" -fg "#$FG" -e $APP) ;;
  gnome-terminal)
    cat >"$OUT/gt.sh" <<GT
P=b1dcc9dd-5262-4d8d-a863-c897e6d979b9
gsettings set org.gnome.Terminal.ProfilesList list "['\$P']"
gsettings set org.gnome.Terminal.ProfilesList default "\$P"
K="org.gnome.Terminal.Legacy.Profile:/org/gnome/terminal/legacy/profiles:/:\$P/"
gsettings set "\$K" use-theme-colors false
gsettings set "\$K" background-color "#$BG"
gsettings set "\$K" foreground-color "#$FG"
exec gnome-terminal --wait --hide-menubar --geometry=${COLS}x${ROWS} -- $APP
GT
    CMD=(dbus-run-session -- bash "$OUT/gt.sh") ;;
  gnome-terminal-old) CMD=(dbus-run-session -- gnome-terminal --wait --hide-menubar --geometry="${COLS}x${ROWS}" -- $APP) ;;
  kitty)       CMD=(kitty -o initial_window_width=${COLS}c -o initial_window_height=${ROWS}c -o background="#$BG" -o foreground="#$FG" -o remember_window_size=no -o confirm_os_window_close=0 $APP) ;;
  alacritty)   CMD=(alacritty -o window.dimensions.columns=$COLS -o window.dimensions.lines=$ROWS -o "colors.primary.background=\"#$BG\"" -o "colors.primary.foreground=\"#$FG\"" -e $APP) ;;
  wezterm)
    cat >"$OUT/wezterm.lua" <<LUA
local wezterm = require 'wezterm'
return { initial_cols = $COLS, initial_rows = $ROWS, front_end = 'Software', enable_tab_bar = false,
  colors = { background = '#$BG', foreground = '#$FG' }, window_decorations = 'NONE', window_padding = { left = 0, right = 0, top = 0, bottom = 0 }, check_for_updates = false, enable_wayland = false }
LUA
    CMD=(env APPIMAGE_EXTRACT_AND_RUN=1 "$WEZTERM" --config-file "$OUT/wezterm.lua" start --always-new-process -- $APP) ;;
  *) echo "unknown terminal $TERMNAME"; exit 2 ;;
esac

log "launch: ${CMD[*]}"
"${CMD[@]}" >"$OUT/$TERMNAME.stdout" 2>&1 &
TPID=$!

# the terminal window: the largest visible window
win=""
for _ in $(seq 60); do
  best=0
  for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
    eval "$(xdotool getwindowgeometry --shell "$w" 2>/dev/null)" || continue
    a=$((WIDTH * HEIGHT)); if [ "$a" -gt "$best" ] && [ "$WIDTH" -gt 400 ] && [ "$WIDTH" -lt 1600 ]; then best=$a; win=$w; gw=$WIDTH; gh=$HEIGHT; gx=$X; gy=$Y; fi
  done
  [ -n "$win" ] && break
  sleep 0.5
done
if [ -z "$win" ]; then log "NO WINDOW appeared"; cat "$OUT/$TERMNAME.stdout" | tail -20; kill $TPID 2>/dev/null; exit 1; fi
log "window $win ${gw}x${gh}+${gx}+${gy}"

# wait for a stable, non-trivial picture (the dashboard has drawn)
prev=""; stable=0
for _ in $(seq 90); do
  import -window root "$OUT/probe.png" 2>/dev/null
  h=$(convert "$OUT/probe.png" -resize 200x -depth 4 txt:- 2>/dev/null | md5sum | cut -c1-12)
  colors=$(convert "$OUT/probe.png" -format %k info:)
  if [ "$h" = "$prev" ] && [ "$colors" -gt 40 ]; then stable=$((stable + 1)); else stable=0; fi
  prev=$h; [ "$stable" -ge 4 ] && break; sleep 1
done
log "stable after probe (colours=$colors)"
import -window root "$OUT/$TERMNAME-before.png"

cw=$((gw / COLS)); ch=$((gh / ROWS))
cx=$((gx + 12 * cw)); cy=$((gy + CLICK_ROW * ch + ch / 2))
log "cell ${cw}x${ch} click at $cx,$cy (row $CLICK_ROW)"
xdotool windowfocus "$win" 2>/dev/null; sleep 0.5
. "$(dirname "$0")/verify.sh"
for attempt in 1 2; do
  xdotool mousemove $((cx - 30)) "$cy"; sleep 0.2; xdotool mousemove "$cx" "$cy"; sleep 0.3
  xdotool click 1; sleep 1.5
  import -window root "$OUT/$TERMNAME-after.png"
  log "click attempt $attempt"
  verify "$TERMNAME" "$OUT" && break
done
echo "attempts=$attempt $VERDICT_NUMBERS" >>"$OUT/verdict"
kill "$TPID" 2>/dev/null; sleep 1; pkill -P "$TPID" 2>/dev/null
# the job must go red when the click did not move the selection
grep -q '^PASS' "$OUT/verdict"
