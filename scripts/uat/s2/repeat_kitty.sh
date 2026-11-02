#!/usr/bin/env bash
# Run one terminal+theme N times in a row and count the verdicts. Usage: repeat_kitty.sh <earthwave> <fixture> <outdir> [terminal] [n]
# Env: THEME, WRAP_INPUT (1 = log the bytes the app receives). Keeps every iteration's frames, logs and verdict.
EW=$1 FIX=$2 OUT=$3 TERM_NAME=${4:-kitty} N=${5:-20}
here=$(dirname "$0")
pass=0 fail=0
for i in $(seq "$N"); do
  d="$OUT/iter$(printf %02d "$i")"
  mkdir -p "$d"
  if bash "$here/xvfb_terminal.sh" "$TERM_NAME" "$EW" "$FIX" "$d" >"$d.out" 2>&1; then pass=$((pass + 1)); r=PASS; else fail=$((fail + 1)); r=FAIL; fi
  # a Help overlay on screen at start shows as the 'Keys' heading; record whether the app received a '?' byte
  q=""; [ -f "$d/app-input.bin" ] && q=" input_has_question_mark=$(grep -c '?' "$d/app-input.bin")"
  echo "iter $i $r $(tail -1 "$d/verdict" 2>/dev/null)$q" | tee -a "$OUT/summary.txt"
  pkill -f "Xvfb :99" 2>/dev/null; sleep 1
done
echo "TOTAL $TERM_NAME ${THEME:-dark}: pass=$pass fail=$fail of $N" | tee -a "$OUT/summary.txt"
[ "$fail" -eq 0 ]
