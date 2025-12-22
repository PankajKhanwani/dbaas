.PHONY: help install dev test lint format clean docker-build docker-up docker-down k8s-deploy run-api run-reconciler run-worker run-all stop-all status logs

# Colors for output
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

# Configuration
export KUBECONFIG ?= /Users/coredge.io/Downloads/airtel-k8s-do-not-delete-kubeconfig.yaml
export RECONCILE_INTERVAL ?= 30
PID_DIR := .pids
LOG_DIR := .logs

help: ## Show this help message
	@echo '${BLUE}KubeDB DBaaS Platform - Available Commands${NC}'
	@echo ''
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies using Poetry
	poetry install

install-dev: ## Install dependencies including dev dependencies
	poetry install --with dev
	pre-commit install

dev: ## Run development server with hot reload
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run tests with coverage
	poetry run pytest --cov=app --cov-report=html --cov-report=term

test-watch: ## Run tests in watch mode
	poetry run ptw -- --cov=app

lint: ## Run linters
	poetry run ruff check app/
	poetry run mypy app/

format: ## Format code with black and ruff
	poetry run black app/ tests/
	poetry run ruff --fix app/ tests/

format-check: ## Check code formatting
	poetry run black --check app/ tests/
	poetry run ruff check app/ tests/

clean: ## Clean up generated files
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.coverage" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/

docker-build: ## Build Docker image
	docker build -t kubedb-dbaas:latest .

docker-build-no-cache: ## Build Docker image without cache
	docker build --no-cache -t kubedb-dbaas:latest .

docker-up: ## Start all services with docker-compose
	docker-compose up -d

docker-down: ## Stop all services
	docker-compose down

docker-logs: ## View logs from all services
	docker-compose logs -f

docker-restart: ## Restart all services
	docker-compose restart

db-shell: ## Connect to MongoDB shell
	docker-compose exec mongodb mongosh kubedb_dbaas -u mongodb -p mongodb

redis-cli: ## Connect to Redis CLI
	docker-compose exec redis redis-cli

k8s-deploy: ## Deploy to Kubernetes using Helm
	helm upgrade --install kubedb-dbaas ./k8s/helm/kubedb-dbaas \
		--namespace kubedb-system \
		--create-namespace

k8s-delete: ## Delete Kubernetes deployment
	helm uninstall kubedb-dbaas --namespace kubedb-system

k8s-logs: ## View Kubernetes pod logs
	kubectl logs -f -l app=kubedb-dbaas -n kubedb-system

security-scan: ## Run security vulnerability scan
	poetry run safety check
	poetry run bandit -r app/

pre-commit: ## Run pre-commit hooks on all files
	poetry run pre-commit run --all-files

run-prod: ## Run production server
	poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --loop uvloop --http httptools

all: clean format lint test ## Run all checks

# ============================================================================
# Local Development - Single-Pod Mode (All workers in one process)
# ============================================================================

setup-dirs: ## Create necessary directories for PIDs and logs
	@mkdir -p $(PID_DIR) $(LOG_DIR)

run: setup-dirs ## Run the application (API + all workers in one process)
	@echo "${GREEN}Starting KubeDB DBaaS (single-pod mode)...${NC}"
	@echo "${BLUE}This starts:${NC}"
	@echo "  • API Server (port 8000)"
	@echo "  • Reconciliation Worker (with leader election)"
	@echo "  • 3x Operation Workers"
	@echo "  • Status Sync Worker"
	@echo ""
	@source .venv/bin/activate && \
	export KUBECONFIG=$(KUBECONFIG) && \
	export RECONCILE_INTERVAL=$(RECONCILE_INTERVAL) && \
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 > $(LOG_DIR)/app.log 2>&1 & \
	echo $$! > $(PID_DIR)/app.pid && \
	echo "${GREEN}✓ Application started (PID: $$(cat $(PID_DIR)/app.pid))${NC}" && \
	echo "${BLUE}Logs: $(LOG_DIR)/app.log${NC}" && \
	echo "${BLUE}API: http://localhost:8000${NC}" && \
	echo "${BLUE}Docs: http://localhost:8000/docs${NC}"

run-all: run ## Alias for 'run' (for backward compatibility)

stop: ## Stop the application
	@echo "${YELLOW}Stopping application...${NC}"
	@if [ -f $(PID_DIR)/app.pid ]; then \
		kill -TERM $$(cat $(PID_DIR)/app.pid) 2>/dev/null || true; \
		rm -f $(PID_DIR)/app.pid; \
		echo "${GREEN}✓ Application stopped${NC}"; \
	else \
		echo "${YELLOW}Application is not running${NC}"; \
	fi

stop-all: stop ## Alias for 'stop' (for backward compatibility)

restart: stop run ## Restart the application

restart-all: restart ## Alias for 'restart' (for backward compatibility)

status: ## Check application status
	@echo "${BLUE}Application Status:${NC}"
	@echo ""
	@if [ -f $(PID_DIR)/app.pid ]; then \
		pid=$$(cat $(PID_DIR)/app.pid); \
		if ps -p $$pid > /dev/null 2>&1; then \
			echo "${GREEN}✓ Application${NC}       Running (PID: $$pid)"; \
			echo "  ${BLUE}Components:${NC}"; \
			echo "    • API Server"; \
			echo "    • Reconciliation Worker (leader-elected)"; \
			echo "    • 3x Operation Workers"; \
			echo "    • Status Sync Worker"; \
		else \
			echo "${RED}✗ Application${NC}       Stopped (stale PID file)"; \
			rm -f $(PID_DIR)/app.pid; \
		fi \
	else \
		echo "${RED}✗ Application${NC}       Not running"; \
		echo "  ${YELLOW}Run 'make run' to start${NC}"; \
	fi
	@echo ""

logs: ## Tail application logs
	@echo "${BLUE}Tailing logs (Ctrl+C to stop)...${NC}"
	@tail -f $(LOG_DIR)/app.log 2>/dev/null || echo "${YELLOW}Application log not found${NC}"

logs-api: logs ## Alias for 'logs'
logs-reconciler: logs ## Alias for 'logs'
logs-worker: logs ## Alias for 'logs'

clean-logs: ## Clean up log files
	@rm -rf $(LOG_DIR)/*.log
	@echo "${GREEN}✓ Logs cleaned${NC}"

clean-pids: ## Clean up PID files
	@rm -rf $(PID_DIR)/*.pid
	@echo "${GREEN}✓ PID files cleaned${NC}"

clean-all: clean clean-logs clean-pids ## Clean everything including logs and PIDs

.DEFAULT_GOAL := help
