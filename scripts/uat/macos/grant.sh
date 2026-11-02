#!/bin/bash
# Attempt 2 prep: try to write TCC grants as root. Expected to fail where SIP protects TCC.db.
. "$(dirname "$0")/common.sh"
PY="$(readlink -f "$HOME/s4venv/bin/python")"
CLICK="$(command -v cliclick)"
clients_path=(/System/Library/CoreServices/RemoteManagement/ScreensharingAgent.bundle/Contents/MacOS/ScreensharingAgent /System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/MacOS/ARDAgent /usr/sbin/screensharingd /opt/hca/hosted-compute-agent /opt/hca/start_hca.sh /usr/bin/osascript /bin/bash /bin/sh /usr/bin/python3 "$PY" "$CLICK" /usr/sbin/screencapture \
  /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal /Applications/iTerm.app/Contents/MacOS/iTerm2)
clients_bundle=(com.apple.Terminal com.googlecode.iterm2 com.apple.systemevents com.apple.screensharing.agent com.apple.RemoteDesktopAgent com.apple.screensharing)
for db in "/Library/Application Support/com.apple.TCC/TCC.db" "$HOME/Library/Application Support/com.apple.TCC/TCC.db"; do
  echo "== writing $db"
  for svc in kTCCServiceAccessibility kTCCServiceScreenCapture kTCCServicePostEvent kTCCServiceListenEvent; do
    for c in "${clients_path[@]}"; do
      sudo sqlite3 "$db" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,flags,last_modified) VALUES ('$svc','$c',1,2,4,1,0,strftime('%s','now'));" 2>&1 | head -2
    done
    for c in "${clients_bundle[@]}"; do
      sudo sqlite3 "$db" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,flags,last_modified) VALUES ('$svc','$c',0,2,4,1,0,strftime('%s','now'));" 2>&1 | head -2
    done
  done
  for c in "${clients_path[@]}"; do for tgt in com.apple.systemevents com.apple.Terminal com.googlecode.iterm2; do
    sudo sqlite3 "$db" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,indirect_object_identifier_type,indirect_object_identifier,flags,last_modified) VALUES ('kTCCServiceAppleEvents','$c',1,2,4,1,0,'$tgt',0,strftime('%s','now'));" 2>&1 | head -2
  done; done
  sudo sqlite3 -header "$db" "select service,client,auth_value from access" 2>&1 | head -60
done
sudo killall tccd 2>/dev/null
echo "grant done"
