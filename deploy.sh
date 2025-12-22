#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
KUBECONFIG_PATH="${KUBECONFIG:-/Users/coredge.io/Downloads/southaz1-21nov-kubeconfig.yaml}"
NAMESPACE="kubedb-dbaas"
DOCKER_IMAGE="${DOCKER_IMAGE:-kubedb-dbaas-api:latest}"
DOCKER_REGISTRY="${DOCKER_REGISTRY:-}"  # Set to your registry URL if using remote registry

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}KubeDB DBaaS Platform Deployment${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check if kubeconfig exists
if [ ! -f "$KUBECONFIG_PATH" ]; then
    echo -e "${RED}Error: Kubeconfig not found at $KUBECONFIG_PATH${NC}"
    exit 1
fi

export KUBECONFIG="$KUBECONFIG_PATH"

# Check kubectl connectivity
echo -e "${YELLOW}[1/6] Checking Kubernetes cluster connectivity...${NC}"
if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}Error: Cannot connect to Kubernetes cluster${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Connected to cluster${NC}"
echo ""

# Build Docker image
echo -e "${YELLOW}[2/6] Building Docker image...${NC}"
docker build -t $DOCKER_IMAGE -f Dockerfile .
echo -e "${GREEN}✓ Docker image built: $DOCKER_IMAGE${NC}"
echo ""

# Load image to kind/minikube if needed (skip for production)
# If using a remote registry, push the image here
# docker push your-registry.com/$DOCKER_IMAGE

# Create namespace
echo -e "${YELLOW}[3/6] Creating namespace...${NC}"
kubectl apply -f k8s/app/namespace.yaml
echo -e "${GREEN}✓ Namespace created/updated${NC}"
echo ""

# Deploy RBAC
echo -e "${YELLOW}[4/6] Deploying RBAC...${NC}"
kubectl apply -f k8s/app/rbac.yaml
echo -e "${GREEN}✓ RBAC configured${NC}"
echo ""

# Deploy MongoDB and Redis using KubeDB
echo -e "${YELLOW}[5/6] Deploying MongoDB and Redis with KubeDB...${NC}"
kubectl apply -f k8s/app/mongodb.yaml
kubectl apply -f k8s/app/redis.yaml
echo -e "${GREEN}✓ Databases deployed${NC}"
echo ""

echo -e "${YELLOW}Waiting for MongoDB to be ready (this may take a few minutes)...${NC}"
kubectl wait --for=condition=Ready mongodb/kubedb-dbaas-mongodb -n $NAMESPACE --timeout=600s || true

echo -e "${YELLOW}Waiting for Redis to be ready...${NC}"
kubectl wait --for=condition=Ready redis/kubedb-dbaas-redis -n $NAMESPACE --timeout=300s || true

# Deploy application
echo -e "${YELLOW}[6/6] Deploying application...${NC}"
kubectl apply -f k8s/app/configmap.yaml
kubectl apply -f k8s/app/secret.yaml
kubectl apply -f k8s/app/deployment.yaml
kubectl apply -f k8s/app/service.yaml
kubectl apply -f k8s/app/hpa.yaml
# kubectl apply -f k8s/app/ingress.yaml  # Uncomment if you have ingress controller
echo -e "${GREEN}✓ Application deployed${NC}"
echo ""

# Wait for deployment
echo -e "${YELLOW}Waiting for application pods to be ready...${NC}"
kubectl wait --for=condition=Available deployment/kubedb-dbaas-api -n $NAMESPACE --timeout=300s

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Show status
echo -e "${YELLOW}Deployment Status:${NC}"
kubectl get all -n $NAMESPACE
echo ""

# Get LoadBalancer IP
echo -e "${YELLOW}Getting LoadBalancer endpoint...${NC}"
kubectl get svc kubedb-dbaas-api-lb -n $NAMESPACE
echo ""

echo -e "${GREEN}To access the API:${NC}"
echo -e "1. Get the LoadBalancer IP:"
echo -e "   ${YELLOW}kubectl get svc kubedb-dbaas-api-lb -n $NAMESPACE${NC}"
echo -e ""
echo -e "2. Access the API docs at:"
echo -e "   ${YELLOW}http://<LOAD_BALANCER_IP>/docs${NC}"
echo -e ""
echo -e "${GREEN}To view logs:${NC}"
echo -e "   ${YELLOW}kubectl logs -f deployment/kubedb-dbaas-api -n $NAMESPACE${NC}"
echo -e ""
echo -e "${GREEN}To view pods:${NC}"
echo -e "   ${YELLOW}kubectl get pods -n $NAMESPACE${NC}"
echo -e ""
