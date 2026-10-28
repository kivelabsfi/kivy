#!/bin/bash
# cron, every minute: save new Kivy security records to Walrus Memory. Status for kivy_watch.py in .status.json.
cd /srv/kivy/walrus || exit 1
exec 9>.lock; flock -n 9 || exit 0
out=$(python3 export.py 2>&1) || { echo "{\"ok\":false,\"at\":\"$(date -u +%FT%TZ)\",\"err\":\"export failed\"}" > .status.json; echo "$out" | tail -3 >> run.log; exit 1; }
n=$(echo "$out" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
if [ "$n" = "0" ]; then echo "{\"ok\":true,\"at\":\"$(date -u +%FT%TZ)\",\"saved\":0}" > .status.json; exit 0; fi
res=$(echo "$out" | timeout 500 node upload.mjs 2>&1); rc=$?
echo "$(date -u +%FT%TZ) $n new: $(echo "$res" | tail -1)" >> run.log
last=$(echo "$res" | tail -1)
if [ $rc -eq 0 ] && echo "$last" | grep -q '"failed":0'; then echo "{\"ok\":true,\"at\":\"$(date -u +%FT%TZ)\",\"saved\":$n}" > .status.json
else echo "{\"ok\":false,\"at\":\"$(date -u +%FT%TZ)\",\"err\":$(echo "$res" | tail -2 | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()[:200]))')}" > .status.json; fi
