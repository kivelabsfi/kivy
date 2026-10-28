#!/bin/bash
# cron, every minute: save new Kivy security records to Walrus Memory. Status for watchdog.py in .status.json.
# The Walrus saver, end to end: export_records.py (new records as JSON) | upload.mjs (store them on Walrus Memory).
# Sends nothing when nothing is new.
# Out: .status.json ({ok, at, saved | err}, read by tools/watchdog.py) and run.log (one line per upload).
cd /srv/kivy/walrus || exit 1
# one run at a time: a run still uploading makes the next one exit at once
exec 9>.lock; flock -n 9 || exit 0
# step 1: build the new records; if export fails, record the error and stop
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
