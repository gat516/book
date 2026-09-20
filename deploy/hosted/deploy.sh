#!/usr/bin/env bash
# Apply one already-built, immutable release. DATABASE_URL here is operator-only.
set -euo pipefail
: "${DATABASE_URL:?migration connection required}"
manifest="${1:?rendered manifest file required}"
# Stop periodic maintenance before quiescing application writers. A failed migration
# deliberately leaves services stopped; schema changes are forward-only (§15).
if kubectl -n book get deployment reader-api >/dev/null 2>&1; then
  kubectl -n book patch cronjob portable-backup --type=merge -p '{"spec":{"suspend":true}}'
  kubectl -n book patch cronjob account-cleanup --type=merge -p '{"spec":{"suspend":true}}'
  for attempt in {1..30}; do
    active="$(kubectl -n book get jobs -o json | python -c 'import json,sys; print(sum(j.get("status",{}).get("active",0) for j in json.load(sys.stdin)["items"]))')"
    [[ "$active" == 0 ]] && break
    if [[ "$attempt" == 30 ]]; then
      echo 'Maintenance jobs are still running; let them finish before retrying.' >&2
      exit 1
    fi
    sleep 2
  done
  kubectl -n book scale deployment reader-api ingest-api scraper pipeline askai --replicas=0
  kubectl -n book wait --for=delete pod -l 'app in (reader-api,ingest-api,scraper,pipeline,askai)' --timeout=180s
fi
python db/migrate.py
kubectl apply -f "$manifest"
for app in redis textproc ingest-api scraper pipeline askai reader-api web; do
  kubectl -n book rollout status "deployment/$app" --timeout=180s
done
