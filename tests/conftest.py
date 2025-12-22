"""
Pytest configuration and fixtures.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient
from typing import AsyncGenerator

from app.main import app
from app.config.settings import settings


@pytest.fixture(scope="session")
def test_settings():
    """Override settings for testing."""
    settings.environment = "testing"
    settings.debug = True
    return settings


@pytest_asyncio.fixture
async def test_client() -> AsyncGenerator[AsyncClient, None]:
    """Create test client."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def mock_tenant_id():
    """Mock tenant ID for testing."""
    return "tenant-test123"


@pytest.fixture
def mock_user_id():
    """Mock user ID for testing."""
    return "user-test123"


@pytest.fixture
def mock_database_id():
    """Mock database ID for testing."""
    return "db-test123"
