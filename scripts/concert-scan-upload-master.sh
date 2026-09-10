#!/usr/bin/env bash
# concert-scan-upload-master.sh
#
# Command-line wrapper for running the Concert scan/upload directly on the
# Kubernetes master node (kubeadm, containerd, no docker):
#   * uses the node's own kubectl/admin kubeconfig (no --context needed)
#   * downloads a static trivy binary into ~/.local/bin on first use
#   * reads the Concert credentials from ~/.concert.env
#   * scans the images running in NAMESPACE and uploads them to the Concert
#     application that Concert imported from Instana (APP_NAME/APP_VERSION)
#
# Install on the master node (from your workstation):
#   scp scripts/concert-scan-upload.sh scripts/concert-scan-upload-master.sh <user>@<master>:~/
#   ssh <user>@<master>
#   cat > ~/.concert.env <<'EOF'
#   CONCERT_URL=https://concert3.lab.allwaysbeginner.com:12443
#   CONCERT_INSTANCE_ID=0000-0000-0000-0000
#   CONCERT_API_KEY=<base64 user:apikey>
#   EOF
#   chmod 600 ~/.concert.env; chmod +x ~/concert-scan-upload*.sh
#
# Run:
#   ~/concert-scan-upload-master.sh                       # namespace load-testing -> app load-testing 1.0.0
#   ~/concert-scan-upload-master.sh -n family-ai -a family-ai
#   ~/concert-scan-upload-master.sh -a "All Services" --skip-scan   # same images, another imported app
#   ~/concert-scan-upload-master.sh --dry-run
# Any option of concert-scan-upload.sh can be appended.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$HERE/concert-scan-upload.sh"
[[ -f "$MAIN" ]] || { echo "concert-scan-upload.sh not found next to this wrapper ($HERE)" >&2; exit 1; }

# kubeconfig: prefer the user's, fall back to the kubeadm admin config (needs sudo/root)
if [[ -z "${KUBECONFIG:-}" && ! -f "$HOME/.kube/config" && -r /etc/kubernetes/admin.conf ]]; then
  export KUBECONFIG=/etc/kubernetes/admin.conf
fi
command -v kubectl >/dev/null || { echo "kubectl not in PATH" >&2; exit 1; }
kubectl get ns >/dev/null 2>&1 || { echo "kubectl cannot reach the cluster (kubeconfig?)" >&2; exit 1; }

export NAMESPACE="${NAMESPACE:-load-testing}"
export APP_NAME="${APP_NAME:-$NAMESPACE}"
export APP_VERSION="${APP_VERSION:-1.0.0}"
export OUTDIR="${OUTDIR:-$HOME/concert-scans}"
export CONCERT_ENV_FILE="${CONCERT_ENV_FILE:-$HOME/.concert.env}"
export PATH="$HOME/.local/bin:$PATH"

[[ -f "$CONCERT_ENV_FILE" || -n "${CONCERT_API_KEY:-}" ]] || {
  echo "no credentials: create $CONCERT_ENV_FILE (see header of this script) or export CONCERT_API_KEY" >&2; exit 1; }

exec bash "$MAIN" -n "$NAMESPACE" -a "$APP_NAME" -v "$APP_VERSION" -o "$OUTDIR" "$@"
