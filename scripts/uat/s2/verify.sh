#!/usr/bin/env bash
# Shared by the S2 scripts. verify <name> <outdir>: needs gx gy cw ch CLICK_ROW SEL_ROW LIST_COLS and
# <outdir>/<name>-before.png, <name>-after.png. Writes <outdir>/verdict (PASS|FAIL) and logs the changed-pixel counts.
band() { # band <png> <row> -> crop of the list pane for that row
  convert "$1" -crop "$((LIST_COLS * cw))x${ch}+${gx}+$((gy + $2 * ch))" +repage miff:-
}
diffpx() { compare -metric AE -fuzz 6% "$1" "$2" null: 2>&1 | awk '{print int($1)}'; }
verify() {
  local n=$1 o=$2 dc ds df
  band "$o/$n-before.png" "$CLICK_ROW" >"$o/c-before.miff"; band "$o/$n-after.png" "$CLICK_ROW" >"$o/c-after.miff"
  band "$o/$n-before.png" "$SEL_ROW" >"$o/s-before.miff";   band "$o/$n-after.png" "$SEL_ROW" >"$o/s-after.miff"
  band "$o/$n-before.png" $((CLICK_ROW + 6)) >"$o/f-before.miff"; band "$o/$n-after.png" $((CLICK_ROW + 6)) >"$o/f-after.miff"
  dc=$(diffpx "$o/c-before.miff" "$o/c-after.miff"); ds=$(diffpx "$o/s-before.miff" "$o/s-after.miff"); df=$(diffpx "$o/f-before.miff" "$o/f-after.miff")
  log "changed pixels: clicked row=$dc, previously selected row=$ds, untouched row=$df"
  VERDICT_NUMBERS="clicked=$dc previous=$ds untouched=$df"
  if [ "$dc" -gt 200 ] && [ "$ds" -gt 200 ] && [ "$df" -lt 50 ]; then
    log "SELECTION CHANGED (verdict from pixels; the PNGs still have to be viewed)"; echo PASS >"$o/verdict"
    return 0
  fi
  log "SELECTION NOT CONFIRMED"; echo FAIL >"$o/verdict"; return 1
}
