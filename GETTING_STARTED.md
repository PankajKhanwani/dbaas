# Getting Started with KubeDB DBaaS Platform

This guide will help you get the KubeDB DBaaS platform up and running in under 10 minutes.

## Prerequisites

- Python 3.11 or higher
- Docker and Docker Compose
- kubectl (optional, for Kubernetes deployment)
- A Kubernetes cluster with KubeDB installed (optional, for production)

## Step 1: Clone and Setup

```bash
cd kube-db

# Copy environment file
cp .env.example .env

# Edit .env and set your SECRET_KEY (must be at least 32 characters)
# For testing, you can use:
echo "SECRET_KEY=development-secret-key-change-this-in-production-32chars" >> .env
```

## Step 2: Install Dependencies

### Option A: Using Poetry (Recommended)

```bash
# Install Poetry if you don't have it
curl -sSL https://install.python-poetry.org | python3 -

# Install dependencies
make install-dev

# Or manually:
poetry install --with dev
```

### Option B: Using pip

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Step 3: Start Services

```bash
# Start MongoDB, Redis, Prometheus, and Grafana
make docker-up

# Wait for services to be healthy (check with):
docker-compose ps
```

## Step 4: Run the Application

### Development Mode

```bash
# Run with hot reload
make dev

# Or manually:
poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Production Mode

```bash
# Run with multiple workers
make run-prod

# Or using Docker:
docker-compose up app
```

## Step 5: Verify Installation

Open your browser and navigate to:

- **API Documentation**: http://localhost:8000/docs
- **Alternative Docs (ReDoc)**: http://localhost:8000/redoc
- **Health Check**: http://localhost:8000/health

### Test the API

```bash
# Health check
curl http://localhost:8000/health/

# Expected response:
# {
#   "status": "healthy",
#   "version": "1.0.0",
#   "environment": "development",
#   "timestamp": "2024-01-15T10:30:00Z"
# }

# Readiness check
curl http://localhost:8000/health/ready
```

## Step 6: Access Monitoring

### Prometheus
- URL: http://localhost:9090
- Metrics endpoint: http://localhost:8000/metrics

### Grafana
- URL: http://localhost:3001
- Username: `admin`
- Password: `admin`

### MongoDB
```bash
# Connect to MongoDB shell
make db-shell

# Or manually:
docker-compose exec mongodb mongosh kubedb_dbaas -u mongodb -p mongodb
```

### Redis
```bash
# Connect to Redis CLI
make redis-cli

# Or manually:
docker-compose exec redis redis-cli
```

## Step 7: Run Tests

```bash
# Run all tests with coverage
make test

# Run specific test file
poetry run pytest tests/test_health.py -v

# Run with coverage report
poetry run pytest --cov=app --cov-report=html
# Open htmlcov/index.html in browser
```

## Step 8: Code Quality Checks

```bash
# Format code
make format

# Run linters
make lint

# Run all checks (format + lint + test)
make all
```

## Common Commands

```bash
# View all available commands
make help

# Start services
make docker-up

# Stop services
make docker-down

# View logs
make docker-logs

# Restart services
make docker-restart

# Clean up generated files
make clean
```

## Development Workflow

### 1. Create a New Feature

```bash
# Create a new branch
git checkout -b feature/my-new-feature

# Make changes to code
# ...

# Format and lint
make format
make lint

# Run tests
make test

# Commit changes
git add .
git commit -m "Add new feature"
```

### 2. Adding a New API Endpoint

```python
# app/api/v1/my_endpoint.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def my_endpoint():
    return {"message": "Hello World"}
```

```python
# app/main.py
from app.api.v1 import my_endpoint

app.include_router(
    my_endpoint.router,
    prefix="/api/v1/my-endpoint",
    tags=["My Endpoint"]
)
```

### 3. Adding a New Database Model

```python
# app/repositories/models.py
from beanie import Document

class MyModel(Document):
    name: str
    description: str

    class Settings:
        name = "my_collection"
```

```python
# app/config/database.py
# Add to document_models list
from app.repositories.models import MyModel

document_models = [..., MyModel]
```

### 4. Environment Variables

Edit `.env` file:

```bash
# Application
APP_NAME=My DBaaS Platform
ENVIRONMENT=development
DEBUG=true
LOG_LEVEL=INFO

# MongoDB
MONGODB_URL=mongodb://mongodb:mongodb@localhost:27017/kubedb_dbaas
MONGODB_DATABASE=kubedb_dbaas

# Redis
REDIS_URL=redis://localhost:6379/0

# Security
SECRET_KEY=your-secret-key-here-must-be-32-characters-minimum

# Kubernetes (for local testing)
K8S_IN_CLUSTER=false
KUBECONFIG_PATH=/path/to/your/kubeconfig
```

## Deploying to Kubernetes

### 1. Build Docker Image

```bash
# Build image
make docker-build

# Or with no cache
make docker-build-no-cache

# Tag for your registry
docker tag kubedb-dbaas:latest your-registry/kubedb-dbaas:1.0.0

# Push to registry
docker push your-registry/kubedb-dbaas:1.0.0
```

### 2. Install KubeDB

```bash
# Add KubeDB Helm repository
helm repo add appscode https://charts.appscode.com/stable/
helm repo update

# Install KubeDB
helm install kubedb appscode/kubedb \
  --version v2023.12.28 \
  --namespace kubedb \
  --create-namespace
```

### 3. Deploy Application

```bash
# Create namespace
kubectl create namespace kubedb-system

# Create secrets
kubectl create secret generic kubedb-dbaas-secrets \
  --from-literal=mongodb-url=mongodb://user:pass@mongodb:27017/db \
  --from-literal=redis-url=redis://redis:6379/0 \
  --from-literal=secret-key=your-production-secret-key-32-chars \
  -n kubedb-system

# Deploy with Helm
make k8s-deploy

# Or manually:
helm upgrade --install kubedb-dbaas ./k8s/helm/kubedb-dbaas \
  --namespace kubedb-system \
  --values ./k8s/helm/kubedb-dbaas/values.yaml
```

### 4. Verify Deployment

```bash
# Check pods
kubectl get pods -n kubedb-system

# Check services
kubectl get svc -n kubedb-system

# View logs
kubectl logs -f deployment/kubedb-dbaas -n kubedb-system

# Port forward to access locally
kubectl port-forward svc/kubedb-dbaas 8000:8000 -n kubedb-system
```

## Troubleshooting

### MongoDB Connection Issues

```bash
# Check MongoDB is running
docker-compose ps mongodb

# Check logs
docker-compose logs mongodb

# Restart MongoDB
docker-compose restart mongodb
```

### Application Won't Start

```bash
# Check environment variables
cat .env

# Check logs
docker-compose logs app

# Rebuild containers
docker-compose down
docker-compose build --no-cache
docker-compose up
```

### Tests Failing

```bash
# Ensure services are running
make docker-up

# Clear cache
make clean

# Reinstall dependencies
poetry install --with dev

# Run tests with verbose output
poetry run pytest -vv
```

### Port Already in Use

```bash
# Find process using port 8000
lsof -i :8000

# Kill process
kill -9 <PID>

# Or use different port
uvicorn app.main:app --port 8001
```

## Next Steps

1. **Explore the API**: Visit http://localhost:8000/docs
2. **Read the Project Summary**: See `PROJECT_SUMMARY.md`
3. **Check the README**: See `README.md` for detailed documentation
4. **Implement Auth**: Complete the authentication service
5. **Add Database Creation**: Wire up the KubeDB service
6. **Configure Monitoring**: Set up Grafana dashboards
7. **Deploy to Production**: Follow the Kubernetes deployment guide

## Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [KubeDB Documentation](https://kubedb.com/docs/)
- [Beanie ODM Documentation](https://beanie-odm.dev/)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Prometheus Documentation](https://prometheus.io/docs/)

## Support

If you encounter any issues:

1. Check the logs: `make docker-logs`
2. Review environment variables in `.env`
3. Ensure all services are healthy: `docker-compose ps`
4. Check the troubleshooting section above
5. Open an issue on GitHub

---

Happy coding! 🚀
