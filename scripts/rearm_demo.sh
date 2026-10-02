#!/usr/bin/env bash
# Re-arm the local demo: redeploy v2.4.0, restart the payment-service port-forward, and leak memory so the alert fires again.
set -euo pipefail
cd "$(dirname "$0")/.."

kubectl apply -f k8s/payment-service/manifests.yaml >/dev/null
kubectl rollout status deployment/payment-service --timeout=90s

pkill -f "port-forward svc/payment-service" || true
sleep 1
nohup kubectl port-forward svc/payment-service 8080:8080 -n default >/tmp/pf-payment.log 2>&1 &
sleep 4

curl -s "localhost:8080/leak?entries=230000"; echo
echo "Leaked. PaymentServiceHighMemory should fire in about 60-90s; the dashboard will open a new incident."
