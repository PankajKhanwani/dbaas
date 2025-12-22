"""
Metrics Service for Database Monitoring

Collects and aggregates metrics from database Prometheus exporters
without using port-forward (scalable for production).
"""
import httpx
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import structlog

from app.models.database import DatabaseEngine

logger = structlog.get_logger()


class MetricsService:
    """Service for collecting database metrics from Prometheus exporters."""

    def __init__(self):
        self.http_client = None

    async def initialize(self):
        """Initialize HTTP client."""
        if not self.http_client:
            self.http_client = httpx.AsyncClient(timeout=10.0)

    async def get_database_metrics(
        self,
        engine: DatabaseEngine,
        name: str,
        namespace: str,
    ) -> Dict[str, Any]:
        """
        Get comprehensive metrics for a database.

        Queries the database's Prometheus exporter service directly
        (no port-forward needed - uses Kubernetes service endpoints).

        Args:
            engine: Database engine type
            name: Database resource name (e.g., "mydb-demo-demo")
            namespace: Kubernetes namespace

        Returns:
            Dictionary with all available metrics
        """
        await self.initialize()

        metrics = {
            "timestamp": datetime.utcnow().isoformat(),
            "database": name,
            "engine": engine.value,
            "available": False,
        }

        try:
            # Get metrics endpoint URL (Kubernetes service DNS)
            # Format: <service-name>.<namespace>.svc.cluster.local:<port>
            stats_service = f"{name}-stats"

            # Try multiple URLs: cluster DNS for production, localhost for development
            urls_to_try = [
                f"http://{stats_service}.{namespace}.svc.cluster.local:56790/metrics",  # In-cluster
                "http://localhost:56790/metrics",  # Development (port-forward)
                "http://localhost:56791/metrics",  # Development (2nd database)
                "http://localhost:56792/metrics",  # Development (3rd database)
            ]

            response = None
            last_error = None

            for metrics_url in urls_to_try:
                try:
                    logger.debug(
                        "trying_metrics_endpoint",
                        name=name,
                        namespace=namespace,
                        url=metrics_url,
                    )

                    # Try to fetch from this URL
                    response = await self.http_client.get(metrics_url)

                    if response.status_code == 200:
                        logger.info(
                            "fetching_database_metrics",
                            name=name,
                            namespace=namespace,
                            url=metrics_url,
                        )
                        break  # Success, use this URL
                    else:
                        last_error = f"HTTP {response.status_code}"
                        response = None

                except Exception as e:
                    last_error = str(e)
                    response = None
                    continue

            # If all URLs failed
            if response is None:
                logger.warning(
                    "all_metrics_endpoints_failed",
                    name=name,
                    namespace=namespace,
                    last_error=last_error,
                )
                return metrics

            if response.status_code == 200:
                raw_metrics = response.text

                # Parse and structure metrics based on engine
                if engine == DatabaseEngine.MONGODB:
                    parsed = self._parse_mongodb_metrics(raw_metrics)
                elif engine == DatabaseEngine.POSTGRES:
                    parsed = self._parse_postgres_metrics(raw_metrics)
                elif engine == DatabaseEngine.MYSQL:
                    parsed = self._parse_mysql_metrics(raw_metrics)
                else:
                    parsed = self._parse_generic_metrics(raw_metrics)

                metrics.update(parsed)
                metrics["available"] = True

                logger.info(
                    "metrics_fetched_successfully",
                    name=name,
                    metric_count=len(parsed),
                )
            else:
                logger.warning(
                    "metrics_endpoint_unavailable",
                    name=name,
                    status=response.status_code,
                )

        except httpx.ConnectError:
            logger.warning(
                "metrics_service_unreachable",
                name=name,
                namespace=namespace,
            )
        except Exception as e:
            logger.error(
                "metrics_fetch_failed",
                name=name,
                error=str(e),
            )

        return metrics

    def _parse_mongodb_metrics(self, raw_metrics: str) -> Dict[str, Any]:
        """Parse MongoDB-specific metrics from Prometheus format with operational focus."""
        metrics = {}
        lines = raw_metrics.split('\n')

        # Temporary storage for raw values
        raw_values = {}

        # Parse all lines and extract relevant metrics
        for line in lines:
            if line.startswith('#') or not line.strip():
                continue

            try:
                if ' ' in line:
                    metric_part, value_str = line.rsplit(' ', 1)
                    value = float(value_str)

                    # Extract metric name and labels
                    if '{' in metric_part:
                        metric_name, labels_part = metric_part.split('{', 1)
                        labels_part = labels_part.rstrip('}')
                        labels = {}
                        for label_pair in labels_part.split(','):
                            if '=' in label_pair:
                                key, val = label_pair.split('=', 1)
                                labels[key.strip()] = val.strip('"')
                    else:
                        metric_name = metric_part
                        labels = {}

                    # Store in raw_values with full key
                    key = f"{metric_name}|{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
                    raw_values[key] = {'name': metric_name, 'labels': labels, 'value': value}
            except (ValueError, IndexError):
                pass

        # Extract OPERATIONAL METRICS

        # 1. CONNECTIONS
        metrics['connections'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mongodb_ss_connections':
                conn_type = data['labels'].get('conn_type', '')
                if conn_type == 'current':
                    metrics['connections']['active'] = int(data['value'])
                elif conn_type == 'available':
                    metrics['connections']['available'] = int(data['value'])

        if 'active' in metrics['connections'] and 'available' in metrics['connections']:
            total = metrics['connections']['active'] + metrics['connections']['available']
            metrics['connections']['total'] = total
            metrics['connections']['used_percent'] = (metrics['connections']['active'] / total * 100) if total > 0 else 0

        # 2. READ/WRITE OPERATIONS
        metrics['operations'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mongodb_ss_opcounters':
                # Try both 'type' and 'legacy_op_type' label keys
                op_type = data['labels'].get('type', data['labels'].get('legacy_op_type', ''))
                if op_type in ['insert', 'update', 'delete']:
                    if 'writes' not in metrics['operations']:
                        metrics['operations']['writes'] = 0
                    metrics['operations']['writes'] += int(data['value'])
                elif op_type == 'query':
                    metrics['operations']['reads'] = int(data['value'])

                # Also store individual counters
                if op_type == 'insert':
                    metrics['operations']['inserts'] = int(data['value'])
                elif op_type == 'update':
                    metrics['operations']['updates'] = int(data['value'])
                elif op_type == 'delete':
                    metrics['operations']['deletes'] = int(data['value'])
                elif op_type == 'command':
                    metrics['operations']['commands'] = int(data['value'])

        # 3. LOAD METRICS (CPU, Memory, Network)
        metrics['load'] = {}
        for key, data in raw_values.items():
            # CPU usage
            if data['name'] == 'mongodb_sys_cpu_user_ms':
                metrics['load']['cpu_user_ms'] = data['value']
            elif data['name'] == 'mongodb_sys_cpu_sys_ms':
                metrics['load']['cpu_system_ms'] = data['value']

            # Memory usage
            elif data['name'] == 'mongodb_ss_mem' and data['labels'].get('type') == 'resident':
                metrics['load']['memory_resident_mb'] = data['value']
            elif data['name'] == 'mongodb_ss_mem' and data['labels'].get('type') == 'virtual':
                metrics['load']['memory_virtual_mb'] = data['value']

            # Network I/O
            elif data['name'] == 'mongodb_ss_network_bytesIn':
                metrics['load']['network_in_bytes'] = data['value']
            elif data['name'] == 'mongodb_ss_network_bytesOut':
                metrics['load']['network_out_bytes'] = data['value']
            elif data['name'] == 'mongodb_ss_network_numRequests':
                metrics['load']['network_requests'] = int(data['value'])

        # Calculate CPU usage percent if possible
        if 'cpu_user_ms' in metrics['load'] and 'cpu_system_ms' in metrics['load']:
            total_cpu_ms = metrics['load']['cpu_user_ms'] + metrics['load']['cpu_system_ms']
            # This is cumulative, so we'd need to track over time for true %
            metrics['load']['cpu_total_ms'] = total_cpu_ms

        # 4. REPLICATION & HEALTHY REPLICAS
        metrics['replication'] = {}
        healthy_count = 0
        total_members = 0

        for key, data in raw_values.items():
            # Replica set member health
            if data['name'] == 'mongodb_members_health':
                total_members += 1
                if data['value'] == 1.0:
                    healthy_count += 1

            # Current member state
            elif data['name'] == 'mongodb_mongod_replset_my_state':
                state_value = int(data['value'])
                state_names = {
                    0: 'STARTUP',
                    1: 'PRIMARY',
                    2: 'SECONDARY',
                    3: 'RECOVERING',
                    5: 'STARTUP2',
                    6: 'UNKNOWN',
                    7: 'ARBITER',
                    8: 'DOWN',
                    9: 'ROLLBACK',
                    10: 'REMOVED'
                }
                metrics['replication']['state'] = state_names.get(state_value, f'UNKNOWN ({state_value})')
                metrics['replication']['state_value'] = state_value

            # Replication lag
            elif data['name'] == 'mongodb_mongod_replset_member_replication_lag':
                metrics['replication']['lag_seconds'] = data['value']

        if total_members > 0:
            metrics['replication']['healthy_replicas'] = healthy_count
            metrics['replication']['total_replicas'] = total_members
            metrics['replication']['health_percent'] = (healthy_count / total_members * 100) if total_members > 0 else 0

        # 5. STORAGE METRICS
        metrics['storage'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mongodb_dbstats_dataSize':
                if 'total_data_size' not in metrics['storage']:
                    metrics['storage']['total_data_size'] = 0
                metrics['storage']['total_data_size'] += data['value']
            elif data['name'] == 'mongodb_dbstats_storageSize':
                if 'total_storage_size' not in metrics['storage']:
                    metrics['storage']['total_storage_size'] = 0
                metrics['storage']['total_storage_size'] += data['value']
            elif data['name'] == 'mongodb_dbstats_indexes':
                if 'total_indexes' not in metrics['storage']:
                    metrics['storage']['total_indexes'] = 0
                metrics['storage']['total_indexes'] += int(data['value'])
            elif data['name'] == 'mongodb_dbstats_collections':
                if 'total_collections' not in metrics['storage']:
                    metrics['storage']['total_collections'] = 0
                metrics['storage']['total_collections'] += int(data['value'])

        return metrics

    def _categorize_mongodb_metric(self, metric_name: str) -> str:
        """Categorize MongoDB metric by its prefix."""
        if metric_name.startswith('mongodb_ss_connections'):
            return 'Connections'
        elif metric_name.startswith('mongodb_ss_network'):
            return 'Network'
        elif metric_name.startswith('mongodb_ss_opcounters'):
            return 'Operations'
        elif metric_name.startswith('mongodb_ss_mem'):
            return 'Memory'
        elif metric_name.startswith('mongodb_ss_wt') or 'wiredtiger' in metric_name.lower():
            return 'WiredTiger Storage'
        elif metric_name.startswith('mongodb_dbstats'):
            return 'Database Stats'
        elif metric_name.startswith('mongodb_ss_metrics'):
            return 'Server Metrics'
        elif metric_name.startswith('mongodb_ss_locks'):
            return 'Locks'
        elif metric_name.startswith('mongodb_ss_globalLock'):
            return 'Global Lock'
        elif metric_name.startswith('mongodb_ss_asserts'):
            return 'Assertions'
        elif metric_name.startswith('mongodb_mongod_replset') or 'repl' in metric_name:
            return 'Replication'
        elif metric_name.startswith('mongodb_mongod_storage'):
            return 'Storage Engine'
        elif metric_name.startswith('mongodb_sys'):
            return 'System'
        elif metric_name.startswith('mongodb_up'):
            return 'Health'
        else:
            return 'Other'

    def _format_metric_name(self, metric_name: str) -> str:
        """Format metric name for display (remove mongodb_ prefix and make readable)."""
        # Remove mongodb_ prefix
        name = metric_name.replace('mongodb_', '')
        # Replace underscores with spaces
        name = name.replace('_', ' ')
        # Capitalize words
        return ' '.join(word.capitalize() for word in name.split())

    def _parse_postgres_metrics(self, raw_metrics: str) -> Dict[str, Any]:
        """Parse PostgreSQL-specific metrics from Prometheus format with operational focus."""
        metrics = {}
        lines = raw_metrics.split('\n')

        # Temporary storage for raw values
        raw_values = {}

        # Parse all lines and extract relevant metrics
        for line in lines:
            if line.startswith('#') or not line.strip():
                continue

            try:
                if ' ' in line:
                    metric_part, value_str = line.rsplit(' ', 1)
                    value = float(value_str)

                    # Extract metric name and labels
                    if '{' in metric_part:
                        metric_name, labels_part = metric_part.split('{', 1)
                        labels_part = labels_part.rstrip('}')
                        labels = {}
                        for label_pair in labels_part.split(','):
                            if '=' in label_pair:
                                key, val = label_pair.split('=', 1)
                                labels[key.strip()] = val.strip('"')
                    else:
                        metric_name = metric_part
                        labels = {}

                    key = f"{metric_name}|{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
                    raw_values[key] = {'name': metric_name, 'labels': labels, 'value': value}
            except (ValueError, IndexError):
                pass

        # Extract OPERATIONAL METRICS

        # 1. ACTIVE CONNECTIONS
        metrics['connections'] = {}
        max_connections = 0
        total_active = 0

        for key, data in raw_values.items():
            if data['name'] == 'pg_stat_database_numbackends':
                # Sum up backends across all databases
                total_active += int(data['value'])
            elif data['name'] == 'pg_settings_max_connections':
                max_connections = int(data['value'])

        if total_active > 0:
            metrics['connections']['active'] = total_active
        if max_connections > 0:
            metrics['connections']['max'] = max_connections
            metrics['connections']['available'] = max_connections - total_active
            metrics['connections']['total'] = max_connections
            metrics['connections']['used_percent'] = (total_active / max_connections * 100) if max_connections > 0 else 0

        # 2. READ/WRITE OPERATIONS
        metrics['operations'] = {}
        total_inserts = 0
        total_updates = 0
        total_deletes = 0
        total_fetched = 0

        for key, data in raw_values.items():
            if data['name'] == 'pg_stat_database_tup_inserted':
                total_inserts += int(data['value'])
            elif data['name'] == 'pg_stat_database_tup_updated':
                total_updates += int(data['value'])
            elif data['name'] == 'pg_stat_database_tup_deleted':
                total_deletes += int(data['value'])
            elif data['name'] == 'pg_stat_database_tup_fetched':
                total_fetched += int(data['value'])

        if total_inserts > 0:
            metrics['operations']['inserts'] = total_inserts
        if total_updates > 0:
            metrics['operations']['updates'] = total_updates
        if total_deletes > 0:
            metrics['operations']['deletes'] = total_deletes
        if total_fetched > 0:
            metrics['operations']['reads'] = total_fetched

        # Calculate total writes
        writes = total_inserts + total_updates + total_deletes
        if writes > 0:
            metrics['operations']['writes'] = writes

        # 3. LOAD METRICS (Cache performance, transactions)
        metrics['load'] = {}
        cache_hits = 0
        cache_reads = 0
        commits = 0
        rollbacks = 0

        for key, data in raw_values.items():
            if data['name'] == 'pg_stat_database_blks_hit':
                cache_hits += data['value']
            elif data['name'] == 'pg_stat_database_blks_read':
                cache_reads += data['value']
            elif data['name'] == 'pg_stat_database_xact_commit':
                commits += data['value']
            elif data['name'] == 'pg_stat_database_xact_rollback':
                rollbacks += data['value']

        if cache_hits > 0 or cache_reads > 0:
            total = cache_hits + cache_reads
            metrics['load']['cache_hit_ratio'] = (cache_hits / total * 100) if total > 0 else 0
            metrics['load']['cache_hits'] = cache_hits
            metrics['load']['cache_reads'] = cache_reads

        if commits > 0:
            metrics['load']['transaction_commits'] = commits
        if rollbacks > 0:
            metrics['load']['transaction_rollbacks'] = rollbacks

        # 4. REPLICATION & HEALTHY REPLICAS
        metrics['replication'] = {}
        is_in_recovery = False
        replication_lag = 0

        for key, data in raw_values.items():
            if data['name'] == 'pg_replication_is_replica':
                is_in_recovery = data['value'] == 1.0
            elif data['name'] == 'pg_replication_lag':
                replication_lag = data['value']

        metrics['replication']['is_replica'] = is_in_recovery
        metrics['replication']['state'] = 'REPLICA' if is_in_recovery else 'PRIMARY'
        if replication_lag > 0:
            metrics['replication']['lag_seconds'] = replication_lag

        # 5. STORAGE METRICS
        metrics['storage'] = {}
        total_db_size = 0
        database_count = 0

        for key, data in raw_values.items():
            if data['name'] == 'pg_database_size_bytes':
                datname = data['labels'].get('datname', '')
                # Exclude template databases
                if datname not in ['template0', 'template1', 'postgres']:
                    total_db_size += data['value']
                    database_count += 1

        if total_db_size > 0:
            metrics['storage']['total_data_size'] = total_db_size
        if database_count > 0:
            metrics['storage']['database_count'] = database_count

        return metrics

    def _parse_mysql_metrics(self, raw_metrics: str) -> Dict[str, Any]:
        """Parse MySQL-specific metrics from Prometheus format with operational focus."""
        metrics = {}
        lines = raw_metrics.split('\n')

        # Temporary storage for raw values
        raw_values = {}

        # Parse all lines and extract relevant metrics
        for line in lines:
            if line.startswith('#') or not line.strip():
                continue

            try:
                if ' ' in line:
                    metric_part, value_str = line.rsplit(' ', 1)
                    value = float(value_str)

                    # Extract metric name and labels
                    if '{' in metric_part:
                        metric_name, labels_part = metric_part.split('{', 1)
                        labels_part = labels_part.rstrip('}')
                        labels = {}
                        for label_pair in labels_part.split(','):
                            if '=' in label_pair:
                                key, val = label_pair.split('=', 1)
                                labels[key.strip()] = val.strip('"')
                    else:
                        metric_name = metric_part
                        labels = {}

                    key = f"{metric_name}|{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
                    raw_values[key] = {'name': metric_name, 'labels': labels, 'value': value}
            except (ValueError, IndexError):
                pass

        # Extract OPERATIONAL METRICS

        # 1. ACTIVE CONNECTIONS
        metrics['connections'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mysql_global_status_threads_connected':
                metrics['connections']['active'] = int(data['value'])
            elif data['name'] == 'mysql_global_variables_max_connections':
                max_conn = int(data['value'])
                metrics['connections']['max'] = max_conn
                metrics['connections']['total'] = max_conn
                if 'active' in metrics['connections']:
                    active = metrics['connections']['active']
                    metrics['connections']['available'] = max_conn - active
                    metrics['connections']['used_percent'] = (active / max_conn * 100) if max_conn > 0 else 0

        # 2. READ/WRITE OPERATIONS (Queries and Commands)
        metrics['operations'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mysql_global_status_queries':
                metrics['operations']['total_queries'] = int(data['value'])
            elif data['name'] == 'mysql_global_status_questions':
                metrics['operations']['questions'] = int(data['value'])
            elif data['name'] == 'mysql_global_status_com_select':
                metrics['operations']['reads'] = int(data['value'])
            elif data['name'] == 'mysql_global_status_com_insert':
                if 'writes' not in metrics['operations']:
                    metrics['operations']['writes'] = 0
                metrics['operations']['writes'] += int(data['value'])
                metrics['operations']['inserts'] = int(data['value'])
            elif data['name'] == 'mysql_global_status_com_update':
                if 'writes' not in metrics['operations']:
                    metrics['operations']['writes'] = 0
                metrics['operations']['writes'] += int(data['value'])
                metrics['operations']['updates'] = int(data['value'])
            elif data['name'] == 'mysql_global_status_com_delete':
                if 'writes' not in metrics['operations']:
                    metrics['operations']['writes'] = 0
                metrics['operations']['writes'] += int(data['value'])
                metrics['operations']['deletes'] = int(data['value'])
            elif data['name'] == 'mysql_global_status_slow_queries':
                metrics['operations']['slow_queries'] = int(data['value'])

        # 3. LOAD METRICS (InnoDB buffer pool, network)
        metrics['load'] = {}
        buffer_pool_total = 0
        buffer_pool_free = 0

        for key, data in raw_values.items():
            if data['name'] == 'mysql_global_status_innodb_buffer_pool_pages_total':
                buffer_pool_total = data['value']
            elif data['name'] == 'mysql_global_status_innodb_buffer_pool_pages_free':
                buffer_pool_free = data['value']
            elif data['name'] == 'mysql_global_status_bytes_received':
                metrics['load']['network_in_bytes'] = data['value']
            elif data['name'] == 'mysql_global_status_bytes_sent':
                metrics['load']['network_out_bytes'] = data['value']
            elif data['name'] == 'mysql_global_status_threads_running':
                metrics['load']['threads_running'] = int(data['value'])

        if buffer_pool_total > 0:
            buffer_pool_used = buffer_pool_total - buffer_pool_free
            metrics['load']['buffer_pool_usage_percent'] = (buffer_pool_used / buffer_pool_total * 100) if buffer_pool_total > 0 else 0
            metrics['load']['buffer_pool_total_pages'] = buffer_pool_total
            metrics['load']['buffer_pool_free_pages'] = buffer_pool_free

        # 4. REPLICATION & HEALTHY REPLICAS
        metrics['replication'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mysql_slave_status_slave_io_running':
                io_running = data['value'] == 1.0
                metrics['replication']['io_running'] = io_running
            elif data['name'] == 'mysql_slave_status_slave_sql_running':
                sql_running = data['value'] == 1.0
                metrics['replication']['sql_running'] = sql_running
            elif data['name'] == 'mysql_slave_status_seconds_behind_master':
                metrics['replication']['lag_seconds'] = data['value']

        # Determine replication state
        if 'io_running' in metrics['replication'] or 'sql_running' in metrics['replication']:
            io_ok = metrics['replication'].get('io_running', False)
            sql_ok = metrics['replication'].get('sql_running', False)
            if io_ok and sql_ok:
                metrics['replication']['state'] = 'REPLICA (Healthy)'
                metrics['replication']['healthy_replicas'] = 1
                metrics['replication']['total_replicas'] = 1
                metrics['replication']['health_percent'] = 100.0
            else:
                metrics['replication']['state'] = 'REPLICA (Degraded)'
                metrics['replication']['healthy_replicas'] = 0
                metrics['replication']['total_replicas'] = 1
                metrics['replication']['health_percent'] = 0.0
        else:
            metrics['replication']['state'] = 'PRIMARY'

        # 5. STORAGE METRICS
        metrics['storage'] = {}
        for key, data in raw_values.items():
            if data['name'] == 'mysql_global_status_innodb_data_written':
                metrics['storage']['data_written_bytes'] = data['value']
            elif data['name'] == 'mysql_global_status_innodb_data_read':
                metrics['storage']['data_read_bytes'] = data['value']

        return metrics

    def _parse_generic_metrics(self, raw_metrics: str) -> Dict[str, Any]:
        """Parse generic metrics (for Redis, Elasticsearch, etc.)."""
        metrics = {}
        lines = raw_metrics.split('\n')

        # Extract basic metrics that are common across engines
        for line in lines:
            if line.startswith('#') or not line.strip():
                continue

            try:
                if ' ' in line:
                    metric_name, value = line.rsplit(' ', 1)
                    # Store as-is for now
                    if 'raw_metrics' not in metrics:
                        metrics['raw_metrics'] = {}
                    metrics['raw_metrics'][metric_name] = float(value)
            except (ValueError, IndexError):
                pass

        return metrics

    async def get_metrics_history(
        self,
        database_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get historical metrics for a database.

        This would typically query a time-series database like Prometheus
        or a metrics storage backend. For now, returns empty list.

        TODO: Implement with actual metrics storage backend
        """
        # Placeholder for historical metrics
        return []


# Global instance
metrics_service = MetricsService()
