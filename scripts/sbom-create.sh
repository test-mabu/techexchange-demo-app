#!/usr/bin/env bash
set -euo pipefail

export CONCERT_HOST="${CONCERT_HOST:?Need to set CONCERT_HOST}"
export CONCERT_PORT="${CONCERT_PORT:-12443}"
export INSTANCE_ID="${INSTANCE_ID:?Need to set INSTANCE_ID}"
export CONCERT_API_KEY="${CONCERT_API_KEY:?Need to set CONCERT_API_KEY}"
export AUTH_TYPE="${AUTH_TYPE:-C_API_KEY}"

: "${CONCERT_HOST:?Need to set CONCERT_HOST}"
: "${INSTANCE_ID:?Need to set INSTANCE_ID}"
: "${CONCERT_API_KEY:?Need to set CONCERT_API_KEY}"

NAMESPACE="${NAMESPACE:?Need to set NAMESPACE}"
OUTDIR="${OUTDIR:-sboms}"
mkdir -p "$OUTDIR"

kubectl get deployments -n "$NAMESPACE" \
  -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{range .spec.template.spec.containers[*]}{@.image}{";"}{end}{"\n"}{end}' \
| while IFS="|" read -r namespace deploy images; do
  for image in ${images//;/ }; do
    safe=$(echo "$image" | tr '/:' '__')
    sbom_file="${OUTDIR}/${namespace}_${deploy}_${safe}-sbom.json"

    echo "→ Generating SBOM for $namespace/$deploy → $image…"
    if ! trivy image \
      --scanners vuln \
      --format cyclonedx \
      --output "$sbom_file" \
      "$image"
    then
      echo "⚠️  Error generating SBOM for $image, skipping." >&2
      continue
    fi

    echo "→ Uploading SBOM to Concert: $sbom_file…"
    if ! curl -k -sS -X POST "https://${CONCERT_HOST}:${CONCERT_PORT}/ingestion/api/v1/upload_files" \
        -H "accept: application/json" \
        -H "InstanceID: ${INSTANCE_ID}" \
        -H "Authorization: ${AUTH_TYPE} ${CONCERT_API_KEY}" \
        -H "Content-Type: multipart/form-data" \
        -F "data_type=package_sbom" \
        -F "filename=@${sbom_file}"
    then
      echo "⚠️  Error uploading $sbom_file, skipping." >&2
      continue
    fi

    echo ""
    echo "✓ Uploaded $sbom_file"
  done
done

echo ""
echo "All done: generated and uploaded all possible SBOMs."
