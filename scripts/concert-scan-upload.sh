#!/usr/bin/env bash
# concert-scan-upload.sh
#
# Scan the container images that are RUNNING in a Kubernetes namespace with
# trivy and upload the results to IBM Concert so that the CVEs land on the
# build artifacts that Concert's Instana discovery created for that
# application.  Those discovered artifacts are named by the fully qualified
# image path (e.g. docker.io/mbx1010/vulnerable-echo-service, tag 2.0) and are
# the only ones linked to an environment - which is what the Instana CVE
# sensor needs (it drops every CVE that has no associated image).
#
# Flow per image (the pattern proven against concert3):
#   1. trivy image --scanners vuln --format cyclonedx <fully-qualified ref>
#      -> ONE CycloneDX file with packages + vulnerabilities
#      (specVersion 1.7 is downgraded to 1.6, Concert accepts 1.4-1.6)
#   2. upload as data_type=package_sbom
#        metadata={"application_name":APP,"application_version":VER}
#   3. upload the same file as data_type=image_scan
#        metadata={"scanner_name":"Trivy","application_name":APP,"application_version":VER}
#      (application context is what makes Concert pick the artifact *instance*
#       that belongs to the discovered application instead of the bare
#       basename artifact; set IMAGE_SCAN_APP_CONTEXT=0 to send scanner_name only)
#   4. poll Concert's ingestion event log for the returned event_id
#   5. verify CVE counts on the discovered artifact / environment
#
# The application name/version must be the ones Concert's Instana discovery
# created (they follow the Instana application perspective, e.g.
# "load-testing" 1.0.0) - NOT the Kubernetes deployment names.
#
# VERIFIED BEHAVIOUR on Concert 3.0.1.1 (2026-09-08, see core logs):
#   * package_sbom + application context -> Concert resolves the file to the
#     discovered artifact (ImageId = the docker.io/<repo>/<image> row of the app).
#   * image_scan -> Concert ALWAYS creates/updates an image object named by the
#     image BASENAME (e.g. "vulnerable-echo-service", tag 2.0) and attaches the
#     CVE findings there. Rewriting metadata.component.purl / bom-ref /
#     repository_url does not change this. That basename object is linked to
#     the application but NOT to the environment, so CVEs show
#     impacted_environments=0 and the Instana CVE sensor still skips them.
#   * The only supported way to get findings onto the discovered (environment-
#     linked) images is Concert's own image scanning in the auto-discovery
#     settings ("Advanced settings and filters" -> image scanning).
# This script therefore feeds packages + CVEs to the Concert application for
# the Concert UI, and reports exactly where Concert attached them.
#   * Side effect seen in the Concert event log: the image_scan pipeline
#     re-ingests the same file internally as package_sbom, which then fails
#     with "The CycloneDX SBOM already exists" because our explicit
#     package_sbom upload (same serialNumber) went first. Harmless.
#
# Usage:
#   CONCERT_API_KEY=<base64 user:apikey> scripts/concert-scan-upload.sh [options]
#
# Options (env vars or flags):
#   -n, --namespace NS        Kubernetes namespace to read running images from   (NAMESPACE, default load-testing)
#   -c, --context CTX         kubectl context                                    (KUBE_CONTEXT, default: current)
#   -a, --app NAME            Concert application name                           (APP_NAME, default = namespace)
#   -v, --app-version VER     Concert application version                        (APP_VERSION, default 1.0.0)
#   -i, --images "ref ref"    Explicit image refs instead of discovering them     (IMAGES)
#   -o, --outdir DIR          Where scan files go                                (OUTDIR, default ./concert-scans)
#   -u, --url URL             Concert URL                                        (CONCERT_URL, default https://concert3.lab.allwaysbeginner.com:12443)
#       --instance ID         Concert instance id                                (CONCERT_INSTANCE_ID, default 0000-0000-0000-0000)
#       --skip-scan           Re-use existing files in OUTDIR, only upload
#       --scan-only           Scan, write files, do not upload
#       --no-app-context      image_scan metadata = scanner_name only
#       --no-deploy-sbom      skip the deploy SBOM that writes the image digests onto the
#                             application's deployed image instances (see step 6)
#       --dry-run             Show what would be scanned/uploaded, do nothing
#   -h, --help
#
# Credentials: CONCERT_API_KEY, or ~/.config/sbom/concert.env providing
# CONCERT_B64 (key), CONCERT_HOST (host:port) and CONCERT_INSTANCE.
set -uo pipefail

# ---------- defaults -------------------------------------------------------
# Credentials file for command-line use (e.g. on the cluster master node):
#   ~/.concert.env with CONCERT_URL=..., CONCERT_INSTANCE_ID=..., CONCERT_API_KEY=...
if [[ -z "${CONCERT_API_KEY:-}" && -f "${CONCERT_ENV_FILE:-$HOME/.concert.env}" ]]; then
  # shellcheck disable=SC1090
  set -a; . "${CONCERT_ENV_FILE:-$HOME/.concert.env}"; set +a
fi
if [[ -z "${CONCERT_API_KEY:-}" && -f "$HOME/.config/sbom/concert.env" ]]; then
  # shellcheck disable=SC1090
  set -a; . "$HOME/.config/sbom/concert.env"; set +a
  CONCERT_API_KEY="${CONCERT_API_KEY:-${CONCERT_B64:-}}"
  [[ -n "${CONCERT_HOST:-}" && -z "${CONCERT_URL:-}" ]] && CONCERT_URL="https://${CONCERT_HOST//\"/}"
  CONCERT_INSTANCE_ID="${CONCERT_INSTANCE_ID:-${CONCERT_INSTANCE:-}}"
fi
CONCERT_URL="${CONCERT_URL:-https://concert3.lab.allwaysbeginner.com:12443}"
CONCERT_INSTANCE_ID="${CONCERT_INSTANCE_ID:-0000-0000-0000-0000}"
NAMESPACE="${NAMESPACE:-load-testing}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
APP_NAME="${APP_NAME:-}"
APP_VERSION="${APP_VERSION:-1.0.0}"
IMAGES="${IMAGES:-}"
OUTDIR="${OUTDIR:-./concert-scans}"
SKIP_SCAN=0; SCAN_ONLY=0; DRY_RUN=0
IMAGE_SCAN_APP_CONTEXT="${IMAGE_SCAN_APP_CONTEXT:-1}"
SCANNER_NAME="${SCANNER_NAME:-trivy}"
DEPLOY_SBOM="${DEPLOY_SBOM:-1}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"

usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace)   NAMESPACE="$2"; shift 2;;
    -c|--context)     KUBE_CONTEXT="$2"; shift 2;;
    -a|--app)         APP_NAME="$2"; shift 2;;
    -v|--app-version) APP_VERSION="$2"; shift 2;;
    -i|--images)      IMAGES="$2"; shift 2;;
    -o|--outdir)      OUTDIR="$2"; shift 2;;
    -u|--url)         CONCERT_URL="$2"; shift 2;;
    --instance)       CONCERT_INSTANCE_ID="$2"; shift 2;;
    --skip-scan)      SKIP_SCAN=1; shift;;
    --scan-only)      SCAN_ONLY=1; shift;;
    --no-app-context) IMAGE_SCAN_APP_CONTEXT=0; shift;;
    --no-deploy-sbom) DEPLOY_SBOM=0; shift;;
    --dry-run)        DRY_RUN=1; shift;;
    -h|--help)        usage 0;;
    *) echo "unknown option: $1" >&2; usage 1;;
  esac
done
APP_NAME="${APP_NAME:-$NAMESPACE}"
CONCERT_URL="${CONCERT_URL%/}"
export CONCERT_URL CONCERT_INSTANCE_ID CONCERT_API_KEY APP_NAME APP_VERSION NAMESPACE
[[ $SCAN_ONLY -eq 1 || $DRY_RUN -eq 1 || -n "${CONCERT_API_KEY:-}" ]] || { echo "CONCERT_API_KEY is not set (base64 of user:apikey)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
mkdir -p "$OUTDIR"

KC=(kubectl); [[ -n "$KUBE_CONTEXT" ]] && KC+=(--context "$KUBE_CONTEXT")
capi() { curl -sk --max-time 60 -H "InstanceID: ${CONCERT_INSTANCE_ID}" -H "Authorization: C_API_KEY ${CONCERT_API_KEY:-}" -H "accept: application/json" "$@"; }

# ---------- trivy runner ---------------------------------------------------
# trivy_scan <image ref> <output file> <format: cyclonedx|json>
#   cyclonedx -> package inventory (package_sbom upload)
#   json      -> trivy native report; the ONLY format from which Concert records the
#                image digest on the CVE findings (scan_object_attr2), which is what
#                links the findings to the deployed image instances.
if command -v trivy >/dev/null 2>&1; then
  trivy_scan() { trivy image --scanners vuln --format "$3" --output "$2" "$1"; }
  TRIVY_DESC="local trivy $(trivy --version 2>/dev/null | head -1)"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  TRIVY_CACHE="${TRIVY_CACHE:-$HOME/.cache/trivy}"; mkdir -p "$TRIVY_CACHE"
  trivy_scan() { # output file must be inside OUTDIR
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$TRIVY_CACHE:/root/.cache" -v "$(cd "$OUTDIR" && pwd):/output" \
      aquasec/trivy:latest image --scanners vuln --format "$3" \
      --output "/output/$(basename "$2")" "$1"; }
  TRIVY_DESC="aquasec/trivy:latest via docker"
else
  # No trivy and no docker (e.g. a kubeadm master node with containerd): fetch the
  # static trivy binary once into TRIVY_INSTALL_DIR (default ~/.local/bin).
  TRIVY_VERSION="${TRIVY_VERSION:-0.72.0}"
  TRIVY_INSTALL_DIR="${TRIVY_INSTALL_DIR:-$HOME/.local/bin}"
  if [[ ! -x "$TRIVY_INSTALL_DIR/trivy" ]]; then
    case "$(uname -s)-$(uname -m)" in
      Linux-x86_64)  TARBALL="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz";;
      Linux-aarch64) TARBALL="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz";;
      Darwin-arm64)  TARBALL="trivy_${TRIVY_VERSION}_macOS-ARM64.tar.gz";;
      Darwin-x86_64) TARBALL="trivy_${TRIVY_VERSION}_macOS-64bit.tar.gz";;
      *) echo "unsupported platform $(uname -s)-$(uname -m); install trivy manually" >&2; exit 1;;
    esac
    echo "trivy not found - downloading v${TRIVY_VERSION} to $TRIVY_INSTALL_DIR ..."
    mkdir -p "$TRIVY_INSTALL_DIR"
    if ! curl -sfL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${TARBALL}" \
         | tar -xz -C "$TRIVY_INSTALL_DIR" trivy; then
      echo "download of trivy failed (no internet access?). Install trivy and re-run." >&2; exit 1
    fi
    chmod +x "$TRIVY_INSTALL_DIR/trivy"
  fi
  trivy_scan() { "$TRIVY_INSTALL_DIR/trivy" image --scanners vuln --format "$3" --output "$2" "$1"; }
  TRIVY_DESC="downloaded trivy $TRIVY_VERSION ($TRIVY_INSTALL_DIR/trivy)"
fi

# ---------- 1. images ------------------------------------------------------
# Fully qualify a ref the way Concert's discovery names artifacts:
#   nginx:1.27-alpine            -> docker.io/library/nginx:1.27-alpine
#   mbx1010/chat-app:2.0         -> docker.io/mbx1010/chat-app:2.0
#   docker.io/x/y:t              -> unchanged
qualify() {
  local ref="$1" first
  first="${ref%%/*}"
  if [[ "$ref" != */* ]]; then echo "docker.io/library/$ref"
  elif [[ "$first" == *.* || "$first" == *:* || "$first" == localhost ]]; then echo "$ref"
  else echo "docker.io/$ref"; fi
}

declare -a IMG_REFS=() IMG_DIGESTS=()
if [[ -n "$IMAGES" ]]; then
  for r in $IMAGES; do IMG_REFS+=("$(qualify "$r")"); IMG_DIGESTS+=(""); done
  echo "Images (explicit): ${IMG_REFS[*]}"
else
  echo "Discovering running images in namespace '$NAMESPACE' ${KUBE_CONTEXT:+(context $KUBE_CONTEXT)} ..."
  # In a pod (no kubectl) read the pods straight from the API server with the service account.
  pods_json() {
    if command -v kubectl >/dev/null 2>&1; then "${KC[@]}" get pods -n "$NAMESPACE" -o json 2>/dev/null
    elif [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]]; then
      local sa=/var/run/secrets/kubernetes.io/serviceaccount
      curl -s --cacert "$sa/ca.crt" -H "Authorization: Bearer $(cat "$sa/token")" \
        "https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT:-443}/api/v1/namespaces/${NAMESPACE}/pods"
    else echo '{"items":[]}'; fi
  }
  while IFS='|' read -r ref digest; do
    [[ -n "$ref" ]] || continue
    IMG_REFS+=("$(qualify "$ref")"); IMG_DIGESTS+=("$digest")
  done < <(pods_json | python3 -c '
import json,sys
seen={}
for p in json.load(sys.stdin).get("items",[]):
    for cs in (p.get("status",{}).get("containerStatuses") or []):
        img=cs.get("image",""); iid=cs.get("imageID","")
        dg=iid.split("@")[-1] if "@" in iid else ""
        if img and img not in seen: seen[img]=dg
for k,v in seen.items(): print(f"{k}|{v}")')
  [[ ${#IMG_REFS[@]} -gt 0 ]] || { echo "no running images found in $NAMESPACE" >&2; exit 1; }
fi

echo
echo "Concert  : $CONCERT_URL  (instance $CONCERT_INSTANCE_ID)"
echo "App      : $APP_NAME  version $APP_VERSION"
echo "Scanner  : $TRIVY_DESC"
echo "Outdir   : $OUTDIR"
echo

# ---------- 0. show what Concert already knows for this app ----------------
APP_ID=""; ENV_ID=""
if [[ $SCAN_ONLY -eq 0 && $DRY_RUN -eq 0 ]]; then
  # Concert's /applications list endpoint is unreliable (HTTP 400 on this build), so
  # resolve the application through the environments it is deployed in.
  capi "$CONCERT_URL/core/api/v1/environments?page_size=100" > "$OUTDIR/.envs.json"
  python3 - "$OUTDIR/.envs.json" > "$OUTDIR/.app.env" <<'EOF'
import json,sys,subprocess,os
envs=json.load(open(sys.argv[1])).get("environments",[])
app=os.environ["APP_NAME"]; ver=os.environ["APP_VERSION"]
url=os.environ["CONCERT_URL"]
hdr=["-H","InstanceID: "+os.environ["CONCERT_INSTANCE_ID"],"-H","Authorization: C_API_KEY "+os.environ["CONCERT_API_KEY"]]
found=False
for e in envs:
    r=subprocess.run(["curl","-sk","--max-time","30",*hdr,url+"/core/api/v1/environments/"+e["id"]+"/applications?page_size=100"],capture_output=True,text=True)
    try: apps=json.loads(r.stdout).get("applications",[])
    except Exception: apps=[]
    for a in apps:
        if a.get("name")==app and str(a.get("version"))==ver:
            print("APP_ID='%s'; ENV_ID='%s'; ENV_NAME='%s'"%(a["id"],e["id"],e["name"].replace("'","")))
            found=True; break
    if found: break
if not found: print("APP_ID=''; ENV_ID=''; ENV_NAME=''")
EOF
  # shellcheck disable=SC1091
  . "$OUTDIR/.app.env"; export APP_ID ENV_ID ENV_NAME
  if [[ -z "$APP_ID" ]]; then
    echo "WARNING: application '$APP_NAME' $APP_VERSION not found in any Concert environment."
    echo "         Uploads will still be tagged with it; Concert will reject the package_sbom if the app does not exist."
  else
    echo "Concert application '$APP_NAME' $APP_VERSION = $APP_ID  (environment: $ENV_NAME)"
    echo "Discovered build artifacts of that application:"
    capi "$CONCERT_URL/core/api/v1/applications/$APP_ID/build_artifacts?page_size=100" > "$OUTDIR/.app-artifacts.json"
    python3 - "$OUTDIR/.app-artifacts.json" <<'EOF'
import json,sys
for a in json.load(open(sys.argv[1])).get("build_artifacts",[]):
    print(f"   {a['name']}:{a.get('image_tag')}   id={a['id']}")
EOF
  fi
  echo
fi

# map ref -> discovered artifact id (name without tag == artifact name, tag == image_tag)
artifact_id_for() { # $1 fully-qualified ref with tag
  [[ -f "$OUTDIR/.app-artifacts.json" ]] || { echo ""; return; }
  python3 - "$OUTDIR/.app-artifacts.json" "$1" <<'EOF'
import json,sys
ref=sys.argv[2]; name,_,tag=ref.rpartition(":") if ":" in ref.rsplit("/",1)[-1] else (ref,"","latest")
for a in json.load(open(sys.argv[1])).get("build_artifacts",[]):
    if a["name"]==name and str(a.get("image_tag") or "latest")==tag: print(a["id"]); break
EOF
}
cve_count() { # $1 = deployed image INSTANCE id (what the app's build_artifacts list returns)
  # findings live on build-artifact instances; resolve instance -> parent artifact via the v2 API once
  if [[ ! -s "$OUTDIR/.instances.tsv" ]]; then
    capi "$CONCERT_URL/core/api/v2/build_artifacts?page_size=100" | python3 -c '
import json,sys,subprocess,os
url=os.environ["CONCERT_URL"]; hdr=["-H","InstanceID: "+os.environ["CONCERT_INSTANCE_ID"],"-H","Authorization: C_API_KEY "+os.environ["CONCERT_API_KEY"]]
for a in json.load(sys.stdin).get("build_artifacts",[]):
    r=subprocess.run(["curl","-sk","--max-time","30",*hdr,url+"/core/api/v2/build_artifacts/"+a["id"]+"/instances?page_size=100"],capture_output=True,text=True)
    try:
        for i in json.loads(r.stdout).get("instances",[]): print(i["id"]+"\t"+a["id"]+"\t"+(i.get("image_digest") or ""))
    except Exception: pass' > "$OUTDIR/.instances.tsv" 2>/dev/null
  fi
  local art; art=$(awk -F'\t' -v id="$1" '$1==id{print $2}' "$OUTDIR/.instances.tsv" | head -1)
  [[ -n "$art" ]] || { echo "?"; return; }
  capi "$CONCERT_URL/core/api/v2/build_artifacts/$art/instances/$1/cve_findings?page_size=1" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("pagination") or {}).get("total_count",0))' 2>/dev/null || echo "?"
}
instance_digest() { awk -F'\t' -v id="$1" '$1==id{print $3}' "$OUTDIR/.instances.tsv" 2>/dev/null | head -1; }
env_cve_count() {
  [[ -n "$ENV_ID" ]] || { echo "?"; return; }
  capi "$CONCERT_URL/core/api/v1/environments/$ENV_ID/cves?page_size=1" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("pagination") or {}).get("total_count",0))' 2>/dev/null || echo "?"
}

# ---------- event log polling ---------------------------------------------
poll_event() { # $1 event_id, $2 data_type  -> prints status + comment
  local start end body deadline=$(( $(date +%s) + POLL_TIMEOUT ))
  # python for the timestamps: GNU date, BSD date and busybox date all differ
  start=$(python3 -c 'import datetime as d; print((d.datetime.utcnow()-d.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  while :; do
    end=$(python3 -c 'import datetime as d; print((d.datetime.utcnow()+d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
    body="{\"filters\":{\"logic\":\"and\",\"conditions\":[{\"field\":\"date_range\",\"value\":\"start=$start,end=$end\",\"operator\":\"RANGE\"},{\"field\":\"data_type\",\"value\":\"$2\",\"operator\":\"IN\"}]},\"sort\":{\"field\":\"timestamp\",\"order\":\"desc\"},\"pagination\":{\"page\":1,\"page_size\":50}}"
    local out
    out=$(capi -X POST "$CONCERT_URL/concert/ingestion/api/v1/table_of_contents/lz/advanced_search" -H "Content-Type: application/json" -d "$body" \
      | python3 -c '
import json,sys
eid=sys.argv[1]
for r in json.load(sys.stdin).get("toc_records",[]):
    if r.get("event_id")==eid:
        st=r.get("status") or ""; c=(r.get("additional_comments") or r.get("additional_details") or "").split("\n")[0]
        print(st+"|"+c); break' "$1" 2>/dev/null)
    case "${out%%|*}" in
      processing_completed|error_processing|completed|failed) echo "$out"; return;;
    esac
    [[ $(date +%s) -lt $deadline ]] || { echo "timeout|no final status after ${POLL_TIMEOUT}s (event $1)"; return; }
    sleep 10
  done
}

upload() { # $1 data_type $2 file $3 metadata-json -> prints event_id
  local resp code
  resp=$(capi -w '\n%{http_code}' -X POST "$CONCERT_URL/ingestion/api/v1/upload_files" \
           -F "data_type=$1" -F "metadata=$3" -F "filename=@$2")
  code="${resp##*$'\n'}"; resp="${resp%$'\n'*}"
  echo "$resp" > "$OUTDIR/resp-$1-$(basename "$2")"
  local eid; eid=$(printf '%s' "$resp" | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("event_ids") or [""])[0])
except Exception: print("")')
  echo "$code|$eid"
}

# ---------- 2..5 per image -------------------------------------------------
ENV_BEFORE=$([[ $SCAN_ONLY -eq 0 && $DRY_RUN -eq 0 ]] && env_cve_count || echo "-")
declare -a SUMMARY=() DEP_REFS=() DEP_DIGESTS=()
for idx in "${!IMG_REFS[@]}"; do
  ref="${IMG_REFS[$idx]}"; rdigest="${IMG_DIGESTS[$idx]}"
  safe=$(echo "$ref" | tr '/:@' '___')
  raw="$OUTDIR/${safe}.raw.json"      # trivy CycloneDX (root-owned when run via docker)
  native="$OUTDIR/${safe}.trivy.json" # trivy native JSON report (carries RepoDigests)
  file="$OUTDIR/${safe}.cdx.json"     # normalised, user-owned CycloneDX that gets uploaded
  echo "=============================================================="
  echo "IMAGE  $ref ${rdigest:+(running digest ${rdigest:7:12}...)}"
  aid=""; before="-"
  if [[ $SCAN_ONLY -eq 0 && $DRY_RUN -eq 0 ]]; then
    aid=$(artifact_id_for "$ref")
    if [[ -n "$aid" ]]; then before=$(cve_count "$aid"); echo "  discovered artifact: $aid  (CVEs before: $before)"
    else echo "  WARNING: no discovered artifact with this name:tag under app '$APP_NAME' - Concert will create/attach by its own rules"; fi
  fi
  if [[ $DRY_RUN -eq 1 ]]; then echo "  would scan -> $file and upload as package_sbom + image_scan for $APP_NAME/$APP_VERSION"; continue; fi

  # --- scan
  if [[ $SKIP_SCAN -eq 1 && -s "$raw" && -s "$native" ]]; then
    echo "  scan: reusing $raw + $native"
  else
    echo "  scan: trivy (cyclonedx + native json) -> $OUTDIR/${safe}.*"
    rm -f "$raw" "$native"
    if ! trivy_scan "$ref" "$raw" cyclonedx >"$OUTDIR/${safe}.trivy.log" 2>&1 || [[ ! -s "$raw" ]] \
       || ! trivy_scan "$ref" "$native" json >>"$OUTDIR/${safe}.trivy.log" 2>&1 || [[ ! -s "$native" ]]; then
      echo "  ERROR: trivy failed, see $OUTDIR/${safe}.trivy.log"; SUMMARY+=("$ref | scan FAILED"); continue
    fi
  fi
  # --- normalise raw -> file: CycloneDX <=1.6, report identity + digest match
  rm -f "$file"   # may be a stale root-owned file from an older run
  info=$(python3 - "$raw" "$file" "$rdigest" "$native" <<'EOF'
import json,sys
raw,f,running,native=sys.argv[1:5]
d=json.load(open(raw))
if d.get("specVersion") not in ("1.4","1.5","1.6"):
    d["specVersion"]="1.6"; d["$schema"]="http://cyclonedx.org/schema/bom-1.6.schema.json"
json.dump(d,open(f,"w"),indent=1)
md=d.get("metadata",{}).get("component",{})
purl=md.get("purl","")
scanned=purl.split("@")[1].split("?")[0] if "@" in purl else ""
try:
    n=json.load(open(native)); rd=(n.get("Metadata",{}).get("RepoDigests") or [""])[0]
    if "@" in rd: scanned=rd.split("@")[1]
    nvuln=sum(len(r.get("Vulnerabilities") or []) for r in n.get("Results",[]))
except Exception: nvuln=len(d.get("vulnerabilities",[]))
match="n/a" if not running else ("MATCH" if scanned==running else "MISMATCH")
print(f"{md.get('name')}|{scanned}|{match}|{len(d.get('components',[]))}|{nvuln}|{d.get('specVersion')}")
EOF
)
  if [[ -z "$info" ]]; then echo "  ERROR: could not read/normalise $raw"; SUMMARY+=("$ref | normalise FAILED"); continue; fi
  IFS='|' read -r cname sdig match ncomp nvuln spec <<<"$info"
  echo "  file : component.name=$cname  digest=${sdig:7:12}...  packages=$ncomp  vulns=$nvuln  cyclonedx=$spec  running-digest: $match"
  if [[ "$match" == "MISMATCH" ]]; then
    # The tag moved on in the registry (typical for :latest). Concert links findings to the
    # deployed instance by digest, so rescan the RUNNING digest for the image_scan upload.
    repo="${ref%:*}"; byd="$OUTDIR/${safe}.bydigest.trivy.json"
    echo "  WARNING: tag digest ${sdig:7:12} != running ${rdigest:7:12} -> rescanning ${repo}@${rdigest:0:19}... for the image scan"
    rm -f "$byd"
    if trivy_scan "${repo}@${rdigest}" "$byd" json >>"$OUTDIR/${safe}.trivy.log" 2>&1 && [[ -s "$byd" ]]; then
      native="$byd"; sdig="$rdigest"
    else
      echo "  WARNING: rescan by digest failed (see $OUTDIR/${safe}.trivy.log); uploading the tag scan instead"
    fi
  fi
  # digest used for the deploy SBOM: the running one when known, else what trivy scanned
  DEP_REFS+=("$ref"); DEP_DIGESTS+=("${rdigest:-$sdig}")
  [[ $SCAN_ONLY -eq 1 ]] && { SUMMARY+=("$ref | scanned only ($nvuln vulns)"); continue; }

  # --- upload package_sbom (with application context)
  meta_pkg="{\"application_name\":\"$APP_NAME\",\"application_version\":\"$APP_VERSION\"}"
  IFS='|' read -r code eid <<<"$(upload package_sbom "$file" "$meta_pkg")"
  echo "  package_sbom  HTTP $code  event $eid"
  st_pkg="-"
  if [[ -n "$eid" ]]; then st_pkg=$(poll_event "$eid" package_sbom); echo "     -> ${st_pkg%%|*}: ${st_pkg#*|}"; fi

  # --- upload image_scan: trivy NATIVE json (only this format carries the digest into Concert)
  if [[ $IMAGE_SCAN_APP_CONTEXT -eq 1 ]]; then
    meta_scan="{\"scanner_name\":\"$SCANNER_NAME\",\"application_name\":\"$APP_NAME\",\"application_version\":\"$APP_VERSION\"}"
  else
    meta_scan="{\"scanner_name\":\"$SCANNER_NAME\"}"
  fi
  IFS='|' read -r code eid <<<"$(upload image_scan "$native" "$meta_scan")"
  echo "  image_scan    HTTP $code  event $eid  metadata=$meta_scan  (trivy native json)"
  st_scan="-"
  if [[ -n "$eid" ]]; then st_scan=$(poll_event "$eid" image_scan); echo "     -> ${st_scan%%|*}: ${st_scan#*|}"; fi

  after="-"; [[ -n "$aid" ]] && after=$(cve_count "$aid")
  echo "  discovered artifact CVEs: before=$before after=$after"
  SUMMARY+=("$ref | vulns=$nvuln | pkg:${st_pkg%%|*} | scan:${st_scan%%|*} | artifact CVEs $before -> $after")
done

# ---------- 6. deploy SBOM: write the image digests onto the app's deployed instances -----
# Concert's Instana discovery creates the environment-linked image instances WITHOUT a
# digest. Concert links CVE findings to deployed instances by name + tag + digest, so we
# upload a ConcertDef 2.0.0 deploy SBOM for the same application/environment whose image
# uris are <name>:<tag>@<digest>; Concert then fills image_digest on those instances.
if [[ $DEPLOY_SBOM -eq 1 && $SCAN_ONLY -eq 0 && $DRY_RUN -eq 0 && -n "$APP_ID" && ${#DEP_REFS[@]} -gt 0 ]]; then
  echo "=============================================================="
  echo "DEPLOY SBOM  app=$APP_NAME/$APP_VERSION  env=$ENV_NAME  images=${#DEP_REFS[@]}"
  dsbom="$OUTDIR/deploy-sbom-${APP_NAME// /_}.json"
  printf '%s\n' "${DEP_REFS[@]}" > "$OUTDIR/.dep_refs"; printf '%s\n' "${DEP_DIGESTS[@]}" > "$OUTDIR/.dep_digests"
  python3 - "$dsbom" "$OUTDIR/.dep_refs" "$OUTDIR/.dep_digests" <<'EOF'
import json,sys,os,datetime
out,refs_f,dig_f=sys.argv[1:4]
refs=[l.strip() for l in open(refs_f) if l.strip()]; digs=[l.strip() for l in open(dig_f)]
app=os.environ["APP_NAME"]; ver=os.environ["APP_VERSION"]; env=os.environ["ENV_NAME"]; ns=os.environ["NAMESPACE"]
def parts(ref):
    repo,_,tag=ref.rpartition(":") if ":" in ref.rsplit("/",1)[-1] else (ref,"","latest")
    return repo,tag,repo.rsplit("/",1)[-1]
deps=[]
for ref,dig in zip(refs,digs):
    repo,tag,base=parts(ref)
    uri=f"{repo}:{tag}@{dig}" if dig.startswith("sha256:") else f"{repo}:{tag}"
    img={"bom-ref":"image:"+base,"type":"image","name":base,"uri":uri}
    deps.append({"bom-ref":"deployment:"+base,"type":"deployment","name":base,"components":[img],
        "properties":[{"name":"workload.type","value":"k8s:Deployment"},
                      {"name":"metadata","value":json.dumps({"containers":[{"container":base,"image":uri}]})}]})
doc={"bomFormat":"ConcertDef","specVersion":"2.0.0",
 "metadata":{"timestamp":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
             "type":"deploy","deploy-name":f"deploy_{env}_{app}_scan_{ver}","application-name":app,
             "application-version":ver,"change-request-url":"default"},
 "environment":{"bom-ref":"environment:"+env,"name":env,"purpose":"","properties":[]},
 "runtime-components":[{"bom-ref":"infra:kubernetes:scan:kubernetes","name":"kubernetes","type":"kubernetes","api-server":"kubernetes",
    "properties":[{"name":"platform","value":"kubernetes"},{"name":"cluster_platform","value":""},{"name":"cluster_id","value":""},
                  {"name":"cluster_region","value":""},{"name":"cluster_name","value":"kubernetes"}],
    "components":[{"bom-ref":"namespace:"+ns,"type":"namespace","name":ns,"components":deps}]}],
 "services":[],
 "dependencies":[{"ref":"environment:"+env,"dependsOn":["infra:kubernetes:scan:kubernetes"]}]}
json.dump(doc,open(out,"w"),indent=1)
for d in deps: print("   ",d["components"][0]["uri"])
EOF
  IFS='|' read -r code eid <<<"$(upload application_sbom "$dsbom" "")"
  echo "  application_sbom (deploy)  HTTP $code  event $eid"
  st_dep="-"
  if [[ -n "$eid" ]]; then st_dep=$(poll_event "$eid" application_sbom); echo "     -> ${st_dep%%|*}: ${st_dep#*|}"; fi
  SUMMARY+=("deploy SBOM $APP_NAME -> ${st_dep%%|*}")
fi

if [[ $DRY_RUN -eq 0 ]]; then
  echo
  echo "==================== SUMMARY ===================="
  printf '%s\n' "${SUMMARY[@]}"
  if [[ $SCAN_ONLY -eq 0 ]]; then
    sleep 5
    echo "Environment '${ENV_NAME:-?}' CVEs: before=$ENV_BEFORE after=$(env_cve_count)"
    echo
    echo "Next: restart the Instana agent that runs the Concert CVE sensor to force a new poll"
    echo "      (sudo systemctl restart instana-agent), then check its log for 'Skipped due to' vs. sent events."
  fi
fi
