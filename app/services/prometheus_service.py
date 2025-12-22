"""
Prometheus service for fetching database metrics directly.

Queries Prometheus HTTP API to retrieve metrics.
Designed for production-grade monitoring with lakhs of concurrent users.
"""
import asyncio
import httpx
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from app.config.logging import get_logger
from app.models.database import DatabaseEngine

logger = get_logger(__name__)


class PrometheusService:
    """
    Service for querying Prometheus directly to fetch database metrics.

    Architecture:
    Prometheus (collects + stores) → Application (queries directly)

    Benefits:
    - Direct access to metrics (no intermediary)
    - Lower latency
    - Simpler architecture
    - Full PromQL support
    """

    def __init__(
        self,
        prometheus_url: str = "http://prometheus-server:9090",
    ):
        """
        Initialize Prometheus service.

        Args:
            prometheus_url: Prometheus server base URL
        """
        self.prometheus_url = prometheus_url.rstrip('/')

        # HTTP client with connection pooling for high concurrency
        # Optimized for lakhs of concurrent users
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(
                max_keepalive_connections=100,  # Reuse connections
                max_connections=200,             # Handle high concurrency
            ),
            headers={
                "Content-Type": "application/json",
            }
        )

    async def query_instant(
        self,
        query: str,
        time: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute instant PromQL query.

        Args:
            query: PromQL query string
            time: Optional evaluation timestamp (default: now)

        Returns:
            Query results from Prometheus
        """
        try:
            # Prometheus instant query API
            url = f"{self.prometheus_url}/api/v1/query"

            params = {"query": query}
            if time:
                params["time"] = time

            logger.debug(
                "prometheus_instant_query",
                query=query,
                url=url
            )

            response = await self.client.get(url, params=params)
            response.raise_for_status()

            data = response.json()

            if data.get("status") != "success":
                logger.error(
                    "prometheus_query_failed",
                    query=query,
                    response=data
                )
                return {"error": "Query failed", "details": data}

            return data.get("data", {})

        except httpx.HTTPError as e:
            logger.error(
                "prometheus_http_error",
                query=query,
                error=str(e)
            )
            raise
        except Exception as e:
            logger.error(
                "prometheus_query_exception",
                query=query,
                error=str(e)
            )
            raise

    async def query_range(
        self,
        query: str,
        start: str,
        end: str,
        step: str = "15s"
    ) -> Dict[str, Any]:
        """
        Execute range PromQL query.

        Args:
            query: PromQL query string
            start: Start timestamp (RFC3339 or Unix timestamp)
            end: End timestamp (RFC3339 or Unix timestamp)
            step: Query resolution step (e.g., "15s", "1m")

        Returns:
            Range query results from Prometheus
        """
        try:
            url = f"{self.prometheus_url}/api/v1/query_range"

            params = {
                "query": query,
                "start": start,
                "end": end,
                "step": step
            }

            logger.debug(
                "prometheus_range_query",
                query=query,
                start=start,
                end=end,
                step=step
            )

            response = await self.client.get(url, params=params)
            response.raise_for_status()

            data = response.json()

            if data.get("status") != "success":
                logger.error(
                    "prometheus_range_query_failed",
                    query=query,
                    response=data
                )
                return {"error": "Query failed", "details": data}

            return data.get("data", {})

        except Exception as e:
            logger.error(
                "prometheus_range_query_exception",
                query=query,
                error=str(e)
            )
            raise

    async def get_database_metrics(
        self,
        engine: DatabaseEngine,
        database_name: str,
        namespace: str = "default"
    ) -> Dict[str, Any]:
        """
        Get comprehensive metrics for a database.

        Fetches:
        - Performance metrics (queries/sec, latency)
        - Resource usage (CPU, memory, storage)
        - Connection metrics
        - Engine-specific metrics

        Args:
            engine: Database engine type
            database_name: KubeDB resource name
            namespace: Kubernetes namespace

        Returns:
            Comprehensive metrics dictionary
        """
        logger.info(
            "fetching_database_metrics_from_prometheus",
            engine=engine,
            database_name=database_name,
            namespace=namespace
        )

        # Build queries based on engine type
        queries = self._build_metric_queries(engine, database_name, namespace)

        # Execute all queries concurrently (optimized for high load)
        results = await asyncio.gather(
            *[self.query_instant(q) for q in queries.values()],
            return_exceptions=True
        )

        # Combine results
        metrics = {}
        for (metric_name, query), result in zip(queries.items(), results):
            if isinstance(result, Exception):
                logger.warning(
                    "metric_query_failed",
                    metric=metric_name,
                    error=str(result)
                )
                metrics[metric_name] = None
            else:
                metrics[metric_name] = self._extract_metric_value(result)

        return {
            "database_name": database_name,
            "engine": engine,
            "namespace": namespace,
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": metrics,
        }

    def _build_metric_queries(
        self,
        engine: DatabaseEngine,
        database_name: str,
        namespace: str
    ) -> Dict[str, str]:
        """
        Build PromQL queries for database metrics.

        Returns a dictionary of metric_name -> promql_query
        """
        label_selector = f'job=~".*{database_name}.*",namespace="{namespace}"'

        # Common metrics for all databases
        queries = {
            "cpu_usage": f'sum(rate(container_cpu_usage_seconds_total{{{label_selector}}}[5m])) by (pod)',
            "memory_usage": f'sum(container_memory_working_set_bytes{{{label_selector}}}) by (pod)',
            "network_receive": f'sum(rate(container_network_receive_bytes_total{{{label_selector}}}[5m])) by (pod)',
            "network_transmit": f'sum(rate(container_network_transmit_bytes_total{{{label_selector}}}[5m])) by (pod)',
        }

        # Engine-specific metrics
        if engine == DatabaseEngine.MONGODB:
            queries.update({
                "operations_per_sec": f'sum(rate(mongodb_op_counters_total{{{label_selector}}}[5m]))',
                "connections": f'mongodb_connections{{state="current",{label_selector}}}',
                "documents": f'sum(mongodb_ss_metrics_document_total{{{label_selector}}})',
                "storage_size": f'sum(mongodb_ss_wt_cache_bytes_total{{{label_selector}}})',
            })

        elif engine == DatabaseEngine.POSTGRES:
            queries.update({
                "transactions_per_sec": f'sum(rate(pg_stat_database_xact_commit{{{label_selector}}}[5m]))',
                "connections": f'sum(pg_stat_database_numbackends{{{label_selector}}})',
                "rows_fetched": f'sum(rate(pg_stat_database_tup_fetched{{{label_selector}}}[5m]))',
                "cache_hit_ratio": f'avg(pg_stat_database_blks_hit{{{label_selector}}} / (pg_stat_database_blks_hit{{{label_selector}}} + pg_stat_database_blks_read{{{label_selector}}}) * 100)',
            })

        elif engine == DatabaseEngine.MYSQL or engine == DatabaseEngine.MARIADB:
            queries.update({
                "queries_per_sec": f'sum(rate(mysql_global_status_queries{{{label_selector}}}[5m]))',
                "connections": f'mysql_global_status_threads_connected{{{label_selector}}}',
                "slow_queries": f'sum(rate(mysql_global_status_slow_queries{{{label_selector}}}[5m]))',
                "innodb_buffer_pool_hit_rate": f'avg(mysql_global_status_innodb_buffer_pool_read_requests{{{label_selector}}} / (mysql_global_status_innodb_buffer_pool_read_requests{{{label_selector}}} + mysql_global_status_innodb_buffer_pool_reads{{{label_selector}}}) * 100)',
            })

        elif engine == DatabaseEngine.REDIS:
            queries.update({
                "commands_per_sec": f'sum(rate(redis_commands_processed_total{{{label_selector}}}[5m]))',
                "connections": f'redis_connected_clients{{{label_selector}}}',
                "memory_used": f'redis_memory_used_bytes{{{label_selector}}}',
                "hit_rate": f'avg(rate(redis_keyspace_hits_total{{{label_selector}}}[5m]) / (rate(redis_keyspace_hits_total{{{label_selector}}}[5m]) + rate(redis_keyspace_misses_total{{{label_selector}}}[5m])) * 100)',
            })

        elif engine == DatabaseEngine.ELASTICSEARCH:
            queries.update({
                "search_query_total": f'sum(rate(elasticsearch_indices_search_query_total{{{label_selector}}}[5m]))',
                "indexing_rate": f'sum(rate(elasticsearch_indices_indexing_index_total{{{label_selector}}}[5m]))',
                "jvm_memory_used": f'elasticsearch_jvm_memory_used_bytes{{{label_selector}}}',
                "active_shards": f'elasticsearch_cluster_health_active_shards{{{label_selector}}}',
            })

        return queries

    def _extract_metric_value(self, query_result: Dict[str, Any]) -> Any:
        """
        Extract the actual metric value from Prometheus query result.

        Handles both instant queries and range queries.
        """
        if not query_result:
            return None

        result_type = query_result.get("resultType")
        result = query_result.get("result", [])

        if not result:
            return None

        # For vector results (instant queries)
        if result_type == "vector":
            if len(result) == 1:
                # Single value
                value = result[0].get("value", [None, None])
                return float(value[1]) if len(value) > 1 else None
            else:
                # Multiple values (e.g., per-pod metrics)
                return [
                    {
                        "labels": r.get("metric", {}),
                        "value": float(r.get("value", [None, None])[1]) if r.get("value") else None
                    }
                    for r in result
                ]

        # For matrix results (range queries)
        elif result_type == "matrix":
            return [
                {
                    "labels": r.get("metric", {}),
                    "values": r.get("values", [])
                }
                for r in result
            ]

        return result

    async def health_check(self) -> bool:
        """
        Check if Prometheus is reachable and healthy.

        Returns:
            True if Prometheus is healthy, False otherwise
        """
        try:
            url = f"{self.prometheus_url}/-/healthy"
            response = await self.client.get(url)
            response.raise_for_status()

            is_healthy = response.status_code == 200

            logger.info(
                "prometheus_health_check",
                healthy=is_healthy,
                status_code=response.status_code
            )

            return is_healthy

        except Exception as e:
            logger.error("prometheus_health_check_failed", error=str(e))
            return False

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# Global instance (will be configured from settings)
prometheus_service = PrometheusService()
