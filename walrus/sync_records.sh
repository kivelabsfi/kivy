#!/bin/bash
# Every minute: save new Kivy security records to Walrus Memory. Status for watchdog.py in .status.json.
# Runs as user kivy from kivy-walrus.timer -> kivy-walrus.service (sandboxed; OWASP A03-1, 2026-10-29: was a cron, so the
# third-party npm package in upload.mjs ran as root with a reviewer node). Node is Kivy's own /opt/kivy/node.
# The Walrus saver, end to end: export_records.py (new records as JSON) | upload.mjs (store them on Walrus Memory).
# Sends nothing when nothing is new.
# Out: .status.json ({ok, at, saved | err}, read by tools/watchdog.py and kivystats) and run.log (one line per upload).
#   sync_records.sh --dry   # export + count only, check node / memwal / key access; uploads nothing, writes no status
export PATH=/opt/kivy/node/bin:/usr/bin:/bin
cd /srv/kivy/walrus || exit 1
# root (the old cron line): once the kivy-walrus timer runs the saver, a root run does nothing — npm code never runs as root
if [ "$(id -u)" = 0 ] && systemctl is-active --quiet kivy-walrus.timer; then exit 0; fi
# one run at a time: a run still uploading makes the next one exit at once
exec 9>.lock; flock -n 9 || exit 0
# step 1: build the new records; if export fails, record the error and stop
if [ "$1" = "--dry" ]; then
  out=$(python3 export_records.py 2>&1) || { echo "DRY export failed: $(echo "$out" | tail -3)"; exit 1; }
  echo "DRY user=$(id -un) node=$(command -v node) $(node --version)"
  echo "DRY new records: $(echo "$out" | python3 -c 'import json,sys;r=json.load(sys.stdin);print(len(r), [x["key"] for x in r][:10])')"
  test -r /opt/kivy/data/memwal.key && echo "DRY memwal.key readable" || echo "DRY memwal.key NOT readable"
  node -e "import('@mysten-incubation/memwal').then(m => console.log('DRY memwal module loads:', typeof m.MemWal))" 2>&1 | tail -1
  for f in ledger.jsonl senders.json .status.json run.log; do test -w $f && echo "DRY $f writable" || echo "DRY $f NOT writable"; done
  exit 0
fi
out=$(python3 export_records.py 2>&1) || { echo "{\"ok\":false,\"at\":\"$(date -u +%FT%TZ)\",\"err\":\"export failed\"}" > .status.json; echo "$out" | tail -3 >> run.log; exit 1; }
n=$(echo "$out" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
# nothing new: report ok and stop
if [ "$n" = "0" ]; then echo "{\"ok\":true,\"at\":\"$(date -u +%FT%TZ)\",\"saved\":0}" > .status.json; exit 0; fi
# step 2: upload (cut off after 500 s); its last line is the summary
res=$(echo "$out" | timeout 500 node upload.mjs 2>&1); rc=$?
echo "$(date -u +%FT%TZ) $n new: $(echo "$res" | tail -1)" >> run.log
last=$(echo "$res" | tail -1)
# ok only when node exited 0 and the summary says nothing failed
if [ $rc -eq 0 ] && echo "$last" | grep -q '"failed":0'; then echo "{\"ok\":true,\"at\":\"$(date -u +%FT%TZ)\",\"saved\":$n}" > .status.json
else echo "{\"ok\":false,\"at\":\"$(date -u +%FT%TZ)\",\"err\":$(echo "$res" | tail -2 | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()[:200]))')}" > .status.json; fi
