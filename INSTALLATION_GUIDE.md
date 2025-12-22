# KubeDB DBaaS Platform - Complete Installation Guide

This guide will walk you through installing KubeDB and deploying the DBaaS platform on your Kubernetes cluster.

## Prerequisites

### Required Tools
- **kubectl** (v1.25+) - Kubernetes CLI
- **helm** (v3.0+) - Kubernetes package manager
- **docker** - Container runtime
- **Kubernetes cluster** with:
  - At least 3 worker nodes
  - 8GB RAM per node minimum
  - 50GB storage available
  - LoadBalancer support (for production)

### Access Requirements
- Valid kubeconfig file with cluster-admin permissions
- Access to Docker registry (DockerHub, ECR, GCR, etc.)
- Network connectivity to cluster API server

---

## Installation Steps

### Step 1: Verify Cluster Access

```bash
# Set your kubeconfig
export KUBECONFIG=/path/to/your/kubeconfig.yaml

# Test connectivity
kubectl cluster-info
kubectl get nodes

# Expected output: Control plane running, nodes in Ready state
```

### Step 2: Install KubeDB Operator and Catalog

We've provided an automated script for this:

```bash
# Make the script executable
chmod +x install-kubedb.sh

# Run the installation
./install-kubedb.sh
```

This script will:
1. Add AppsCode Helm repository
2. Create `kubedb` namespace
3. Install KubeDB Operator
4. Install KubeDB Catalog (MongoDB, PostgreSQL, MySQL, Redis, Elasticsearch versions)
5. Verify installation

**Manual Installation (Alternative):**

```bash
# Add Helm repo
helm repo add appscode https://charts.appscode.com/stable/
helm repo update

# Create namespace
kubectl create namespace kubedb

# Install KubeDB operator
helm upgrade --install kubedb-operator appscode/kubedb \
  --version v2024.11.18 \
  --namespace kubedb \
  --set-file global.license=/dev/null \
  --wait

# Install KubeDB catalog
helm upgrade --install kubedb-catalog appscode/kubedb-catalog \
  --version v2024.11.18 \
  --namespace kubedb \
  --wait
```

**Verify KubeDB Installation:**

```bash
# Check operator pods
kubectl get pods -n kubedb

# Check available database versions
kubectl get mongodbversions
kubectl get postgresversions
kubectl get mysqlversions
kubectl get redisversions
```

---

### Step 3: Build and Push Docker Image

You need to push the DBaaS API image to a container registry accessible by your cluster.

**Option A: DockerHub**

```bash
# Build the image
docker build -t your-dockerhub-username/kubedb-dbaas-api:latest .

# Login to DockerHub
docker login

# Push the image
docker push your-dockerhub-username/kubedb-dbaas-api:latest

# Update deployment to use this image
export DOCKER_IMAGE="your-dockerhub-username/kubedb-dbaas-api:latest"
```

**Option B: Private Registry (ECR, GCR, ACR)**

```bash
# Example for AWS ECR
AWS_ACCOUNT_ID="123456789012"
AWS_REGION="us-east-1"
REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Login to ECR
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $REGISTRY

# Build and tag
docker build -t ${REGISTRY}/kubedb-dbaas-api:latest .

# Push
docker push ${REGISTRY}/kubedb-dbaas-api:latest

# Update deployment
export DOCKER_IMAGE="${REGISTRY}/kubedb-dbaas-api:latest"
```

**Update Deployment Manifest:**

```bash
# Edit k8s/app/deployment.yaml and update the image:
sed -i "s|image:.*kubedb-dbaas-api.*|image: ${DOCKER_IMAGE}|g" k8s/app/deployment.yaml
```

---

### Step 4: Configure Environment Variables

Create or update the secret with your configuration:

```bash
# Edit k8s/app/secret.yaml with your values
# Important: Update MongoDB and Redis connection strings after they're deployed
```

---

### Step 5: Deploy the DBaaS Platform

**Automated Deployment:**

```bash
# Make the script executable
chmod +x deploy.sh

# Run deployment
./deploy.sh
```

**Manual Deployment:**

```bash
# Set namespace
export NAMESPACE="kubedb-dbaas"

# 1. Create namespace
kubectl apply -f k8s/app/namespace.yaml

# 2. Deploy RBAC (ServiceAccount, Role, RoleBinding)
kubectl apply -f k8s/app/rbac.yaml

# 3. Deploy MongoDB and Redis using KubeDB
kubectl apply -f k8s/app/mongodb.yaml
kubectl apply -f k8s/app/redis.yaml

# Wait for databases to be ready (this may take 2-5 minutes)
kubectl wait --for=condition=Ready \
  mongodb/kubedb-dbaas-mongodb \
  -n $NAMESPACE --timeout=600s

kubectl wait --for=condition=Ready \
  redis/kubedb-dbaas-redis \
  -n $NAMESPACE --timeout=300s

# 4. Deploy ConfigMap and Secret
kubectl apply -f k8s/app/configmap.yaml
kubectl apply -f k8s/app/secret.yaml

# 5. Deploy application
kubectl apply -f k8s/app/deployment.yaml
kubectl apply -f k8s/app/service.yaml
kubectl apply -f k8s/app/hpa.yaml

# Optional: Deploy ingress if you have ingress controller
kubectl apply -f k8s/app/ingress.yaml

# Wait for application to be ready
kubectl wait --for=condition=Available \
  deployment/kubedb-dbaas-api \
  -n $NAMESPACE --timeout=300s
```

---

### Step 6: Verify Deployment

```bash
# Check all resources
kubectl get all -n kubedb-dbaas

# Check pods
kubectl get pods -n kubedb-dbaas

# Check logs
kubectl logs -f deployment/kubedb-dbaas-api -n kubedb-dbaas

# Check MongoDB
kubectl get mongodb -n kubedb-dbaas

# Check Redis
kubectl get redis -n kubedb-dbaas
```

**Expected Output:**
```
NAME                                    READY   STATUS    RESTARTS   AGE
pod/kubedb-dbaas-api-xxxxxxxxx-xxxxx    1/1     Running   0          2m
pod/kubedb-dbaas-mongodb-0              2/2     Running   0          5m
pod/kubedb-dbaas-mongodb-1              2/2     Running   0          4m
pod/kubedb-dbaas-mongodb-2              2/2     Running   0          3m
pod/kubedb-dbaas-redis-0                1/1     Running   0          5m

NAME                                TYPE           CLUSTER-IP       EXTERNAL-IP
service/kubedb-dbaas-api-lb         LoadBalancer   10.100.200.50    <pending/IP>
service/kubedb-dbaas-mongodb        ClusterIP      10.100.200.51    <none>
service/kubedb-dbaas-redis          ClusterIP      10.100.200.52    <none>
```

---

### Step 7: Access the Application

**Get the LoadBalancer IP:**

```bash
kubectl get svc kubedb-dbaas-api-lb -n kubedb-dbaas

# If using NodePort (for local testing):
kubectl get svc kubedb-dbaas-api -n kubedb-dbaas
```

**Access the API:**

```bash
# Replace with your LoadBalancer IP or NodePort
EXTERNAL_IP=$(kubectl get svc kubedb-dbaas-api-lb -n kubedb-dbaas -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

# API Documentation
echo "API Docs: http://${EXTERNAL_IP}/docs"

# Health check
curl http://${EXTERNAL_IP}/health

# List databases
curl http://${EXTERNAL_IP}/api/v1/domain/demo/project/demo/databases/
```

---

## Frontend Deployment (Optional)

If you want to deploy the React frontend to Kubernetes:

```bash
# Build frontend
cd frontend
npm install
npm run build

# Create Docker image for frontend
cat > Dockerfile.frontend <<EOF
FROM nginx:alpine
COPY dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
EOF

# Build and push
docker build -t your-registry/kubedb-dbaas-frontend:latest -f Dockerfile.frontend .
docker push your-registry/kubedb-dbaas-frontend:latest

# Deploy (create frontend-deployment.yaml)
kubectl apply -f k8s/frontend-deployment.yaml
```

---

## Troubleshooting

### Pods Not Starting

```bash
# Check pod status
kubectl describe pod <pod-name> -n kubedb-dbaas

# Check logs
kubectl logs <pod-name> -n kubedb-dbaas

# Common issues:
# - Image pull errors: Check image name and registry access
# - Resource limits: Check node resources
# - MongoDB/Redis not ready: Wait for databases to initialize
```

### Database Connection Issues

```bash
# Get MongoDB connection string
kubectl get secret kubedb-dbaas-mongodb-auth -n kubedb-dbaas -o jsonpath='{.data.password}' | base64 -d

# Get Redis connection
kubectl get secret kubedb-dbaas-redis-auth -n kubedb-dbaas -o jsonpath='{.data.password}' | base64 -d

# Test connectivity from pod
kubectl exec -it deployment/kubedb-dbaas-api -n kubedb-dbaas -- \
  python3 -c "from pymongo import MongoClient; print(MongoClient('mongodb://kubedb-dbaas-mongodb:27017').admin.command('ping'))"
```

### API Not Accessible

```bash
# Check service
kubectl get svc -n kubedb-dbaas

# Check endpoints
kubectl get endpoints -n kubedb-dbaas

# Port forward for testing
kubectl port-forward svc/kubedb-dbaas-api-lb 8000:80 -n kubedb-dbaas

# Access at http://localhost:8000/docs
```

---

## Scaling

### Scale Application Pods

```bash
# Manual scaling
kubectl scale deployment kubedb-dbaas-api --replicas=5 -n kubedb-dbaas

# HPA is already configured for auto-scaling based on CPU/memory
kubectl get hpa -n kubedb-dbaas
```

### Scale MongoDB

```bash
# Edit MongoDB resource
kubectl edit mongodb kubedb-dbaas-mongodb -n kubedb-dbaas

# Change spec.replicas to desired count
# KubeDB will handle the scaling automatically
```

---

## Monitoring

### View Metrics

```bash
# Prometheus metrics endpoint
curl http://${EXTERNAL_IP}/metrics

# Application logs
kubectl logs -f deployment/kubedb-dbaas-api -n kubedb-dbaas

# MongoDB logs
kubectl logs kubedb-dbaas-mongodb-0 -n kubedb-dbaas -c mongodb

# Redis logs
kubectl logs kubedb-dbaas-redis-0 -n kubedb-dbaas
```

---

## Cleanup

To remove everything:

```bash
# Delete DBaaS application
kubectl delete -f k8s/app/

# Delete KubeDB
helm uninstall kubedb-operator -n kubedb
helm uninstall kubedb-catalog -n kubedb

# Delete namespaces
kubectl delete namespace kubedb-dbaas
kubectl delete namespace kubedb
```

---

## Production Considerations

### Security
- [ ] Use TLS/HTTPS with valid certificates
- [ ] Enable authentication (currently disabled for demo)
- [ ] Use Kubernetes Secrets for sensitive data
- [ ] Enable network policies for pod isolation
- [ ] Use RBAC with minimal permissions

### High Availability
- [ ] Run multiple API replicas (HPA configured)
- [ ] Use MongoDB replica set (3+ nodes)
- [ ] Configure Redis persistence
- [ ] Set up proper backup strategy
- [ ] Configure pod anti-affinity rules

### Monitoring & Observability
- [ ] Deploy Prometheus for metrics collection
- [ ] Set up Grafana dashboards
- [ ] Configure alerting rules
- [ ] Enable structured logging
- [ ] Set up log aggregation (ELK, Loki)

### Performance
- [ ] Configure resource requests/limits properly
- [ ] Enable caching strategies
- [ ] Use connection pooling
- [ ] Optimize database indexes
- [ ] Configure HPA thresholds

---

## Support

For issues or questions:
- GitHub Issues: [Create an issue](https://github.com/your-org/kubedb-dbaas/issues)
- Documentation: See README.md and PROJECT_SUMMARY.md
- Logs: Always check `kubectl logs` for debugging

---

**Installation Complete! 🎉**

Your KubeDB DBaaS platform is now ready to provision and manage databases on Kubernetes!
