"""
Tests for health check endpoints.
"""
import pytest
from httpx import AsyncClient
from fastapi import status


@pytest.mark.asyncio
async def test_health_check(test_client: AsyncClient):
    """Test basic health check endpoint."""
    response = await test_client.get("/health/")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "healthy"
    assert "version" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_liveness_probe(test_client: AsyncClient):
    """Test Kubernetes liveness probe."""
    response = await test_client.get("/health/live")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "alive"
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_readiness_probe(test_client: AsyncClient):
    """Test Kubernetes readiness probe."""
    response = await test_client.get("/health/ready")
    # May return 503 if database is not connected
    assert response.status_code in [status.HTTP_200_OK, status.HTTP_503_SERVICE_UNAVAILABLE]
    data = response.json()
    assert "status" in data
    assert "database" in data


@pytest.mark.asyncio
async def test_startup_probe(test_client: AsyncClient):
    """Test Kubernetes startup probe."""
    response = await test_client.get("/health/startup")
    # May return 503 during startup
    assert response.status_code in [status.HTTP_200_OK, status.HTTP_503_SERVICE_UNAVAILABLE]
    data = response.json()
    assert "status" in data
    assert "timestamp" in data
