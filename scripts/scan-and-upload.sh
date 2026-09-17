#!/usr/bin/env bash
# Scans der techexchange-demo-app auf dem Build-Host, Upload nach IBM Concert.
#
# Bewusst NICHT enthalten:
#   * application_sbom — wird einmalig separat gepflegt, nicht bei jedem Scanlauf.
#     Ein ConcertDef-Upload ist immer ein VOLLERSATZ und wuerde die entdeckte
#     Topologie (Deployments, Environment-Bindung) bei jedem Lauf loeschen.
#   * image_scan — das besitzt die Kubernetes-Discovery. Ein manuell hochgeladener
#     image_scan erzeugt ein Image-Objekt ohne Environment-Bezug
#     (impacted_environments=0) und damit ein zweites, totes Artefakt im Blast Radius.
#
# Trivy und Semgrep laufen als Container, auf dem Host ist keines von beiden installiert.
#
# Korrekturen 17.09.2026 nach dem ersten Fehllauf:
#   1. --offline-scan: ohne ihn holt Trivy die Spring-Boot-Parent-POM 2.6.1 von
#      Maven Central. Die IP lief dort in ein 429 (Sperre ~27 min), Trivy brach
#      FATAL ab und schrieb gar keine Datei -> code_scan fehlte still.
#   2. --scanners vuln: "--format cyclonedx" schaltet die Schwachstellensuche ab.
#      Ohne das Flag ist der code_scan nur eine Stueckliste ohne CVEs, und
#      Secure Coder findet Log4Shell nicht.
#   3. downgrade_cdx schreibt eine NEUE Datei statt in-place. Die Trivy-Ausgaben
#      gehoeren root (Container laeuft als root), in-place gab Permission denied
#      und alle package_sbom gingen als specVersion 1.7 raus, die Concert ablehnt.
#   4. SARIF heisst jetzt .json — Concert lehnte .sarif mit
#      "Unsupported file type" ab. Die Upload-Matrix listet SARIF als json.
set -uo pipefail

CONF="${CONCERT_ENV_FILE:-$HOME/.config/sbom/concert.env}"
[ -f "$CONF" ] || { echo "FEHLER: $CONF fehlt"; exit 1; }
set -a; . "$CONF"; set +a
: "${CONCERT_HOST:?}" "${CONCERT_INSTANCE:?}" "${CONCERT_B64:?}"

REPO_DIR="${REPO_DIR:-$HOME/techexchange-demo-app}"
REPO_NAME="${REPO_NAME:-test-mabu/techexchange-demo-app}"
REPO_URL="${REPO_URL:-https://github.com/test-mabu/techexchange-demo-app}"
APP_NAME="${APP_NAME:-kubernetes_demo-environment_load-testing_load-testing}"
APP_VERSION="${APP_VERSION:-1.0.0}"
TAG="${TAG:-2.0}"
OUT="${OUT:-$HOME/tx-scan/out}"
CACHE="$HOME/tx-scan/cache"
M2="$HOME/.m2"
UPLOAD="https://${CONCERT_HOST}/ingestion/api/v1/upload_files"

# Registry-lose Namen, exakt wie im Pod-Spec. Concert korreliert ueber den Namen.
IMAGES="
mbx1010/load-test-app:${TAG}
mbx1010/vulnerable-echo-service:${TAG}
mbx1010/chat-app:${TAG}
bkimminich/juice-shop:latest
nginx:1.27-alpine
"

mkdir -p "$OUT" "$CACHE" "$M2"
TRIVY="docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v $CACHE:/root/.cache -v $M2:/root/.m2 -v $OUT:/output -v $REPO_DIR:/src aquasec/trivy:latest"
SEMGREP="docker run --rm -v $REPO_DIR:/src -v $OUT:/output --workdir /src semgrep/semgrep:latest"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# Concert nimmt CycloneDX 1.4 bis 1.6; Trivy schreibt inzwischen 1.7.
# Gibt den Pfad der Datei aus, die hochgeladen werden soll.
downgrade_cdx() {
  python3 - "$1" <<'PY'
import json,sys
p=sys.argv[1]
try:
    d=json.load(open(p))
except Exception:
    print(p); sys.exit(0)
if d.get("specVersion","") > "1.6":
    d["specVersion"]="1.6"
    out=(p[:-5] if p.endswith(".json") else p)+".cdx16.json"
    try:
        json.dump(d, open(out,"w"))
        print(out)
    except Exception:
        print(p)
else:
    print(p)
PY
}

upload() { # upload <datei> <data_type> <metadata-json>
  local f="$1" dt="$2" meta="$3"
  [ -s "$f" ] || { log "    ABBRUCH, Datei leer oder fehlt: $(basename "$f")"; return 1; }
  local resp
  resp=$(curl -sk --max-time 180 -X POST "$UPLOAD" \
    -H "InstanceId: ${CONCERT_INSTANCE}" \
    -H "Authorization: C_API_KEY ${CONCERT_B64}" \
    -F "data_type=${dt}" \
    -F "filename=@${f}" \
    -F "metadata=${meta}")
  if grep -q '"event_ids"' <<<"$resp"; then
    log "    ok: $dt <- $(basename "$f")"
  else
    log "    FEHLER: $dt <- $(basename "$f") :: ${resp:0:180}"
  fi
}

log "Repo aktualisieren"
git -C "$REPO_DIR" pull --ff-only 2>&1 | tail -1

# ---------- 1. Code-Scan (CVEs in Abhaengigkeiten) ----------
log "Trivy fs ueber das Repo -> code_scan"
$TRIVY fs --scanners vuln --offline-scan --skip-version-check \
  --format cyclonedx --output /output/repo-code.cdx.json /src 2>&1 | grep -E "FATAL|ERROR" || true
F=$(downgrade_cdx "$OUT/repo-code.cdx.json")
upload "$F" code_scan \
  "{\"scanner_name\":\"Trivy\",\"repo_url\":\"${REPO_URL}\",\"application_name\":\"${APP_NAME}\",\"application_version\":\"${APP_VERSION}\"}"

# ---------- 2. SAST (Exposures ohne CVE) ----------
log "Semgrep -> static_code_scan"
$SEMGREP semgrep --config=auto --sarif --output /output/sast.json java-app python-app >/dev/null 2>&1
upload "$OUT/sast.json" static_code_scan \
  "{\"scanner_name\":\"semgrep\",\"repo_name\":\"${REPO_NAME}\",\"repo_url\":\"${REPO_URL}\",\"application_name\":\"${APP_NAME}\",\"application_version\":\"${APP_VERSION}\"}"

# ---------- 3. Package-SBOMs je Image ----------
log "Package-SBOMs je Image"
for image in $IMAGES; do
  safe=$(echo "$image" | tr '/:' '__')
  log "  $image"
  $TRIVY image --skip-version-check --format cyclonedx \
    --output "/output/${safe}.cdx.json" "$image" 2>&1 | grep -E "FATAL|ERROR" || true
  F=$(downgrade_cdx "$OUT/${safe}.cdx.json")
  upload "$F" package_sbom \
    "{\"image_name\":\"${image%%:*}\",\"tag\":\"${image##*:}\",\"application_name\":\"${APP_NAME}\",\"application_version\":\"${APP_VERSION}\"}"
done

log "Fertig. Ergebnis pruefen, nicht auf HTTP 202 verlassen:"
log "  Ingestion ist asynchron — Fehler stehen nur im Event-Log von Concert."
