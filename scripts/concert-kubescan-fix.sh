#!/usr/bin/env bash
# concert-kubescan-fix.sh - re-ingest Concert's own Kubernetes-discovery image scans that failed with
# "The vulnerabilities assessment can't be generated." (Concert 3.0.1.1, appdb encoding SQL_ASCII:
# ERROR conversion between UTF8 and SQL_ASCII is not supported).
#
# What it does
#   1. searches the ingestion event log for image_scan records named trivy-kubescan-*.csv with
#      status error_processing (newest record per file name only, unless --all)
#   2. downloads the CSV from the landing zone
#   3. replaces every non-ASCII character (U+2011 NON-BREAKING HYPHEN, curly quotes, dashes, ...) and
#      control characters (tabs) with ASCII equivalents
#   4. re-uploads the file as data_type image_scan with the same application context that Concert
#      used (application_name / application_version from the failed record; override with -a / -v)
#   5. waits for the new record to reach processing_completed and prints the finding counts
#   Records already fixed are remembered in ~/.concert-kubescan-fix.state (lz_path per line).
#
# Usage
#   concert-kubescan-fix.sh [-u url] [-k key] [--instance id] [-a app] [-v ver] [--since 2026-09-01]
#                           [--all] [--force] [--dry-run] [--no-wait]
# Credentials: -u/-k or CONCERT_URL / CONCERT_API_KEY (also read from ~/.concert.env or
#              ~/.config/sbom/concert.env), instance id via --instance or CONCERT_INSTANCE_ID (default 0000-0000-0000-0000).
set -euo pipefail

for f in "$HOME/.concert.env" "$HOME/.config/sbom/concert.env"; do
  # shellcheck disable=SC1090
  [ -r "$f" ] && set -a && . "$f" && set +a
done
URL="${CONCERT_URL:-}"; KEY="${CONCERT_API_KEY:-}"; INSTANCE="${CONCERT_INSTANCE_ID:-0000-0000-0000-0000}"
APP=""; VER=""; SINCE=""; ALL=0; FORCE=0; DRY=0; WAIT=1
STATE="${CONCERT_KUBESCAN_STATE:-$HOME/.concert-kubescan-fix.state}"
while [ $# -gt 0 ]; do
  case "$1" in
    -u) URL="$2"; shift 2;; -k) KEY="$2"; shift 2;; --instance) INSTANCE="$2"; shift 2;;
    -a) APP="$2"; shift 2;; -v) VER="$2"; shift 2;; --since) SINCE="$2"; shift 2;;
    --all) ALL=1; shift;; --force) FORCE=1; shift;; --dry-run) DRY=1; shift;; --no-wait) WAIT=0; shift;;
    -h|--help) sed -n '2,22p' "$0"; exit 0;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
[ -n "$URL" ] && [ -n "$KEY" ] || { echo "need -u <concert url> and -k <api key> (or CONCERT_URL / CONCERT_API_KEY)" >&2; exit 2; }
URL="${URL%/}"
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 2; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/kubescan-fix.XXXXXX")"; trap 'rm -rf "$WORK"' EXIT
touch "$STATE"

capi() { curl -sk --max-time 120 -H "Authorization: C_API_KEY $KEY" -H "InstanceID: $INSTANCE" "$@"; }
now_utc() { python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))'; }
[ -n "$SINCE" ] || SINCE="$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=30)).strftime("%Y-%m-%d"))')"
END="$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)).strftime("%Y-%m-%d"))')"

# ---- 1. find failed Concert-generated scans ------------------------------------------------------
capi -X POST "$URL/concert/ingestion/api/v1/table_of_contents/lz/advanced_search" -H "Content-Type: application/json" -d "{
 \"filters\":{\"logic\":\"and\",\"conditions\":[
   {\"field\":\"date_range\",\"value\":\"start=${SINCE}T00:00:00Z,end=${END}T00:00:00Z\",\"operator\":\"RANGE\"},
   {\"field\":\"data_type\",\"value\":\"image_scan\",\"operator\":\"IN\"}]},
 \"sort\":{\"field\":\"timestamp\",\"order\":\"desc\"},\"pagination\":{\"page\":1,\"page_size\":500}}" > "$WORK/toc.json"

python3 - "$WORK/toc.json" "$ALL" > "$WORK/failed.tsv" <<'PY'
import json,sys
recs=json.load(open(sys.argv[1])).get("toc_records",[]); allrec=sys.argv[2]=="1"
seen=set()
for r in recs:  # newest first
    fn=r.get("file_name","")
    if not fn.startswith("trivy-kubescan-") or r.get("status")!="error_processing": continue
    if not allrec:
        if fn in seen: continue
        seen.add(fn)
    md={m["name"]:m["value"] for m in (r.get("metadata") or [])}
    print("\t".join([r["timestamp"],fn,r["lz_path"],md.get("application_name",""),md.get("application_version",""),(r.get("additional_details") or "").split("\n")[0]]))
PY

if [ ! -s "$WORK/failed.tsv" ]; then echo "no failed trivy-kubescan-*.csv records since $SINCE"; exit 0; fi
echo "failed Concert image scans (newest per file unless --all):"
awk -F'\t' '{printf "  %s  %s  app=%s/%s  [%s]\n",$1,$2,$4,$5,$6}' "$WORK/failed.tsv"

rc=0
while IFS=$'\t' read -r ts fn lz app_md ver_md detail; do
  app="${APP:-$app_md}"; ver="${VER:-${ver_md:-1.0.0}}"
  if [ "$FORCE" = 0 ] && grep -qxF "$lz" "$STATE"; then echo "-- $ts $fn already fixed (state file), skipping"; continue; fi
  [ -n "$app" ] || { echo "-- $ts $fn: no application_name in record, pass -a <app>" >&2; rc=1; continue; }
  echo "== $ts $fn -> application $app / $ver"
  # ---- 2. download ---------------------------------------------------------------------------------
  capi "$URL/core/api/v1/landing_zone/data?filepath=$lz" -o "$WORK/raw"
  # ---- 3. sanitize ---------------------------------------------------------------------------------
  python3 - "$WORK/raw" "$WORK/$fn" <<'PY'
import sys,json,base64,csv,io,unicodedata
raw=open(sys.argv[1],"rb").read()
try:                                   # landing zone may wrap the file as a base64 JSON string
    j=json.loads(raw)
    if isinstance(j,str): raw=base64.b64decode(j)
except Exception: pass
text=raw.decode("utf-8","replace")
repl={"‑":"-","‐":"-","‒":"-","–":"-","—":"-","―":"-","−":"-"," ":" ",
      "‘":"'","’":"'","‚":"'","“":'"',"”":'"',"„":'"',"…":"...","·":"*","•":"*"}
def clean(s):
    for k,v in repl.items(): s=s.replace(k,v)
    s=unicodedata.normalize("NFKD",s).encode("ascii","ignore").decode()
    return "".join(c if (ord(c)>=32 or c in "\n") else " " for c in s)
rows=list(csv.reader(io.StringIO(text)))
changed=sum(1 for r in rows for c in r if clean(c)!=c)
with open(sys.argv[2],"w",newline="") as f:
    w=csv.writer(f,lineterminator="\n")
    for r in rows: w.writerow([clean(c) for c in r])
print(f"   rows={len(rows)-1} cells_changed={changed}")
PY
  if [ "$DRY" = 1 ]; then echo "   dry-run: would upload $WORK/$fn as image_scan for $app/$ver"; continue; fi
  # ---- 4. upload -----------------------------------------------------------------------------------
  t0="$(now_utc)"
  resp="$(capi -X POST "$URL/ingestion/api/v1/upload_files" -F data_type=image_scan \
        -F "metadata={\"scanner_name\":\"trivy\",\"application_name\":\"$app\",\"application_version\":\"$ver\"}" \
        -F "filename=@$WORK/$fn")"
  echo "   upload: $(echo "$resp" | head -c 200)"
  echo "$resp" | grep -q '202' || { rc=1; continue; }
  echo "$lz" >> "$STATE"
  [ "$WAIT" = 1 ] || continue
  # ---- 5. wait for the new record --------------------------------------------------------------
  for _ in $(seq 1 60); do
    sleep 10
    st="$(capi -X POST "$URL/concert/ingestion/api/v1/table_of_contents/lz/advanced_search" -H "Content-Type: application/json" -d "{
      \"filters\":{\"logic\":\"and\",\"conditions\":[
        {\"field\":\"date_range\",\"value\":\"start=${t0},end=${END}T00:00:00Z\",\"operator\":\"RANGE\"},
        {\"field\":\"data_type\",\"value\":\"image_scan\",\"operator\":\"IN\"}]},
      \"sort\":{\"field\":\"timestamp\",\"order\":\"desc\"},\"pagination\":{\"page\":1,\"page_size\":20}}" \
      | python3 -c '
import json,sys; fn=sys.argv[1]
for r in json.load(sys.stdin).get("toc_records",[]):
    if r.get("file_name")==fn and r.get("event_source")=="user":
        md={m["name"]:m["value"] for m in (r.get("metadata") or [])}
        print(r["status"],"|",(r.get("additional_details") or "").split("\n")[0][:120],"| findings",md.get("total_findings","?"),"cves",md.get("total_cves","?")); break' "$fn")"
    echo "   $(now_utc) $st"
    case "$st" in processing_completed*|error_processing*|*failed*) break;; esac
  done
  case "$st" in processing_completed*) ;; *) rc=1;; esac
done < "$WORK/failed.tsv"
exit $rc
