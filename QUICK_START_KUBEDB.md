# Quick Start - KubeDB Integration

## 🚀 Start the Platform (2 minutes)

```bash
# 1. Start services
make docker-up

# 2. Run the app
make dev

# 3. Open API docs
open http://localhost:8000/docs
```

## 🎯 Test the Complete Flow (5 minutes)

### Step 1: Register & Get Token

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@mycompany.com",
    "password": "SecurePass123!",
    "full_name": "Admin User",
    "organization": "My Company"
  }'
```

Save the `access_token` from the response!

### Step 2: Create a PostgreSQL Database

```bash
curl -X POST http://localhost:8000/api/v1/databases \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-first-db",
    "engine": "postgres",
    "version": "15.0",
    "size": "db.t3.small",
    "storage_gb": 20,
    "replicas": 1,
    "backup_enabled": true
  }'
```

### Step 3: List Your Databases

```bash
curl http://localhost:8000/api/v1/databases \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

### Step 4: Get Database Credentials

```bash
curl http://localhost:8000/api/v1/databases/DATABASE_ID/credentials \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

## 🎨 Interactive API Docs

Visit http://localhost:8000/docs for Swagger UI where you can:
- Try all endpoints interactively
- See request/response schemas
- Authenticate with Bearer token
- No need for curl commands!

## 📊 All Available Endpoints

### Authentication
- `POST /api/v1/auth/register` - Create account
- `POST /api/v1/auth/login` - Login
- `POST /api/v1/auth/refresh` - Refresh token
- `POST /api/v1/auth/api-keys` - Create API key
- `GET /api/v1/auth/me` - Get user info

### Databases (KubeDB Integrated!)
- `POST /api/v1/databases` - Create database ✨
- `GET /api/v1/databases` - List databases
- `GET /api/v1/databases/{id}` - Get database
- `DELETE /api/v1/databases/{id}` - Delete database ✨
- `POST /api/v1/databases/{id}/scale` - Scale database
- `GET /api/v1/databases/{id}/credentials` - Get credentials ✨
- `POST /api/v1/databases/{id}/backup` - Backup database
- `POST /api/v1/databases/{id}/restore` - Restore database

### Tenants
- `POST /api/v1/tenants` - Create tenant
- `GET /api/v1/tenants/{id}` - Get tenant
- `PATCH /api/v1/tenants/{id}` - Update tenant
- `GET /api/v1/tenants/{id}/quota` - Get quota

### Health
- `GET /health` - Health check
- `GET /health/ready` - Readiness probe
- `GET /health/live` - Liveness probe

## 🗄️ Supported Databases

- **PostgreSQL** (versions: 12, 13, 14, 15)
- **MySQL** (versions: 5.7, 8.0)
- **MongoDB** (versions: 4.4, 5.0, 6.0)
- **Redis** (versions: 6.2, 7.0)
- **Elasticsearch** (versions: 7.x, 8.x)

## 💰 Instance Sizes

- `db.t3.micro` - 0.5 vCPU, 1GB RAM
- `db.t3.small` - 1 vCPU, 2GB RAM
- `db.t3.medium` - 2 vCPU, 4GB RAM
- `db.t3.large` - 2 vCPU, 8GB RAM
- `db.t3.xlarge` - 4 vCPU, 16GB RAM
- `db.t3.2xlarge` - 8 vCPU, 32GB RAM

## 🔒 Authentication

After registering/logging in, use the access token:

```bash
-H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

Tokens expire after 30 minutes. Use refresh token to get a new one.

## 📝 Example: Create MySQL Database

```bash
curl -X POST http://localhost:8000/api/v1/databases \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-mysql-db",
    "engine": "mysql",
    "version": "8.0",
    "size": "db.t3.medium",
    "storage_gb": 50,
    "replicas": 3,
    "high_availability": true
  }'
```

## 📝 Example: Create MongoDB Database

```bash
curl -X POST http://localhost:8000/api/v1/databases \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-mongodb",
    "engine": "mongodb",
    "version": "6.0",
    "size": "db.t3.large",
    "storage_gb": 100,
    "replicas": 3
  }'
```

## 🐛 Troubleshooting

### MongoDB Connection Failed
```bash
# Check MongoDB is running
docker-compose ps mongodb

# Restart MongoDB
docker-compose restart mongodb
```

### Application Won't Start
```bash
# Check logs
docker-compose logs app

# Restart all services
make docker-down && make docker-up
```

### Authentication Failed
```bash
# Make sure you're using the latest token
# Tokens expire after 30 minutes
# Use /api/v1/auth/refresh to get a new one
```

## 🎓 Learn More

- **Full Documentation**: See `PROJECT_SUMMARY.md`
- **Integration Details**: See `INTEGRATION_SUMMARY.md`
- **Getting Started**: See `GETTING_STARTED.md`
- **API Docs**: http://localhost:8000/docs

## ⚡ Power User Tips

### Save Token to Environment Variable
```bash
export TOKEN=$(curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@company.com","password":"SecurePass123!"}' \
  | jq -r '.access_token')

# Now use $TOKEN in all requests
curl http://localhost:8000/api/v1/databases -H "Authorization: Bearer $TOKEN"
```

### Auto-refresh Token
```bash
export REFRESH_TOKEN="your-refresh-token"

export TOKEN=$(curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Authorization: Bearer $REFRESH_TOKEN" \
  | jq -r '.access_token')
```

### List All Databases with Filtering
```bash
# Only PostgreSQL databases
curl "http://localhost:8000/api/v1/databases?engine=postgres" \
  -H "Authorization: Bearer $TOKEN"

# Only running databases
curl "http://localhost:8000/api/v1/databases?status=running" \
  -H "Authorization: Bearer $TOKEN"

# Pagination
curl "http://localhost:8000/api/v1/databases?page=1&page_size=20" \
  -H "Authorization: Bearer $TOKEN"
```

## 🎉 You're Ready!

Your production-grade DBaaS platform is now running with full KubeDB integration!

Create, manage, and scale databases just like AWS RDS or Google Cloud SQL! 🚀
