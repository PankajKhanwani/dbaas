#!/bin/bash
# KubeDB Installation Script
# This script installs KubeDB operator and catalog on your Kubernetes cluster

set -e

echo "=========================================="
echo "KubeDB Installation Script"
echo "=========================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
KUBEDB_VERSION="v2024.11.18"
KUBEDB_NAMESPACE="kubedb"

echo -e "${BLUE}Configuration:${NC}"
echo "  KubeDB Version: $KUBEDB_VERSION"
echo "  Namespace: $KUBEDB_NAMESPACE"
echo ""

# Check if kubectl is installed
if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}Error: kubectl is not installed${NC}"
    exit 1
fi

# Check if helm is installed
if ! command -v helm &> /dev/null; then
    echo -e "${RED}Error: helm is not installed${NC}"
    echo "Install helm: https://helm.sh/docs/intro/install/"
    exit 1
fi

# Check cluster connectivity
echo -e "${BLUE}Step 1: Checking cluster connectivity...${NC}"
if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}Error: Cannot connect to Kubernetes cluster${NC}"
    echo "Please check your KUBECONFIG and cluster access"
    exit 1
fi
echo -e "${GREEN}✓ Cluster is accessible${NC}"
echo ""

# Add KubeDB Helm repository
echo -e "${BLUE}Step 2: Adding KubeDB Helm repository...${NC}"
helm repo add appscode https://charts.appscode.com/stable/
helm repo update
echo -e "${GREEN}✓ Helm repository added${NC}"
echo ""

# Create namespace
echo -e "${BLUE}Step 3: Creating namespace '$KUBEDB_NAMESPACE'...${NC}"
kubectl create namespace $KUBEDB_NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
echo -e "${GREEN}✓ Namespace created${NC}"
echo ""

# Install KubeDB Operator
echo -e "${BLUE}Step 4: Installing KubeDB Operator...${NC}"
helm upgrade --install kubedb-operator appscode/kubedb \
  --version $KUBEDB_VERSION \
  --namespace $KUBEDB_NAMESPACE \
  --set-file global.license=/dev/null \
  --wait \
  --timeout 10m

echo -e "${GREEN}✓ KubeDB Operator installed${NC}"
echo ""

# Install KubeDB Catalog
echo -e "${BLUE}Step 5: Installing KubeDB Catalog...${NC}"
helm upgrade --install kubedb-catalog appscode/kubedb-catalog \
  --version $KUBEDB_VERSION \
  --namespace $KUBEDB_NAMESPACE \
  --wait \
  --timeout 10m

echo -e "${GREEN}✓ KubeDB Catalog installed${NC}"
echo ""

# Verify installation
echo -e "${BLUE}Step 6: Verifying installation...${NC}"
echo ""

echo "Checking KubeDB operator pods:"
kubectl get pods -n $KUBEDB_NAMESPACE

echo ""
echo "Checking available database versions:"
kubectl get mongodbversions --no-headers | head -5
kubectl get postgresversions --no-headers | head -5
kubectl get mysqlversions --no-headers | head -5
kubectl get redisversions --no-headers | head -5

echo ""
echo -e "${GREEN}=========================================="
echo -e "KubeDB Installation Complete! ✓"
echo -e "==========================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Run ./deploy-dbaas.sh to install the DBaaS platform"
echo "  2. Or manually deploy: kubectl apply -f k8s/"
echo ""
