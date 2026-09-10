# IBM Concert + Instana vulnerability handbook (lab edition, "for dummies")

Everything in this document was verified hands-on in the lab in September 2026 against
IBM Concert **3.0.1.1** (self-hosted, `concert3.lab.allwaysbeginner.com:12443`) and
IBM Instana **3.323** (self-hosted, `instanak3s`). Where something is still being validated it is
(the Instana export in section 4 is a confirmed Concert defect with the exact broken SQL, see there). Replace `<CONCERT_API_KEY>` with your key everywhere.

Contents

1. [The moving parts](#1-the-moving-parts)
2. [One-time setup](#2-one-time-setup)
3. [Getting SBOMs and CVE scans into Concert](#3-getting-sboms-and-cve-scans-into-concert)
4. [Getting Concert CVEs into Instana](#4-getting-concert-cves-into-instana)
5. [How Concert links things together (the data model you must respect)](#5-how-concert-links-things-together)
6. [Troubleshooting](#6-troubleshooting)
7. [Command cookbook](#7-command-cookbook)
8. [Start from scratch (reset)](#8-start-from-scratch-reset)
9. [Lab inventory](#9-lab-inventory)

---

## 1. The moving parts

| Piece | What it does | Where it runs in the lab |
|---|---|---|
| Instana backend | Observability. Shows the *Vulnerabilities* tab on application perspectives once CVE events arrive. | `instanak3s` (192.168.178.105), UI `https://unit0-tenant0.instanak3s.lab.allwaysbeginner.com` |
| Instana host agent + **CVE sensor** (`com.instana.plugin.cve`) | Polls Concert every 6 h, turns CVEs into Instana `cve_issue` events. | native agent on `kubeflow` (192.168.178.95) |
| Concert | Inventory, SBOMs, CVE assessment, risk scoring. | k3s on `concert3` (192.168.178.94), API `:12443`, instance ID `0000-0000-0000-0000` |
| Concert **Instana ingestion job** | Pulls Instana application perspectives, clusters, namespaces and image names into Concert (creates applications + environment deployments). | Concert UI: Administration > Integrations |
| Concert **Kubernetes discovery** | Discovers workloads directly from a cluster; can run Concert's own trivy scans on the images. | Concert UI: Administration > Integrations |
| Scan/upload script | Scans running images with trivy and uploads package SBOM, image scan and a deploy SBOM to Concert. | `scripts/concert-scan-upload.sh` (build host `192.168.178.61`, master node, or CronJob) |

Data flow in one line:

```
cluster images --(trivy / Concert discovery)--> Concert findings --(CVE sensor on the agent)--> Instana events
```

---

## 2. One-time setup

### 2.1 Instana side

1. Backend feature flag `feature.cve.issues` must be enabled (Core CR on the Instana cluster; it is on in the lab).
2. The application perspective must show a **Vulnerabilities** tab (it does for `load-testing`).
3. Application perspectives that Concert should import must be Kubernetes-scoped. IBM docs say the
   perspective must define `kubernetes.cluster.name` and `kubernetes.namespace.name`; in the lab a perspective
   defined only by `kubernetes.deployment.namespace = load-testing` was imported as well. Only perspectives with
   traffic in the last 7 days are imported.
4. Create an API token for Concert: Instana > Settings > Security & Access > API Tokens > New API Token
   (default permissions are enough).

### 2.2 Concert side

1. Inventory > Environments > Create environment. Pick a short name without spaces (this becomes the
   `environment` in every deploy SBOM Concert generates).
2. Administration > Integrations > Connections > Create connection > **IBM Instana Observability**:
   endpoint `https://unit0-tenant0.instanak3s.lab.allwaysbeginner.com`, the token from 2.1, Validate, Create.
3. Administration > Integrations > Ingestion jobs > Create ingestion job: type Instana, the connection,
   target environment from step 1, Create, then **Run now** on the job's menu.
4. Profile > API key > Generate API key. Keep it, it is what the agent and all scripts use
   (`Authorization: C_API_KEY <key>`).

Result of a successful ingestion job: one Concert application per Instana perspective (`load-testing`,
`All Services`, `otel-demo`, ...), one deployment per application in the target environment, and one image
instance per running image named by the full path, e.g. `docker.io/mbx1010/vulnerable-echo-service` tag `2.0`
(no digest, Instana does not provide one).

### 2.3 The CVE sensor on the Instana agent

`/opt/instana/agent/etc/instana/configuration.yaml` on the agent host:

```yaml
com.instana.plugin.cve:
  enabled: true
  concert:
    base_url: 'https://concert3.lab.allwaysbeginner.com:12443/'
    instance_id: '0000-0000-0000-0000'
    api_key: '<CONCERT_API_KEY>'
    ignore_cert: true
    poll_rate: 10        # ignored: the sensor enforces a minimum of 360 minutes
```

Facts about the sensor (from `sensor-cve-2.0.14.jar`):

- It lists `GET /core/api/v1/vulnerability/cves?...page_size=100` and then calls
  `GET /core/api/v1/cves/{cve}` **with the header `Source: instana`** for every CVE.
- It only forwards a CVE when `data.associations.build_artifacts` in that response is non-empty.
  Otherwise it logs `Skipped due to : ... No associated images found` (TRACE level only) and drops it.
- A restart of the agent triggers an immediate poll. A full poll of ~750 CVEs takes ~10 minutes.

Enable trace logging for the sensor (hot-reloaded): append to `/opt/instana/agent/etc/org.ops4j.pax.logging.cfg`

```
log4j2.logger.cve.name = com.instana.agent.cve
log4j2.logger.cve.level = TRACE
```

---

## 3. Getting SBOMs and CVE scans into Concert

Three sources, in the order IBM recommends.

### 3.1 From Kubernetes with Concert's own discovery and scanning (Concert does the trivy work)

Administration > Integrations > Connections > Create connection > Kubernetes (kubeconfig or service-account token,
API server `https://192.168.178.35:6443`). Then create a discovery for that connection with the target
environment, the namespace(s) and **image scanning enabled** in *Advanced settings and filters*. Concert generates
Prisma-style CSV scans (`trivy-kubescan-<cluster>-<namespace>.csv`, columns `image, tag, digest, cve, ...`) and
ingests them itself.

**Known defect (verified on the clean instance, 2026-09-09):** Concert's own assessment job rejects its own CSV with
*"The vulnerabilities assessment can't be generated."* The real error, visible only in the short-lived task pod
(`ibm-roja-pipeline` image), is

```
assessment_utils.py:679 failed to persist assessment data due to : conversion between UTF8 and SQL_ASCII is not supported
... Assessment generation failed with error: ERR_VUL_MSG_4
```

Cause: the CSV contains U+2011 NON-BREAKING HYPHEN in CVE descriptions/titles (and tabs), and Concert's application
database is SQL_ASCII. The node VM scan (15 rows, plain ASCII) from the same discovery ingests fine.

**Why:** the standalone installer's Postgres image runs `initdb` without `--encoding`/locale and without `LANG`,
so the cluster and `appdb` are created as **SQL_ASCII** (`create database appdb;` in the entrypoint inherits it).
Concert's own pipeline then trips over the first non-ASCII character. Two ways to deal with it:

**Workaround (automatic):** `scripts/concert-kubescan-fix.sh` finds every `trivy-kubescan-*.csv` record in the
event log with status `error_processing`, downloads it from the landing zone, replaces non-ASCII and control
characters with ASCII equivalents, re-uploads it as `image_scan` with the same application context Concert used,
waits for the result and remembers what it fixed in `~/.concert-kubescan-fix.state`:

```bash
scripts/concert-kubescan-fix.sh --dry-run            # show what would be re-ingested
scripts/concert-kubescan-fix.sh --since 2026-09-09   # fix everything failed since that date
scripts/concert-kubescan-fix.sh -a load-testing -v 1.0.0   # attach to a different application instead
```

It reads the same `~/.concert.env` as the upload script. Run it after every discovery run (or from cron).
Lab run (2026-09-09 11:14 UTC): 1993 rows, 125 cells changed, new record `processing_completed`, findings attached to the digest-bearing instances of the discovered application.

**Real fix (database encoding UTF8):** convert `appdb` once; afterwards Concert's own scans ingest unchanged.
This is a manual, one-time operation on a test system, do it with a backup and Concert scaled down:

```bash
# 0. backup (Concert's own tool) - on concert3
/home/manfred/ibm-concert/ibm-concert-std/bin/backup
# 1. stop everything that talks to the DB (keep the two postgres and minio deployments)
kubectl -n ibm-concert get deploy -o name | grep -v 'postgres\|minio' | xargs -I{} kubectl -n ibm-concert scale {} --replicas=0
# 2. shell in the appdb pod with the superuser credentials (role name comes from the pod's secret directory)
kubectl -n ibm-concert exec -it deploy/roja-postgres-appdb -- bash -c 'for i in $(ls /mnt/infra/app-cfg-oob-secret); do export $i=$(<"/mnt/infra/app-cfg-oob-secret/$i"); done; export PGPASSWORD="$OOB_POSTGRES_PASSWORD" PGUSER="$OOB_POSTGRES_USER" PGHOST=localhost PGSSLMODE=require PATH=/usr/pgsql-15/bin:$PATH; exec bash'
#    inside that shell:
psql -d postgres -c "SELECT datname, pg_encoding_to_char(encoding) FROM pg_database;"      # shows SQL_ASCII
psql -d postgres -c "CREATE DATABASE appdb_utf8 TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' OWNER \"$PGUSER\";"
pg_dump -d appdb -Fc -f /mnt/vol/appdb-sqlascii.dump
pg_restore --exit-on-error -d appdb_utf8 /mnt/vol/appdb-sqlascii.dump
psql -d postgres -c "ALTER DATABASE appdb RENAME TO appdb_sqlascii;" -c "ALTER DATABASE appdb_utf8 RENAME TO appdb;"
exit
# 3. start Concert again
kubectl -n ibm-concert get deploy -o name | grep -v 'postgres\|minio' | xargs -I{} kubectl -n ibm-concert scale {} --replicas=1
```

If `pg_restore` stops on an "invalid byte sequence" row, that row holds bytes that are not valid UTF-8; fix or
drop it in `appdb` and repeat. `cfgdb` has the same encoding but no scan content, leave it. Report to IBM that the
installer should pass `--encoding=UTF8` to `initdb` (`POSTGRES_INITDB_ARGS`).

**Image naming rule (important):** Concert's Kubernetes discovery names image instances exactly as written in the pod
spec; its scanner writes names without the registry (`mbx1010/chat-app`); the Instana ingestion uses what Instana
reports. Reference images in your manifests **without** the `docker.io/` prefix (`mbx1010/chat-app:2.0`,
`nginx:1.27-alpine`), otherwise you get two instances per image and the scan lands on the wrong one. After changing
manifests, re-run both the Kubernetes discovery and the Instana ingestion job.

### 3.2 From Instana (topology only)

The Instana ingestion job (2.2) brings applications, environments, namespaces and image names. It brings **no**
packages and **no** CVEs. You still need 3.1 or 3.3 for findings. Image names come straight from the pod spec as
Instana reports them; after changing manifests, **Run now** the job again or the artifacts keep the old names.

### 3.3 Native uploads with trivy (the script)

`scripts/concert-scan-upload.sh` does, per running image in a namespace:

1. `trivy image --scanners vuln --format cyclonedx` -> uploaded as `package_sbom` with
   `metadata={"application_name":"<app>","application_version":"1.0.0"}` (package inventory).
2. `trivy image --scanners vuln --format json` (trivy **native** JSON) -> uploaded as `image_scan` with
   `metadata={"scanner_name":"trivy","application_name":"<app>","application_version":"1.0.0"}`.
   Only the native format carries the image digest into Concert's findings.
3. A ConcertDef 2.0.0 **deploy SBOM** for the application/environment whose image `uri` is
   `<repo>:<tag>@<digest>`; this writes the running digest onto the Instana-discovered image instances.
4. Polls the Concert event log for every upload and prints the final status.

Run it (build host, needs docker):

```bash
CONCERT_API_KEY=<CONCERT_API_KEY> ~/tx-scan/concert-scan-upload.sh -c kubernetes-admin@kubernetes -n load-testing -a load-testing -v 1.0.0 -o ~/tx-scan/concert
```

Master node (no docker, trivy is downloaded automatically, credentials in `~/.concert.env`):

```bash
~/concert-scan-upload-master.sh            # defaults: namespace load-testing, app load-testing 1.0.0
~/concert-scan-upload-master.sh -a "All Services" --skip-scan
```

CronJob in the cluster: `k8s/concert-scan-upload-cronjob.yaml` (see its header for the secret and ConfigMap).

The application name/version must be the ones the Instana ingestion job created (they follow the Instana
perspective names, not the Kubernetes deployment names).

Manual equivalents of the uploads:

```bash
# package inventory
curl -sk -X POST "$B/ingestion/api/v1/upload_files" -H "Authorization: C_API_KEY $K" -H "InstanceID: 0000-0000-0000-0000" \
  -F data_type=package_sbom -F 'metadata={"application_name":"load-testing","application_version":"1.0.0"}' -F filename=@image.cdx.json
# CVE findings (trivy native json)
curl -sk -X POST "$B/ingestion/api/v1/upload_files" -H "Authorization: C_API_KEY $K" -H "InstanceID: 0000-0000-0000-0000" \
  -F data_type=image_scan -F 'metadata={"scanner_name":"trivy","application_name":"load-testing","application_version":"1.0.0"}' -F filename=@image.trivy.json
# ConcertDef (application / build / deploy) all go through application_sbom
curl -sk -X POST "$B/ingestion/api/v1/upload_files" -H "Authorization: C_API_KEY $K" -H "InstanceID: 0000-0000-0000-0000" \
  -F data_type=application_sbom -F filename=@deploy-sbom.json
```

HTTP 202 only means *queued*. Always check the event log (section 7).

---

## 4. Getting Concert CVEs into Instana

Prerequisite chain, each verified in the lab:

1. Agent connected to the Instana backend (`Connected using HTTP/2 to instanak3s...:1444` in `agent.log`).
2. Sensor active (`Activated Sensor for concertCve`) and Concert reachable (`GET .../vulnerability/cves` = 200
   in Concert's gateway log).
3. CVEs exist in Concert **and** the sensor's detail call returns build artifacts for them.

Check step 3 yourself without touching the agent (this is exactly what the sensor sees):

```bash
curl -sk -H "Authorization: C_API_KEY <CONCERT_API_KEY>" -H "InstanceID: 0000-0000-0000-0000" -H "Source: instana" \
  https://concert3.lab.allwaysbeginner.com:12443/core/api/v1/cves/CVE-2022-22965
```

Expected when it works: `{"cve":"...","data":{"description":...,"vector_string":...,"associations":{"build_artifacts":[{"id":...,"name":"docker.io/mbx1010/vulnerable-echo-service","tag":"2.0","digest":"sha256:..."}]}}}`.
Without the `Source: instana` header the same call returns **403 "The vulnerability can't be retrieved"**, which is
misleading and cost a day.

**State on 2026-09-09 before the reset:** `build_artifacts` was `[]` for all 745 CVEs although Concert's own
v2 blast radius linked each CVE to the deployed image, the application and the environment. See
`docs/CONCERT-INSTANA-CVE-INVESTIGATION.md` for the evidence.

**State after the clean-slate rebuild (2026-09-09, 09:25 UTC):** environment `kubernetes`, Instana job and
Kubernetes discovery re-run with registry-less image names, Concert's own scan re-uploaded ASCII-clean. Concert
now shows application `load-testing 1.0.0` with 8 build artifacts, 1 deployment in `kubernetes`, 290 packages,
1991 CVE findings, and the v2 blast radius is complete (CVE -> package -> image with digest -> application ->
environment). The `Source: instana` detail call **still returns `build_artifacts: []` for all 777 CVEs**, also after the
10:00 UTC `vulnerability_impact_table_sync_job`, and also after findings were attached to the Instana-created
instances (`docker.io/...` names, digests set via deploy SBOM, scan re-uploaded under those names).

**ROOT CAUSE, confirmed 2026-09-09 10:51 UTC with Postgres statement logging (`ALTER SYSTEM SET log_statement='all'`
for one request):** the `Source: instana` handler (rojacore `GetVulnerability` -> `PgGetVulnerabilityAssociations`)
runs this query for the build-artifact associations:

```sql
WITH accessible_entities AS (
    SELECT entity_id FROM access_control WHERE (id = ANY($1) OR id = '*')
)
SELECT DISTINCT
    bai.build_artifacts_id AS image_id,
    va.scan_object_id      AS image_name,
    va.scan_object_attr1   AS image_tag,
    va.scan_object_attr2   AS image_digest
FROM vulnerability_assessment va
JOIN build_artifact_instances bai
    ON bai.name = va.scan_object_id
    AND COALESCE(bai.image_tag, '')    = COALESCE(va.scan_object_attr1, '')
    AND COALESCE(bai.image_digest, '') = COALESCE(va.scan_object_attr2, '')
LEFT JOIN application_deployment_components adc ON adc.build_artifact_instances_id = bai.id
LEFT JOIN environments env ON env.id = adc.environments_id
WHERE
    va.id::text = $2          -- <== BUG: va.id is the assessment UUID; $2 is 'CVE-2022-22965'
    AND va.scan_type = 'image_scan'
    AND va.is_archived = false
    AND va.status <> 'Closed'
    AND ( va.application_id IN (SELECT entity_id FROM accessible_entities)
          OR env.id IN (SELECT entity_id FROM accessible_entities)
          OR (va.application_id IS NULL AND env.id IS NULL) )
-- parameters: $1 = '{admin}', $2 = 'CVE-2022-22965'
```

The WHERE clause compares the **assessment row id** (`va.id`, a UUID) with the **CVE identifier** passed as `$2`.
That can never match, so the result is always empty and every CVE is dropped by the sensor with
"No associated images found". The correct predicate is `va.cve = $2`. Everything else in the query (name + tag +
digest join to the deployed instances, admin access) matches our data. This is a Concert 3.0.1.1 product defect;
no ingestion variant, naming or digest trick can work around it. Open an IBM case with the query above.

Side findings from the same log, worth adding to the case: `relation "vulnerability_business_impact_view" does not
exist` (CVE topology subgraph -> HTTP 400; fresh 3.0.x installs never create that 2.x view), `missing FROM-clause
entry for table "vbi"` (another broken query), and `conversion between UTF8 and SQL_ASCII is not supported`
(Concert's own image-scan assessment). Postgres errors and statements are only in the PVC on the concert3 host:
`/var/lib/rancher/k3s/storage/pvc-<appdb>/postgresql/data/log/postgresql-<Day>.log` (sudo needed).

How to get a psql prompt on the appdb (role is not `postgres`, UID 1000 has no passwd entry):

```bash
kubectl -n ibm-concert exec -it deploy/roja-postgres-appdb -- bash -c 'for i in $(ls /mnt/infra/app-cfg-oob-secret); do export $i=$(<"/mnt/infra/app-cfg-oob-secret/$i"); done; export PGPASSWORD="$OOB_POSTGRES_PASSWORD"; /usr/pgsql-15/bin/psql "host=localhost user=$OOB_POSTGRES_USER dbname=appdb sslmode=require"'
```

Statement logging on/off (reload, no restart): `ALTER SYSTEM SET log_statement = 'all'; SELECT pg_reload_conf();`
and `ALTER SYSTEM RESET log_statement; SELECT pg_reload_conf();`. Tables live in schema `ibm_roja_app`
(`SET search_path TO ibm_roja_app;` before `\dv` or `SELECT count(*) FROM vulnerability_impact;`).

Once the endpoint returns artifacts: `sudo systemctl restart instana-agent` on the agent host, wait ~10 min,
then look for `cve_issue` in the agent log and CVE events in Instana (Events, or the Vulnerabilities tab of the
application).

---

## 5. How Concert links things together

Learned the hard way; respect this or nothing correlates.

- **Build artifact** = the image *name* (basename), e.g. `vulnerable-echo-service`. **Instances** of it are the
  concrete images: name (`docker.io/mbx1010/vulnerable-echo-service`), `image_tag`, `image_digest`.
  The Instana ingestion job creates instances with full name + tag and **no digest**.
- **Findings** (v2 `cve_findings`) carry `scan_object_id` (image name), `scan_object_attr1` (tag),
  `scan_object_attr2` (digest). CycloneDX uploads leave the digest empty; trivy native JSON fills it.
- Findings are attached to the deployed instance by name + tag (+ digest). Environment impact and the Instana
  export need the digest on both sides.
- Image scan uploads for an image that belongs to several applications need `application_name`/`application_version`
  in the metadata, otherwise Concert answers *"Other instances of this component are already associated with
  applications"*.
- Re-uploading a CycloneDX file with the same `serialNumber` fails with *"The CycloneDX SBOM already exists"*.
  Trivy generates a new serial per run, so real runs are fine.
- CycloneDX must be 1.4-1.6. Trivy 0.6x writes 1.7; the script downgrades it.
- Tags move. If the registry's `:latest` no longer points to the running digest, scan the running digest
  (`repo@sha256:...`); the script does this automatically when it detects the mismatch.
- ConcertDef build SBOMs need a matching **1.0.2 application SBOM** with a component of the same name/version,
  otherwise: *"No application SBOM has been uploaded for the component name and version specified"*.
- `build_sbom` is not an upload data type; build and deploy ConcertDef files go through `application_sbom`.

---

## 6. Troubleshooting

| Symptom | Where to look | Meaning / fix |
|---|---|---|
| No CVEs in Instana | agent log with TRACE (2.3): `Skipped due to : ... No associated images found` | Concert returns no build artifacts for the CVE. On 3.0.1.1 this is a Concert defect: the association query filters on `va.id::text = '<CVE>'` instead of `va.cve` (section 4). |
| `GET /core/api/v1/topology/subgraphs/cves/{id}` returns 400 | appdb Postgres log on the host | `relation "vulnerability_business_impact_view" does not exist` - same defect as above |
| `curl .../core/api/v1/cves/<id>` gives 403 | you forgot the header | add `-H "Source: instana"` |
| Upload returned 202 but nothing shows up | event log (7.2) | ingestion is async; the error text is only there |
| `Unsupported CycloneDX version` | event log | trivy wrote CycloneDX 1.7, downgrade to 1.6 |
| `Failed to fetch the build artifact by name while resolving application context` | event log | package_sbom uploaded without `application_name`/`application_version` metadata |
| `Other instances of this component are already associated with applications` | event log | add application name/version to the image_scan metadata |
| `The CycloneDX SBOM already exists` | event log | same serialNumber uploaded twice, regenerate the file |
| `The vulnerabilities assessment can't be generated` | event log, Concert-generated `trivy-kubescan-*.csv`; real error only in the task pod | Concert defect: `conversion between UTF8 and SQL_ASCII is not supported` (non-ASCII in CVE text, appdb is SQL_ASCII). Sanitize the CSV to ASCII and re-upload as `image_scan` (section 3.1) |
| Two instances per image (`docker.io/mbx1010/x` and `mbx1010/x`), findings on the wrong one | app -> Build artifacts, or `v2 build_artifacts/{id}/instances` | manifests reference `docker.io/...`; remove the prefix, redeploy, re-run discovery **and** the Instana job |
| Instana-imported artifacts still named `docker.io/...` after fixing manifests | app -> Build artifacts | the Instana job only refreshes when re-run: Ingestion jobs -> Run now |
| Error text in the event log is generic, need the real error | task pods `task-*` in `ibm-concert` (live only a few minutes) | run the capture loop below while the job runs |
| `/core/api/v1/applications` returns 400 | Concert bug on this build | list applications via `/core/api/v1/environments/{id}/applications` |
| UI still shows deleted objects | browser cache | reload / re-login; the API is authoritative |
| Sensor never polls | `Task ConcertCveDataCollection scheduled (... delay=360 MINUTES)` | poll_rate below 360 is ignored; restart the agent to force a poll |
| Concert deploy SBOM rejected: `does not match any allowed` | schema | ConcertDef 2.0.0 image objects allow only `bom-ref,type,name,uri`; put tag/digest into `uri` |

Where the logs are:

- Agent: `/opt/instana/agent/data/log/agent.log` (rotates at 5 MB into `agent.log.N`).
- Concert gateway (every API call with client IP and status): `kubectl -n ibm-concert logs deploy/concert-solis-gw`.
- Concert core resolution of uploads (`CreateComponents`, `CreateUpdateResponse` with `ImageId`, `ApplicationId`,
  `EnvironmentId`): `kubectl -n ibm-concert logs deploy/rojacore`.
- Concert pipeline (scan processing): `kubectl -n ibm-concert logs deploy/roja-pipeline`; ingestion job runner:
  `deploy/roja-ingestion`.
- Concert **task pods** (`task-*`, images `ibm-roja-pipeline` / `ibm-concert-auto-mod`) do the actual discovery,
  scanning and assessment work and are deleted within minutes. Capture them while a job runs (on concert3):

```bash
mkdir -p /tmp/task-logs; cat > /tmp/task-logs/capture.sh <<'EOS'
#!/bin/bash
# capture logs of every task-* pod in ibm-concert for one hour
end=$(( $(date +%s) + 3600 ))
while [ "$(date +%s)" -lt "$end" ]; do
  for p in $(kubectl -n ibm-concert get pods -o name 2>/dev/null | grep '^pod/task-' | cut -d/ -f2); do
    kubectl -n ibm-concert logs "$p" --all-containers --timestamps > "/tmp/task-logs/$p.log" 2>/dev/null
  done
  sleep 20
done
EOS
chmod +x /tmp/task-logs/capture.sh; nohup /tmp/task-logs/capture.sh >/dev/null 2>&1 &
# afterwards:
grep -l -i 'error\|failed' /tmp/task-logs/task-*.log
```

---

## 7. Command cookbook

Set once per shell:

```bash
export B=https://concert3.lab.allwaysbeginner.com:12443
export K=<CONCERT_API_KEY>
alias capi='curl -sk -H "Authorization: C_API_KEY $K" -H "InstanceID: 0000-0000-0000-0000"'
```

### 7.1 Inventory

```bash
capi "$B/core/api/v1/environments?page_size=50"                          # environments
capi "$B/core/api/v1/environments/<env-id>/applications?page_size=50"   # applications (the /applications list is broken)
capi "$B/core/api/v1/applications/<app-id>/build_artifacts?page_size=50" # deployed image instances of an app
capi "$B/core/api/v2/build_artifacts?page_size=100"                       # build artifacts (basenames)
capi "$B/core/api/v2/build_artifacts/<artifact-id>/instances?page_size=50" # instances with tag + digest
capi "$B/core/api/v1/ingestion_jobs?page_size=50"                          # ingestion jobs
capi "$B/core/api/v1/credentials?page_size=50"                             # connections
```

### 7.2 Event log (the only place ingestion errors appear)

```bash
capi -X POST "$B/concert/ingestion/api/v1/table_of_contents/lz/advanced_search" -H "Content-Type: application/json" -d '{
 "filters":{"logic":"and","conditions":[
   {"field":"date_range","value":"start=2026-09-09T00:00:00Z,end=2026-09-10T00:00:00Z","operator":"RANGE"},
   {"field":"data_type","value":"image_scan","operator":"IN"}]},
 "sort":{"field":"timestamp","order":"desc"},"pagination":{"page":1,"page_size":50}}' \
 | python3 -c 'import json,sys; [print(r["timestamp"][:19],r["status"],r["file_name"],"|",(r.get("additional_details") or "")[:160]) for r in json.load(sys.stdin)["toc_records"]]'
```

Other useful `data_type` values: `package_sbom`, `application_sbom`, `kubernetes_resources`, `DISCOVERY`,
`vulnerability_impact_table_sync_job` (runs every 2 h on even hours UTC). Download an ingested file:
`capi "$B/core/api/v1/landing_zone/data?filepath=<lz_path>"` (JSON files come back base64-encoded).

### 7.3 CVEs and findings

Re-ingest failed Concert-generated image scans: `scripts/concert-kubescan-fix.sh [--dry-run] [--since YYYY-MM-DD]` (section 3.1).


```bash
capi "$B/core/api/v1/vulnerability/cves?search=CVE-2025-8961&page_size=5"      # priority, risk score, findings
capi "$B/core/api/v2/cve_findings?search=CVE-2022-22965&page_size=20"           # per-finding image name/tag/digest, app, env
capi "$B/core/api/v2/cves/CVE-2022-22965/blast_radius"                            # CVE -> packages -> images -> apps -> envs
capi -H "Source: instana" "$B/core/api/v1/cves/CVE-2022-22965"                   # what the Instana sensor sees
capi "$B/core/api/v2/environments/<env-id>/deployments?page_size=20"             # deployments in an environment
capi "$B/core/api/v2/environments/<env-id>/deployments/<dep-id>/cve_findings?page_size=1"
```

Note: Concert's CVE list hides Priority 3 and Deprioritized findings by default in the UI; the API shows all.

### 7.4 Agent side (kubeflow)

```bash
sudo systemctl restart instana-agent                                   # force an immediate Concert poll
L=/opt/instana/agent/data/log/agent.log
grep -c "Fetching CVE details" $L; grep -c "Skipped due to" $L          # fetched vs skipped in the current log
grep -oE "Skipped due to : .{0,90}" $L | sort | uniq -c | sort -rn | head
grep -iE "cve_issue|sendCdc" $L | head                                   # anything actually sent?
```

### 7.5 Deleting things via API (UI sometimes refuses)

```bash
capi -X DELETE "$B/core/api/v1/environments/<environment id or name>"                 # 204 = deleted
capi -X POST "$B/core/api/v2/delete_applications" -H "Content-Type: application/json" -d '{"applications":["<name or id>"]}'
capi -X DELETE "$B/core/api/v2/build_artifacts/<artifact-id>/instances/<instance-id>"
capi -X DELETE "$B/core/api/v1/ingestion_jobs/<job-id>"
capi -X DELETE "$B/core/api/v1/credentials?delete_ids=<connection-id>"
```

If the UI still lists a deleted object, it is cached: reload or re-login, the API is authoritative.
Do deletions one object type at a time and wait for each; an interrupted application delete has wedged the
Concert database in this lab before.

---

## 8. Start from scratch (reset)

Done on 2026-09-09. Order that worked:

1. UI: delete all applications (this also removed build artifacts, findings and SBOMs).
2. UI: delete all ingestion jobs, then all connections.
3. API: delete every environment (the UI refused): `capi -X DELETE "$B/core/api/v1/environments/<id>"`.
4. Verify empty: environments, applications, `v2 build_artifacts`, `vulnerability/cves`, `v2 cve_findings`,
   ingestion jobs, credentials, `software_composition/sboms` all return 0.
5. Rebuild in this order and **verify after each step**: new environment -> Instana connection + ingestion job
   (Run now) -> Kubernetes connection + discovery with image scanning -> check the `Source: instana` endpoint ->
   restart the agent -> only then the script uploads if needed.

Outcome of the 2026-09-09 rebuild (environment `kubernetes`):

- Instana job: OK, created application `load-testing` 1.0.0 (plus one per other application perspective).
- Kubernetes discovery: OK, created `kubernetes_demo-environment_load-testing_load-testing` and the image
  instances; the node VM scan ingested, the image scan CSV failed with the SQL_ASCII error (section 3.1) and was
  re-uploaded ASCII-clean by hand.
- Because the workload manifests had been changed to registry-less image names in between, the Instana job had to
  be **re-run** before its artifacts matched (`docker.io/mbx1010/...` -> `mbx1010/...`).
- Redeploying the demo namespace: `k8s/namespace.yaml`, `configmaps.yaml`, `echo-service-deployment.yaml`,
  `python-app-deployment.yaml`, `juice-shop-deployment.yaml` (substitute `NAMESPACE=load-testing`,
  `IMAGE_TAG=2.0`), `demo-hub/k8s/*.yaml` (+ configmap `demo-hub-html` from `demo-hub/index.html`),
  `chat-app/k8s/*.yaml`, `odcg/k8s/odcg.yaml`; `k8s/ingress.yaml` is traefik/TLS specific and was skipped.

---

## 9. Lab inventory

| Host | Role | Access |
|---|---|---|
| instanak3s 192.168.178.105 | Instana backend (k3s) | `kubectl --kubeconfig ~/.kube/instana.yaml --context=instana`; no SSH key for manfred |
| kubeflow 192.168.178.95 | native Instana agent with CVE sensor, no Kubernetes | `ssh kubeflow`; agent config is root-only |
| concert3 192.168.178.94 | Concert 3.0.1.1 (k3s, namespace `ibm-concert`) | `ssh concert3`; API `https://concert3.lab.allwaysbeginner.com:12443` |
| master-node 192.168.178.35 | workload cluster (`kubernetes-admin@kubernetes`), namespace `load-testing` | kubectl from the Mac |
| linux / .61 192.168.178.61 | build host: docker, trivy image, `~/tx-scan/` scripts and scan outputs | `ssh manfred@192.168.178.61` |

Images in `load-testing` (as written in the manifests, no registry prefix, see 3.1):
`mbx1010/{chat-app,load-test-app,vulnerable-echo-service}:2.0`, `mbx1010/otel-dc-genai:1.0.10`,
`nginx:1.27-alpine`, `bkimminich/juice-shop:latest`, `curlimages/curl:8.11.1`, `aquasec/trivy:0.72.0` (CronJob).
Concert environment: `kubernetes` (Instana job and Kubernetes discovery both target it).

Instana application perspectives imported by Concert: `load-testing`, `All Services`, `otel-demo`, `family-ai`,
`local-ai`, `bum-namespace` (one Concert application each, which is why the same image shows up under several
applications).
