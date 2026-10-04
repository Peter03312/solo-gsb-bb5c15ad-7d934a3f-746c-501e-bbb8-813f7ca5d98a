#!/usr/bin/env bash
# Historical revision replay: the application embedded in an approval record
# returned by GET /api/revision/{n} and /api/revision/compare is projected to
# revision n by replaying the append-only event stream.  Later suspensions,
# resumptions, revocations or emergency-occupation releases never rewrite an
# older revision's view; the live endpoints keep showing real-time state.
#
# Usage: ./examples/historical-revision-replay.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

# Extract the embedded application fields of one application's approval record.
embedded() { # $1 = revision, $2 = application id
  curl -s "$BASE/api/revision/$1" | python3 -c '
import json, sys
revision, app_id = sys.argv[1], int(sys.argv[2])
body = json.load(sys.stdin)
(record,) = [a for a in body["approvals"] if a["application_id"] == app_id]
app = record["application"]
status = app["status"]
approval = app["approval_revision"]
suspended = app["suspended_revision"]
forced_by = app["force_suspended_by"]
status_as_of = record["status_as_of"]
print(
    "  record status_as_of=" + str(status_as_of),
    "embedded: status=" + str(status),
    "approval_revision=" + str(approval),
    "suspended_revision=" + str(suspended),
    "force_suspended_by=" + str(forced_by),
)
' "$1" "$2"
}

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. submit A (100-110), approve at rev 0 -> revision 1 =="
A=$(post /api/applications '{
  "name":"History A",
  "circle":{"cx":0,"cy":0,"radius":1},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2030-01-01T00:00:00Z","end":"2030-12-31T00:00:00Z"}
}')
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 2. suspend A at rev 1 -> revision 2 =="
post "/api/applications/$A_ID/suspend" \
  '{"expected_revision":1,"reason":"equipment outage"}' | J

echo "== 3. history at revision 1: approval record still approved =="
embedded 1 "$A_ID"

echo "== 4. history at revision 2: approval record is suspended @2 =="
embedded 2 "$A_ID"

echo "== 5. resume A at rev 2 -> revision 3 (old views stay unchanged) =="
post "/api/applications/$A_ID/resume" \
  '{"expected_revision":2,"reason":"equipment fixed"}' | J
for n in 1 2 3; do
  echo "-- revision $n --"
  embedded "$n" "$A_ID"
done

echo "== 6. emergency occupation: B (200-210) forced out, EM approved =="
B=$(post /api/applications '{
  "name":"History B",
  "circle":{"cx":0,"cy":0,"radius":1},
  "frequency":{"low":200,"high":210},
  "time_window":{"start":"2030-01-01T00:00:00Z","end":"2030-12-31T00:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
EM=$(post /api/applications '{
  "name":"History EM",
  "circle":{"cx":0,"cy":0,"radius":1},
  "frequency":{"low":204,"high":209},
  "time_window":{"start":"2030-01-01T00:00:00Z","end":"2030-12-31T00:00:00Z"}
}')
EM_ID=$(echo "$EM" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
# A is effective at rev 3; approve B at rev 3 -> revision 4.
post "/api/applications/$B_ID/approve" '{"expected_revision":3}' >/dev/null
# Confirm emergency: B force-suspended at revision 5, EM approved at 6.
post /api/emergency-occupations \
  "{\"application_id\":$EM_ID,\"expected_revision\":4,\"reason\":\"burst comms\"}" | J

echo "== 7. revision 5: B is suspended and force_suspended_by the EM app =="
embedded 5 "$B_ID"

echo "== 8. release EM at rev 6: EM revoked @7, B forced-recovered @8 =="
post "/api/emergency-occupations/$EM_ID/release" \
  '{"expected_revision":6}' | J

echo "== 9. rev 5 view unchanged after release; rev 8 B is effective again =="
embedded 5 "$B_ID"
embedded 8 "$B_ID"

echo "== 10. compare 3 -> 8 filtered to B (read-only; status changes/events) =="
curl -s "$BASE/api/revision/compare?base_revision=3&target_revision=8&application_id=$B_ID" | J

echo "== 11. live state still reflects the present (not the audit past) =="
curl -s "$BASE/api/revision/current" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("current_revision:", d["current_revision"])
print("effective:", d["effective_application_ids"],
      "suspended:", d["suspended_application_ids"])
'
curl -s "$BASE/api/applications/$A_ID" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(
    "live A: status=" + str(d["status"]),
    "suspended_revision=" + str(d["suspended_revision"]),
)
'

echo "== 12. preserved error semantics =="
curl -s -o /dev/null -w "revision/999 -> HTTP %{http_code} (404 out of range)\n" \
  "$BASE/api/revision/999"
curl -s -o /dev/null -w "revision/-1  -> HTTP %{http_code} (422 negative)\n" \
  "$BASE/api/revision/-1"
curl -s -o /dev/null -w "compare future -> HTTP %{http_code}\n" \
  "$BASE/api/revision/compare?base_revision=0&target_revision=999"
curl -s -o /dev/null -w "compare unknown application -> HTTP %{http_code}\n" \
  "$BASE/api/revision/compare?base_revision=0&target_revision=1&application_id=999999"
