#!/bin/bash
# Complete monitoring stack installation for KubeDB DBaaS
# Installs Prometheus + Grafana + Imports Dashboards

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="monitoring"

echo "========================================="
echo "KubeDB Monitoring Stack Installation"
echo "========================================="
echo ""

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl not found. Please install kubectl first."
    exit 1
fi

# Check cluster connection
echo "📡 Checking cluster connection..."
if ! kubectl cluster-info &> /dev/null; then
    echo "❌ Cannot connect to Kubernetes cluster. Please check your kubeconfig."
    exit 1
fi
echo "✅ Cluster connected"
echo ""

# Step 1: Install Prometheus
echo "📊 Installing Prometheus..."
kubectl apply -f "$SCRIPT_DIR/prometheus-deployment.yaml"
echo "✅ Prometheus deployed"
echo ""

# Step 2: Install Grafana
echo "📈 Installing Grafana..."
kubectl apply -f "$SCRIPT_DIR/grafana-deployment.yaml"
echo "✅ Grafana deployed"
echo ""

# Step 3: Wait for pods to be ready
echo "⏳ Waiting for Prometheus to be ready..."
kubectl wait --for=condition=ready pod -l app=prometheus -n $NAMESPACE --timeout=300s || true

echo "⏳ Waiting for Grafana to be ready..."
kubectl wait --for=condition=ready pod -l app=grafana -n $NAMESPACE --timeout=300s || true
echo ""

# Step 4: Get Grafana service information
echo "🔍 Getting service information..."
GRAFANA_NODEPORT=$(kubectl get svc grafana -n $NAMESPACE -o jsonpath='{.spec.ports[0].nodePort}')
GRAFANA_LB=$(kubectl get svc grafana-loadbalancer -n $NAMESPACE -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "pending")

echo ""
echo "========================================="
echo "✅ Installation Complete!"
echo "========================================="
echo ""
echo "📊 Prometheus:"
echo "   NodePort: http://<node-ip>:30900"
echo "   Internal: http://prometheus-server.monitoring.svc.cluster.local"
echo ""
echo "📈 Grafana:"
echo "   NodePort: http://<node-ip>:$GRAFANA_NODEPORT"
if [ "$GRAFANA_LB" != "pending" ]; then
    echo "   LoadBalancer: http://$GRAFANA_LB:3000"
fi
echo "   Username: admin"
echo "   Password: admin"
echo ""
echo "Next Steps:"
echo "1. Access Grafana using one of the URLs above"
echo "2. Login with admin/admin"
echo "3. Run: ./import-dashboard.sh to import the KubeDB dashboard"
echo ""
echo "Or import manually:"
echo "  Grafana UI → Dashboards → Import → Upload JSON file"
echo "  File: $SCRIPT_DIR/grafana-dashboard-database.json"
echo ""

# Step 5: Optional - Auto import dashboard
read -p "Would you like to import the KubeDB dashboard now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "📥 Importing dashboard..."

    # Port forward to access Grafana
    echo "Setting up port-forward to Grafana..."
    kubectl port-forward -n $NAMESPACE svc/grafana 3000:3000 &
    PF_PID=$!
    sleep 5

    # Import dashboard
    export GRAFANA_URL="http://localhost:3000"
    export GRAFANA_USER="admin"
    export GRAFANA_PASSWORD="admin"

    if bash "$SCRIPT_DIR/import-dashboard.sh"; then
        echo "✅ Dashboard imported successfully!"
    else
        echo "⚠️  Dashboard import failed. You can import manually later."
    fi

    # Kill port-forward
    kill $PF_PID 2>/dev/null || true
fi

echo ""
echo "========================================="
echo "Monitoring Stack Ready! 🎉"
echo "========================================="
