"""
Database service.
Handles database lifecycle management using KubeDB.
"""
import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime

from app.config.logging import get_logger
from app.config.settings import settings
from app.exceptions import (
    NotFoundError,
    ConflictError,
    DatabaseOperationError,
    AuthorizationError,
)
from app.models.database import (
    DatabaseCreateRequest,
    DatabaseUpdateRequest,
    DatabaseScaleRequest,
    DatabaseResponse,
    DatabaseListResponse,
    DatabaseCredentials,
    DatabaseStatus,
    DatabaseEngine,
)
from app.repositories.models import Database, Provider
from app.services.kubedb_service import kubedb_service
from app.services.audit_service import audit_service
from app.services.provider_selector import provider_selector, ProviderSelectionStrategy
from app.services.resource_allocation import ResourceAllocationService
from app.core.exceptions import ResourceAllocationError, InvalidProviderError

logger = get_logger(__name__)


class DatabaseService:
    """Service for database operations."""

    async def _get_provider_kubeconfig(self, db: Database) -> tuple[Optional[str], Optional[str]]:
        """
        Get provider_id and kubeconfig_content for a database.

        Args:
            db: Database object

        Returns:
            Tuple of (provider_id, kubeconfig_content)
        """
        if not db.provider_id:
            logger.debug(
                "no_provider_id_using_default_cluster",
                database_id=db.id,
            )
            return None, None

        try:
            provider = await Provider.find_one(Provider.id == db.provider_id)
            if not provider:
                logger.warning(
                    "provider_not_found_using_default_cluster",
                    database_id=db.id,
                    provider_id=db.provider_id,
                )
                return db.provider_id, None

            return db.provider_id, provider.kubeconfig_content

        except Exception as e:
            logger.error(
                "failed_to_fetch_provider_using_default_cluster",
                database_id=db.id,
                provider_id=db.provider_id,
                error=str(e),
            )
            return db.provider_id, None

    def _get_resource_requirements(self, size: str, storage_gb: int) -> tuple[float, float, float]:
        """Calculate CPU, memory, and storage requirements from database size."""
        from app.models.database import DatabaseSize

        size_configs = {
            DatabaseSize.MICRO: {"cpu": 0.5, "memory": 1},      # 500m CPU, 1GB RAM
            DatabaseSize.SMALL: {"cpu": 1.0, "memory": 2},      # 1 core, 2GB RAM
            DatabaseSize.MEDIUM: {"cpu": 2.0, "memory": 4},     # 2 cores, 4GB RAM
            DatabaseSize.LARGE: {"cpu": 2.0, "memory": 8},      # 2 cores, 8GB RAM
            DatabaseSize.XLARGE: {"cpu": 4.0, "memory": 16},    # 4 cores, 16GB RAM
            DatabaseSize.XXLARGE: {"cpu": 8.0, "memory": 32},   # 8 cores, 32GB RAM
        }

        config = size_configs.get(size, size_configs[DatabaseSize.SMALL])
        return config["cpu"], config["memory"], float(storage_gb)

    async def create_database(
        self,
        db_request: DatabaseCreateRequest,
        domain: str,
        project: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> DatabaseResponse:
        """
        Create a new managed database instance - PRE-CHECKS ONLY.
        
        This method performs only validation and pre-checks, then returns immediately.
        All heavy operations (resource allocation, KubeDB creation) are handled
        asynchronously by the reconciler.

        Args:
            db_request: Database creation request
            domain: Domain name
            project: Project name
            headers: Request headers containing region/AZ for provider selection

        Returns:
            Created database details (status will be PENDING)

        Raises:
            ConflictError: If database name already exists
            ResourceAllocationError: If no provider has sufficient resources
        """
        # PRE-CHECK 1: Validate database name doesn't exist
        existing = await Database.find_one(
            Database.name == db_request.name,
            Database.domain == domain,
            Database.project == project,
        )

        if existing:
            raise ConflictError(
                f"Database '{db_request.name}' already exists for this domain/project"
            )

        # PRE-CHECK 2: Calculate resource requirements
        cpu_cores, memory_gb, storage_gb = self._get_resource_requirements(
            db_request.size, db_request.storage_gb
        )

        # PRE-CHECK 3: Select provider (validates resource availability)
        headers = headers or {}
        selected_provider = await provider_selector.select_provider(
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            storage_gb=storage_gb,
            headers=headers,
        )

        if not selected_provider:
            raise ResourceAllocationError(
                f"No provider available with sufficient resources: "
                f"{cpu_cores} CPU cores, {memory_gb}GB memory, {storage_gb}GB storage. "
                f"Region: {headers.get('x-region', 'any')}, AZ: {headers.get('x-availability-zone', 'any')}"
            )

        # PRE-CHECK 4: Generate Kubernetes-compliant resource name
        kubedb_name = f"{db_request.name}-{domain}-{project}".lower()
        kubedb_name = kubedb_name[:63].rstrip('-')

        # Create database document (minimal save - status is PENDING)
        db = Database(
            name=db_request.name,
            domain=domain,
            project=project,
            engine=db_request.engine,
            version=db_request.version,
            size=db_request.size,
            storage_gb=db_request.storage_gb,
            replicas=db_request.replicas,
            status=DatabaseStatus.PENDING,  # Will be updated by reconciler
            backup_enabled=db_request.backup_enabled,
            backup_schedule=db_request.backup_schedule,
            backup_retention_days=db_request.backup_retention_days,
            high_availability=db_request.high_availability,
            monitoring_enabled=db_request.monitoring_enabled,
            labels=db_request.labels or {},
            annotations=db_request.annotations or {},
            namespace=settings.k8s_namespace,
            kubedb_resource_name=kubedb_name,
            # Multi-provider fields
            provider_id=selected_provider.id,
            allocated_cpu_cores=cpu_cores,
            allocated_memory_gb=memory_gb,
            allocated_storage_gb=storage_gb,
        )

        await db.save()

        logger.info(
            "database_created_pending",
            database_id=db.id,
            name=db.name,
            domain=domain,
            project=project,
            provider_id=selected_provider.id,
            message="Database created. Reconciler will handle provisioning."
        )

        # Audit log (async, non-blocking)
        asyncio.create_task(
            audit_service.log_action(
                action="database.create",
                resource_type="database",
                resource_id=db.id,
                domain=domain,
                project=project,
                details={"name": db.name, "engine": db.engine.value},
            )
        )

        # Start async handler for resource allocation and KubeDB creation
        # This runs in background - reconciler will sync status
        asyncio.create_task(
            self._handle_database_creation(
                db=db,
                username=db_request.username,
                password=db_request.password
            )
        )

        return self._to_response(db)
    
    async def _handle_database_creation(
        self,
        db: Database,
        username: Optional[str] = None,
        password: Optional[str] = None
    ) -> None:
        """
        Handle database creation asynchronously.
        
        This method:
        1. Allocates resources from provider
        2. Creates KubeDB resource
        3. Updates database status
        
        The reconciler will then sync the status periodically.
        """
        try:
            # Step 1: Allocate resources
            await ResourceAllocationService.allocate_resources(
                provider_id=db.provider_id,
                cpu_cores=db.allocated_cpu_cores or 0,
                memory_gb=db.allocated_memory_gb or 0,
                storage_gb=db.allocated_storage_gb or 0,
                database_id=db.id,
            )

            logger.info(
                "resources_allocated",
                database_id=db.id,
                provider_id=db.provider_id,
            )

            # Step 2: Update status to PROVISIONING
            db.status = DatabaseStatus.PROVISIONING
            await db.save()

            # Step 3: Get provider kubeconfig
            provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

            # Step 4: Create KubeDB resource
            await kubedb_service.create_database(
                engine=db.engine,
                name=db.kubedb_resource_name,
                version=db.version,
                size=db.size,
                storage_gb=db.storage_gb,
                replicas=db.replicas,
                high_availability=db.high_availability,
                namespace=db.namespace,
                labels={
                    **db.labels,
                    "app.kubernetes.io/managed-by": "kubedb-dbaas",
                    "domain": db.domain,
                    "project": db.project,
                    "database-id": db.id,
                },
                annotations=db.annotations,
                username=username,
                password=password,
                monitoring_enabled=db.monitoring_enabled,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )

            logger.info(
                "kubedb_resource_created",
                database_id=db.id,
                resource_name=db.kubedb_resource_name,
                message="Reconciler will sync status and endpoint"
            )

            # Status will be updated by the reconciler
            # No need to monitor here - reconciler handles it

        except ResourceAllocationError as e:
            # Rollback database creation if allocation fails
            await db.delete()
            logger.error(
                "resource_allocation_failed_rollback",
                database_id=db.id,
                provider_id=db.provider_id,
                error=str(e),
            )
        except Exception as e:
            logger.error(
                "database_creation_handler_failed",
                database_id=db.id,
                error=str(e),
                exc_info=True
            )
            db.status = DatabaseStatus.FAILED
            await db.save()

    async def _create_kubedb_resource(self, db: Database, username: Optional[str] = None, password: Optional[str] = None) -> None:
        """Create KubeDB resource in background."""
        try:
            db.status = DatabaseStatus.PROVISIONING
            await db.save()

            # Get provider kubeconfig for multi-cluster support
            provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

            # Create KubeDB resource
            result = await kubedb_service.create_database(
                engine=db.engine,
                name=db.kubedb_resource_name,
                version=db.version,
                size=db.size,
                storage_gb=db.storage_gb,
                replicas=db.replicas,
                high_availability=db.high_availability,
                namespace=db.namespace,
                labels={
                    **db.labels,
                    "app.kubernetes.io/managed-by": "kubedb-dbaas",
                    "domain": db.domain,
                    "project": db.project,
                    "database-id": db.id,
                },
                annotations=db.annotations,
                username=username,
                password=password,
                monitoring_enabled=db.monitoring_enabled,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )

            logger.info(
                "kubedb_resource_created",
                database_id=db.id,
                resource_name=db.kubedb_resource_name,
            )

            # Start status monitoring to fetch real-time status from KubeDB
            # Status will be updated by monitoring task based on actual KubeDB resource
            asyncio.create_task(self._monitor_database_status(db))

        except Exception as e:
            logger.error(
                "kubedb_resource_creation_failed",
                database_id=db.id,
                error=str(e),
            )
            db.status = DatabaseStatus.FAILED
            await db.save()

    async def _monitor_database_status(self, db: Database) -> None:
        """
        Monitor database status and update endpoint with real-time KubeDB data.

        Polls KubeDB resource for up to 10 minutes, checking every 10 seconds.
        Stops when database is ready or fails.
        """
        max_attempts = 60  # 10 minutes with 10-second intervals
        attempt = 0

        # Get provider kubeconfig for multi-cluster support
        provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

        logger.info(
            "starting_database_monitoring",
            database_id=db.id,
            name=db.kubedb_resource_name,
        )

        while attempt < max_attempts:
            try:
                print("Checking detailed status")
                # Sync detailed status from KubeDB
                detailed_status = await kubedb_service.get_detailed_status(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                print("checked detailed status")
                if not detailed_status:
                    # Resource not found yet, keep waiting
                    await asyncio.sleep(10)
                    attempt += 1
                    continue

                phase = detailed_status.get("phase", "Unknown")
                is_ready = detailed_status.get("ready", False)
                ready_replicas = detailed_status.get("ready_replicas", 0)

                # Update database with real-time status
                db.health_status = phase
                db.ready_replicas = ready_replicas
                db.status = self._map_kubedb_phase_to_status(phase, is_ready)

                logger.info(
                    "database_status_update",
                    database_id=db.id,
                    phase=phase,
                    is_ready=is_ready,
                    status=db.status,
                    replicas=f"{ready_replicas}/{db.replicas}",
                    attempt=attempt + 1,
                )

                # If ready, fetch endpoint and finish monitoring
                if is_ready:
                    endpoint_info = await kubedb_service.get_database_endpoint(
                        engine=db.engine,
                        name=db.kubedb_resource_name,
                        namespace=db.namespace,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )

                    if endpoint_info:
                        db.endpoint = endpoint_info.get("host")
                        db.port = endpoint_info.get("port")

                    await db.save()

                    logger.info(
                        "database_ready",
                        database_id=db.id,
                        endpoint=db.endpoint,
                        port=db.port,
                        total_time_seconds=(attempt + 1) * 10,
                    )
                    break

                # If failed, stop monitoring
                if phase.lower() == "failed":
                    await db.save()
                    logger.error(
                        "database_provisioning_failed",
                        database_id=db.id,
                        phase=phase,
                    )
                    break

                # Save status update
                await db.save()

                # Wait before next check
                await asyncio.sleep(10)
                attempt += 1

            except Exception as e:
                logger.error(
                    "status_monitoring_error",
                    database_id=db.id,
                    error=str(e),
                    attempt=attempt + 1,
                )
                await asyncio.sleep(10)
                attempt += 1

        if attempt >= max_attempts:
            logger.warning(
                "database_monitoring_timeout",
                database_id=db.id,
                max_attempts=max_attempts,
            )

    async def list_databases(
        self,
        domain: str,
        project: str,
        page: int = 1,
        page_size: int = 10,
        engine: Optional[str] = None,
        status: Optional[str] = None,
    ) -> DatabaseListResponse:
        """
        List databases for domain/project.

        Args:
            domain: Domain name
            project: Project name
            page: Page number
            page_size: Page size
            engine: Filter by engine
            status: Filter by status

        Returns:
            List of databases
        """
        # Build query - exclude deleted databases
        filters = [
            Database.domain == domain,
            Database.project == project,
            Database.status != DatabaseStatus.DELETED
        ]

        if engine:
            filters.append(Database.engine == DatabaseEngine(engine))

        if status:
            filters.append(Database.status == DatabaseStatus(status))

        # Get total count
        total = await Database.find(*filters).count()

        # Get paginated results
        skip = (page - 1) * page_size
        databases = await Database.find(*filters).skip(skip).limit(page_size).to_list()

        return DatabaseListResponse(
            databases=[self._to_response(db) for db in databases],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_database(self, database_id: str, domain: str, project: str) -> DatabaseResponse:
        """
        Get database by ID.

        Args:
            database_id: Database ID
            domain: Domain name
            project: Project name

        Returns:
            Database details

        Raises:
            NotFoundError: If database not found
            AuthorizationError: If database doesn't belong to domain/project
        """
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        # Check access
        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # Update status from KubeDB
        await self._sync_database_status(db)

        return self._to_response(db)

    async def delete_database(self, database_id: str, domain: str, project: str) -> bool:
        """
        Delete database.

        Args:
            database_id: Database ID
            domain: Domain name
            project: Project name

        Returns:
            True if deleted

        Raises:
            NotFoundError: If database not found
            AuthorizationError: If database doesn't belong to domain/project
        """
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # Update status
        db.status = DatabaseStatus.DELETING
        await db.save()

        # Audit log
        await audit_service.log_action(
            action="database.delete",
            resource_type="database",
            resource_id=db.id,
            domain=domain,
            project=project,
            details={"name": db.name},
        )

        # Reconciler will handle the actual deletion
        logger.info(
            "database_deletion_queued",
            database_id=db.id,
            name=db.name,
            message="Reconciler will perform KubeDB deletion"
        )

        return True

    async def update_database(
        self,
        database_id: str,
        update_data: DatabaseUpdateRequest,
        domain: str,
        project: str,
    ) -> DatabaseResponse:
        """
        Update database configuration.

        Supports updating backup settings, monitoring, and scaling parameters.
        """
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # Track what's being updated
        updates = {}
        needs_kubedb_patch = False

        # Update backup settings (metadata only)
        if update_data.backup_enabled is not None:
            db.backup_enabled = update_data.backup_enabled
            updates["backup_enabled"] = update_data.backup_enabled

        if update_data.backup_schedule is not None:
            db.backup_schedule = update_data.backup_schedule
            updates["backup_schedule"] = update_data.backup_schedule

        # Update monitoring (metadata only)
        if update_data.monitoring_enabled is not None:
            db.monitoring_enabled = update_data.monitoring_enabled
            updates["monitoring_enabled"] = update_data.monitoring_enabled

        # Update labels and annotations
        if update_data.labels is not None:
            db.labels.update(update_data.labels)
            updates["labels"] = update_data.labels

        if update_data.annotations is not None:
            db.annotations.update(update_data.annotations)
            updates["annotations"] = update_data.annotations

        # PRE-CHECKS ONLY - Reconciler will handle the actual operations

        # Check for invalid updates
        if update_data.replicas is not None:
            if update_data.replicas < 1:
                raise DatabaseOperationError(
                    operation="update",
                    database_id=database_id,
                    reason="Replicas must be >= 1"
                )
            db.replicas = update_data.replicas
            updates["replicas"] = update_data.replicas

        if update_data.storage_gb is not None:
            # Can only increase storage
            if update_data.storage_gb < db.storage_gb:
                raise DatabaseOperationError(
                    operation="update",
                    database_id=database_id,
                    reason="Cannot decrease storage size"
                )
            db.storage_gb = update_data.storage_gb
            updates["storage_gb"] = update_data.storage_gb

        if update_data.size is not None:
            db.size = update_data.size
            updates["size"] = update_data.size

        # Update desired state - reconciler will detect the change
        await db.save()

        # Audit log
        await audit_service.log_action(
            action="database.update",
            resource_type="database",
            resource_id=db.id,
            domain=domain,
            project=project,
            details=updates,
        )

        logger.info(
            "database_desired_state_updated",
            database_id=db.id,
            updates=updates,
            message="Reconciler will detect and apply changes"
        )

        return self._to_response(db)

    async def scale_database(
        self,
        database_id: str,
        scale_request: DatabaseScaleRequest,
        domain: str,
        project: str,
    ) -> DatabaseResponse:
        """Scale database resources - updates desired state only."""
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # PRE-CHECKS ONLY - Reconciler will handle the actual operations
        updates = {}

        if scale_request.size:
            db.size = scale_request.size
            updates["size"] = scale_request.size.value

        if scale_request.replicas:
            if scale_request.replicas < 1:
                raise DatabaseOperationError(
                    operation="scale",
                    database_id=database_id,
                    reason="Replicas must be >= 1"
                )
            db.replicas = scale_request.replicas
            updates["replicas"] = scale_request.replicas

        if scale_request.storage_gb:
            if scale_request.storage_gb < db.storage_gb:
                raise DatabaseOperationError(
                    operation="scale",
                    database_id=database_id,
                    reason="Cannot decrease storage size"
                )
            db.storage_gb = scale_request.storage_gb
            updates["storage_gb"] = scale_request.storage_gb

        if not updates:
            raise DatabaseOperationError(
                operation="scale",
                database_id=database_id,
                reason="No scaling parameters provided"
            )

        # Update desired state - reconciler will detect and apply
        await db.save()

        # Audit log
        await audit_service.log_action(
            action="database.scale",
            resource_type="database",
            resource_id=db.id,
            domain=domain,
            project=project,
            details=scale_request.dict(exclude_none=True),
        )

        logger.info(
            "database_desired_state_updated",
            database_id=db.id,
            updates=updates,
            message="Reconciler will detect and apply changes"
        )

        return self._to_response(db)

    async def pause_database(
        self,
        database_id: str,
        domain: str,
        project: str,
    ) -> DatabaseResponse:
        """
        Pause a database instance.

        Paused databases stop consuming compute resources but retain all data.
        """
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        if db.status == DatabaseStatus.PAUSED:
            logger.info("database_already_paused", database_id=db.id)
            return self._to_response(db)

        # Update database status to PAUSING (transient state)
        previous_status = db.status
        db.status = DatabaseStatus.PAUSING
        await db.save()

        # Audit log
        await audit_service.log_action(
            action="database.pause",
            resource_type="database",
            resource_id=db.id,
            domain=domain,
            project=project,
            details={"previous_status": previous_status.value},
        )

        # Reconciler will handle the actual pause
        logger.info(
            "database_pause_queued",
            database_id=db.id,
            message="Reconciler will perform KubeDB pause. Status set to PAUSING."
        )

        return self._to_response(db)

    async def resume_database(
        self,
        database_id: str,
        domain: str,
        project: str,
    ) -> DatabaseResponse:
        """
        Resume a paused database instance.

        The database will be started and become available within a few minutes.
        """
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        if db.status != DatabaseStatus.PAUSED:
            logger.info("database_not_paused", database_id=db.id, status=db.status)
            return self._to_response(db)

        # Update database status to RESUMING (transient state)
        db.status = DatabaseStatus.RESUMING
        await db.save()

        # Audit log
        await audit_service.log_action(
            action="database.resume",
            resource_type="database",
            resource_id=db.id,
            domain=domain,
            project=project,
            details={},
        )

        # Reconciler will handle the actual resume
        logger.info(
            "database_resume_queued",
            database_id=db.id,
            message="Reconciler will perform KubeDB resume. Status set to RESUMING."
        )

        return self._to_response(db)

    async def get_database_status_realtime(
        self, database_id: str, domain: str, project: str
    ) -> Dict[str, Any]:
        """
        Get real-time database status directly from KubeDB.

        Args:
            database_id: Database ID
            domain: Domain name
            project: Project name

        Returns:
            Real-time status information from KubeDB

        Raises:
            NotFoundError: If database not found
            AuthorizationError: If database doesn't belong to domain/project
        """
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # Get provider kubeconfig for multi-cluster support
        provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

        # Fetch detailed status from KubeDB
        detailed_status = await kubedb_service.get_detailed_status(
            engine=db.engine,
            name=db.kubedb_resource_name,
            namespace=db.namespace,
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )

        if not detailed_status:
            return {
                "database_id": db.id,
                "name": db.name,
                "kubedb_phase": None,
                "is_ready": False,
                "application_status": db.status,
                "health_status": db.health_status,
                "endpoint": db.endpoint,
                "port": db.port,
                "message": "KubeDB resource not found or not yet created",
            }

        phase = detailed_status.get("phase", "Unknown")
        is_ready = detailed_status.get("ready", False)
        conditions = detailed_status.get("conditions", [])
        ready_replicas = detailed_status.get("ready_replicas", 0)
        desired_replicas = detailed_status.get("replicas", db.replicas)

        # Update database record with latest status
        db.health_status = phase
        db.ready_replicas = ready_replicas
        db.status = self._map_kubedb_phase_to_status(phase, is_ready)
        await db.save()

        return {
            "database_id": db.id,
            "name": db.name,
            "kubedb_phase": phase,
            "is_ready": is_ready,
            "application_status": db.status,
            "health_status": db.health_status,
            "endpoint": db.endpoint,
            "port": db.port,
            "replicas": {
                "desired": desired_replicas,
                "ready": ready_replicas,
                "status": f"{ready_replicas}/{desired_replicas}",
            },
            "conditions": conditions,
            "observed_generation": detailed_status.get("observedGeneration"),
            "last_updated": datetime.utcnow().isoformat(),
        }

    async def get_credentials(self, database_id: str, domain: str, project: str) -> DatabaseCredentials:
        """Get database credentials from Kubernetes secret."""
        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # Get provider kubeconfig for multi-cluster support
        provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

        # Get the correct Kubernetes client for this database's provider
        client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)

        try:
            secret_name = f"{db.kubedb_resource_name}-auth"
            secret = await client_set.core_api.read_namespaced_secret(
                name=secret_name,
                namespace=db.namespace,
            )

            import base64

            username = base64.b64decode(secret.data.get("username", "")).decode("utf-8")
            password = base64.b64decode(secret.data.get("password", "")).decode("utf-8")

            # Default ports by engine
            port_map = {
                DatabaseEngine.POSTGRES: 5432,
                DatabaseEngine.MYSQL: 3306,
                DatabaseEngine.MONGODB: 27017,
                DatabaseEngine.REDIS: 6379,
                DatabaseEngine.ELASTICSEARCH: 9200,
            }

            port = db.port or port_map.get(db.engine, 5432)
            # Use IP endpoint if available, otherwise fall back to internal DNS
            # (DNS fallback is for when database is still provisioning)
            host = db.endpoint or f"{db.kubedb_resource_name}.{db.namespace}.svc.cluster.local"

            # Build connection string
            if db.engine == DatabaseEngine.POSTGRES:
                conn_str = f"postgresql://{username}:{password}@{host}:{port}/postgres"
            elif db.engine == DatabaseEngine.MYSQL:
                conn_str = f"mysql://{username}:{password}@{host}:{port}/mysql"
            elif db.engine == DatabaseEngine.MONGODB:
                conn_str = f"mongodb://{username}:{password}@{host}:{port}"
            elif db.engine == DatabaseEngine.REDIS:
                conn_str = f"redis://:{password}@{host}:{port}/0"
            else:
                conn_str = f"{db.engine.value}://{username}:{password}@{host}:{port}"

            return DatabaseCredentials(
                username=username,
                password=password,
                host=host,
                port=port,
                database=db.name,
                connection_string=conn_str,
            )

        except Exception as e:
            logger.error("failed_to_get_credentials", database_id=db.id, error=str(e))
            raise DatabaseOperationError(
                operation="get_credentials",
                database_id=database_id,
                reason=f"Failed to retrieve credentials: {str(e)}"
            )

    async def get_metrics(
        self,
        database_id: str,
        domain: str,
        project: str,
        user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get database metrics with production-grade caching.

        Architecture:
        User → Database Service → MetricsCacheService → KubeDB Stats Service

        Features:
        - Multi-layer caching (Memory 5s → Redis 15s → KubeDB Stats)
        - Rate limiting (100 req/min per user, 1000 req/min per database)
        - Request coalescing (prevents thundering herd)
        - Circuit breaker (fail fast when backend is down)
        - Graceful degradation (returns stale data on failures)

        Note: Uses KubeDB's built-in stats service (no Prometheus required).
        For Prometheus-based monitoring, use PrometheusService instead.

        Args:
            database_id: Database ID
            domain: Domain name
            project: Project name
            user_id: Optional user ID for rate limiting

        Returns:
            Metrics dictionary with caching metadata
        """
        from app.services.metrics_cache_service import metrics_cache_service

        db = await Database.find_one(Database.id == database_id)

        if not db:
            raise NotFoundError("Database", database_id)

        if db.domain != domain or db.project != project:
            raise AuthorizationError("Access denied to this database")

        # Check if monitoring is enabled
        if not db.monitoring_enabled:
            raise DatabaseOperationError(
                operation="get_metrics",
                database_id=database_id,
                reason="Monitoring is not enabled for this database"
            )

        # Check if database is ready
        if db.status != DatabaseStatus.RUNNING:
            logger.info(
                "metrics_unavailable_database_not_ready",
                database_id=db.id,
                status=db.status.value,
            )
            return {
                "message": f"Metrics unavailable. Database status: {db.status.value}",
                "status": db.status.value,
            }

        # Get provider kubeconfig for multi-cluster support
        provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

        # Use production-grade caching service
        # This handles: rate limiting, caching, circuit breaker, request coalescing
        try:
            # Use KubeDB stats service (no Prometheus required)
            # This fetches metrics directly from the database's monitoring endpoint
            metrics = await metrics_cache_service.get_metrics(
                database_id=database_id,
                user_id=user_id,
                fetch_fn=lambda: kubedb_service.get_database_metrics(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                ),
                use_aggregated=True
            )

            # Check if metrics fetch returned None
            if metrics is None:
                return {
                    "message": "Monitoring data is currently unavailable for this database",
                    "reason": "The monitoring service is not accessible",
                    "suggestion": "Ensure monitoring is enabled and the database stats service is running",
                    "status": "unavailable"
                }

            # Check if rate limited
            if isinstance(metrics, dict) and "error" in metrics and "rate limit" in metrics.get("error", "").lower():
                logger.warning(
                    "metrics_rate_limited",
                    database_id=db.id,
                    user_id=user_id
                )
                return metrics

            # Add database context
            if isinstance(metrics, dict) and "database_name" not in metrics:
                metrics["database_id"] = db.id
                metrics["database_name"] = db.name
                metrics["engine"] = db.engine

            logger.info(
                "metrics_fetched_with_cache",
                database_id=db.id,
                name=db.name,
                stale=metrics.get("stale", False) if isinstance(metrics, dict) else False
            )

            return metrics

        except Exception as e:
            logger.error(
                "metrics_fetch_failed",
                database_id=db.id,
                name=db.name,
                error=str(e)
            )

            # Return user-friendly message
            return {
                "message": "Monitoring data is currently unavailable for this database",
                "reason": "The monitoring system is still initializing or temporarily unavailable",
                "suggestion": "Please wait a few moments and try again. If the issue persists, contact support.",
                "status": "unavailable",
                "error": str(e)
            }

    async def _monitor_scaling_operation(self, db: Database) -> None:
        """
        Monitor database scaling operation.

        Polls KubeDB resource to check if scaling completed successfully.
        """
        max_attempts = 30  # 5 minutes with 10-second intervals
        attempt = 0

        # Get provider kubeconfig for multi-cluster support
        provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

        logger.info(
            "starting_scaling_monitoring",
            database_id=db.id,
            name=db.kubedb_resource_name,
        )

        while attempt < max_attempts:
            try:
                await asyncio.sleep(10)

                # Get detailed status from KubeDB
                detailed_status = await kubedb_service.get_detailed_status(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )

                if detailed_status:
                    phase = detailed_status.get("phase", "Unknown")
                    is_ready = detailed_status.get("ready", False)
                    ready_replicas = detailed_status.get("ready_replicas", 0)

                    # Update status
                    db.health_status = phase
                    db.ready_replicas = ready_replicas
                    db.status = self._map_kubedb_phase_to_status(phase, is_ready)

                    logger.info(
                        "scaling_status_check",
                        database_id=db.id,
                        phase=phase,
                        is_ready=is_ready,
                        replicas=f"{ready_replicas}/{db.replicas}",
                        attempt=attempt + 1,
                    )

                    # If ready, scaling is complete
                    if is_ready:
                        db.status = DatabaseStatus.RUNNING
                        await db.save()

                        logger.info(
                            "scaling_completed",
                            database_id=db.id,
                            total_time_seconds=(attempt + 1) * 10,
                        )
                        break

                    # If failed, stop monitoring
                    if phase.lower() == "failed":
                        db.status = DatabaseStatus.FAILED
                        await db.save()
                        logger.error("scaling_failed", database_id=db.id, phase=phase)
                        break

                    await db.save()

                attempt += 1

            except Exception as e:
                logger.error(
                    "scaling_monitoring_error",
                    database_id=db.id,
                    error=str(e),
                    attempt=attempt + 1,
                )
                attempt += 1

        if attempt >= max_attempts:
            logger.warning(
                "scaling_monitoring_timeout",
                database_id=db.id,
                max_attempts=max_attempts,
            )
            # Set back to running if timeout (scaling might still succeed)
            db.status = DatabaseStatus.RUNNING
            await db.save()

    async def _sync_database_status(self, db: Database) -> None:
        """
        Sync database status from KubeDB with detailed information.

        Fetches real-time status from Kubernetes and updates both
        health_status and application status.
        """
        try:
            # Handle legacy databases without kubedb_resource_name
            # Reconstruct it using the standard naming pattern
            if not db.kubedb_resource_name:
                db.kubedb_resource_name = f"{db.name}-{db.domain}-{db.project}"
                logger.info(
                    "reconstructed_kubedb_resource_name",
                    database_id=db.id,
                    kubedb_resource_name=db.kubedb_resource_name
                )

            # Get provider kubeconfig for multi-cluster support
            provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

            # Get detailed status from KubeDB
            detailed_status = await kubedb_service.get_detailed_status(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )

            if detailed_status:
                phase = detailed_status.get("phase", "Unknown")
                is_ready = detailed_status.get("ready", False)
                ready_replicas = detailed_status.get("ready_replicas", 0)

                # Update health status with KubeDB phase
                db.health_status = phase

                # Update replica counts
                db.ready_replicas = ready_replicas

                # Map KubeDB phase to application status
                db.status = self._map_kubedb_phase_to_status(phase, is_ready)

                # If database is ready, fetch and update endpoint (always refresh to get latest IP)
                if is_ready:
                    endpoint_info = await kubedb_service.get_database_endpoint(
                        engine=db.engine,
                        name=db.kubedb_resource_name,
                        namespace=db.namespace,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    if endpoint_info:
                        db.endpoint = endpoint_info.get("host")
                        db.port = endpoint_info.get("port")

                await db.save()

                logger.info(
                    "database_status_synced",
                    database_id=db.id,
                    health_status=db.health_status,
                    status=db.status,
                    ready=is_ready,
                )

        except Exception as e:
            logger.warning("status_sync_failed", database_id=db.id, error=str(e))

    def _map_kubedb_phase_to_status(self, phase: str, is_ready: bool) -> DatabaseStatus:
        """
        Map KubeDB phase to application DatabaseStatus.

        Args:
            phase: KubeDB resource phase
            is_ready: Whether database is ready

        Returns:
            Mapped DatabaseStatus
        """
        phase_lower = phase.lower()

        # KubeDB phases: Provisioning, Ready, Running, Halted, Failed, Unknown
        if phase_lower in ["ready", "running"] and is_ready:
            return DatabaseStatus.RUNNING
        elif phase_lower == "provisioning":
            return DatabaseStatus.PROVISIONING
        elif phase_lower == "failed":
            return DatabaseStatus.FAILED
        elif phase_lower == "halted":
            return DatabaseStatus.PAUSED
        elif phase_lower in ["pending", "unknown"]:
            return DatabaseStatus.PENDING
        else:
            # Default: keep current status or set to updating
            return DatabaseStatus.UPDATING

    def _get_size_resources(self, size: str) -> Dict[str, str]:
        """Get resource specs for database size."""
        from app.models.database import DatabaseSize

        size_configs = {
            DatabaseSize.MICRO: {"cpu": "500m", "memory": "1Gi"},
            DatabaseSize.SMALL: {"cpu": "1000m", "memory": "2Gi"},
            DatabaseSize.MEDIUM: {"cpu": "2000m", "memory": "4Gi"},
            DatabaseSize.LARGE: {"cpu": "2000m", "memory": "8Gi"},
            DatabaseSize.XLARGE: {"cpu": "4000m", "memory": "16Gi"},
            DatabaseSize.XXLARGE: {"cpu": "8000m", "memory": "32Gi"},
        }
        return size_configs.get(size, size_configs[DatabaseSize.SMALL])

    def _to_response(self, db: Database) -> DatabaseResponse:
        """Convert database document to response model."""
        return DatabaseResponse(
            id=db.id,
            name=db.name,
            domain=db.domain,
            project=db.project,
            engine=db.engine,
            version=db.version,
            size=db.size,
            storage_gb=db.storage_gb,
            replicas=db.replicas,
            ready_replicas=db.ready_replicas,
            status=db.status,
            backup_enabled=db.backup_enabled,
            backup_schedule=db.backup_schedule,
            high_availability=db.high_availability,
            monitoring_enabled=db.monitoring_enabled,
            endpoint=db.endpoint,
            port=db.port,
            labels=db.labels,
            annotations=db.annotations,
            created_at=db.created_at,
            updated_at=db.updated_at,
            health_status=db.health_status,
        )

    async def refresh_database_endpoint(
        self, domain_name: str, project_name: str, database_id: str
    ) -> Dict[str, Any]:
        """
        Manually refresh the database endpoint to get the latest IP address.

        Useful for updating legacy databases or after infrastructure changes.
        """
        # Get database from MongoDB
        db = await Database.find_one(
            Database.id == database_id,
            Database.domain == domain_name,
            Database.project == project_name
        )

        if not db:
            raise ValueError(f"Database {database_id} not found")

        # Reconstruct kubedb_resource_name if missing (legacy databases)
        if not db.kubedb_resource_name:
            db.kubedb_resource_name = f"{db.name}-{db.domain}-{db.project}"
            logger.info(
                "reconstructed_kubedb_resource_name",
                database_id=db.id,
                kubedb_resource_name=db.kubedb_resource_name
            )

        # Get provider kubeconfig for multi-cluster support
        provider_id, kubeconfig_content = await self._get_provider_kubeconfig(db)

        # Fetch the latest endpoint from Kubernetes
        endpoint_info = await kubedb_service.get_database_endpoint(
            engine=db.engine,
            name=db.kubedb_resource_name,
            namespace=db.namespace,
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )

        old_endpoint = db.endpoint

        if endpoint_info:
            db.endpoint = endpoint_info.get("host")
            db.port = endpoint_info.get("port")
            await db.save()

            logger.info(
                "endpoint_refreshed",
                database_id=db.id,
                old_endpoint=old_endpoint,
                new_endpoint=db.endpoint,
                port=db.port
            )

            return {
                "success": True,
                "database_id": db.id,
                "old_endpoint": old_endpoint,
                "new_endpoint": db.endpoint,
                "port": db.port,
                "message": "Endpoint successfully refreshed"
            }
        else:
            logger.warning(
                "endpoint_refresh_failed",
                database_id=db.id,
                kubedb_resource_name=db.kubedb_resource_name
            )
            return {
                "success": False,
                "database_id": db.id,
                "message": "Could not fetch endpoint from Kubernetes service"
            }

    async def update_database_upgrade_policy(
        self, domain: str, project: str, database_id: str, upgrade_policy
    ) -> bool:
        """
        Update the automated version upgrade policy for a database.

        Args:
            domain: Domain name
            project: Project name
            database_id: Database unique identifier
            upgrade_policy: UpgradePolicy model with new policy settings

        Returns:
            True if update was successful

        Raises:
            ValueError: If database not found
        """
        logger.info(
            "updating_database_upgrade_policy",
            database_id=database_id,
            domain=domain,
            project=project,
            strategy=upgrade_policy.strategy,
        )

        # Get database document
        db_doc = await self.get_database(domain, project, database_id)
        if not db_doc:
            raise ValueError(f"Database not found: {database_id}")

        # Update the upgrade policy in MongoDB
        update_data = {
            "upgrade_policy": upgrade_policy.model_dump(),
            "updated_at": datetime.utcnow(),
        }

        result = await self.db["databases"].update_one(
            {
                "database_id": database_id,
                "domain": domain,
                "project": project,
            },
            {"$set": update_data},
        )

        if result.modified_count > 0:
            logger.info(
                "upgrade_policy_updated_successfully",
                database_id=database_id,
                strategy=upgrade_policy.strategy,
            )
            return True
        else:
            logger.warning(
                "upgrade_policy_not_modified",
                database_id=database_id,
            )
            return False


# Global instance
database_service = DatabaseService()
