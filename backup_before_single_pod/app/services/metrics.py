"""
Prometheus metrics for operation queue and workers.

Provides observability into async operation processing.
"""
from prometheus_client import Counter, Histogram, Gauge

# Operation metrics
operation_total = Counter(
    "dbaas_operation_total",
    "Total number of operations processed",
    ["type", "status"],
)

operation_duration_seconds = Histogram(
    "dbaas_operation_duration_seconds",
    "Time spent processing operations",
    ["type", "status"],
    buckets=(10, 30, 60, 120, 300, 600, 1800, 3600),  # 10s to 1h
)

operation_retry_total = Counter(
    "dbaas_operation_retry_total",
    "Total number of operation retries",
    ["type"],
)

# Queue metrics
queue_depth = Gauge(
    "dbaas_queue_depth",
    "Number of operations waiting in queue",
)

queue_processing = Gauge(
    "dbaas_queue_processing",
    "Number of operations currently being processed",
)

queue_enqueue_total = Counter(
    "dbaas_queue_enqueue_total",
    "Total number of operations enqueued",
    ["type"],
)

queue_dequeue_total = Counter(
    "dbaas_queue_dequeue_total",
    "Total number of operations dequeued",
)

queue_duplicate_rejected_total = Counter(
    "dbaas_queue_duplicate_rejected_total",
    "Total number of duplicate operations rejected",
    ["type"],
)

# Worker metrics
worker_busy = Gauge(
    "dbaas_worker_busy",
    "Whether worker is currently processing an operation",
    ["worker_id"],
)

worker_operations_processed = Counter(
    "dbaas_worker_operations_processed",
    "Total operations processed by worker",
    ["worker_id"],
)

worker_errors = Counter(
    "dbaas_worker_errors",
    "Total errors encountered by worker",
    ["worker_id"],
)

# OpsRequest metrics
ops_request_created_total = Counter(
    "dbaas_ops_request_created_total",
    "Total KubeDB OpsRequests created",
    ["engine", "type"],
)

ops_request_duration_seconds = Histogram(
    "dbaas_ops_request_duration_seconds",
    "Time spent waiting for OpsRequest completion",
    ["engine", "type", "phase"],
    buckets=(10, 30, 60, 120, 300, 600, 1800, 3600),  # 10s to 1h
)

ops_request_phase_total = Counter(
    "dbaas_ops_request_phase_total",
    "Total OpsRequests by final phase",
    ["engine", "type", "phase"],
)


def record_operation_start(operation_type: str):
    """Record operation start."""
    queue_dequeue_total.inc()


def record_operation_complete(operation_type: str, status: str, duration_seconds: float):
    """Record operation completion."""
    operation_total.labels(type=operation_type, status=status).inc()
    operation_duration_seconds.labels(type=operation_type, status=status).observe(
        duration_seconds
    )


def record_operation_retry(operation_type: str):
    """Record operation retry."""
    operation_retry_total.labels(type=operation_type).inc()


def record_queue_enqueue(operation_type: str):
    """Record operation enqueued."""
    queue_enqueue_total.labels(type=operation_type).inc()


def record_duplicate_rejected(operation_type: str):
    """Record duplicate operation rejected."""
    queue_duplicate_rejected_total.labels(type=operation_type).inc()


def update_queue_stats(queued: int, processing: int):
    """Update queue depth metrics."""
    queue_depth.set(queued)
    queue_processing.set(processing)


def set_worker_busy(worker_id: int, busy: bool):
    """Set worker busy state."""
    worker_busy.labels(worker_id=str(worker_id)).set(1 if busy else 0)


def record_worker_operation_processed(worker_id: int):
    """Record worker processed an operation."""
    worker_operations_processed.labels(worker_id=str(worker_id)).inc()


def record_worker_error(worker_id: int):
    """Record worker error."""
    worker_errors.labels(worker_id=str(worker_id)).inc()


def record_ops_request_created(engine: str, ops_type: str):
    """Record OpsRequest created."""
    ops_request_created_total.labels(engine=engine, type=ops_type).inc()


def record_ops_request_complete(
    engine: str, ops_type: str, phase: str, duration_seconds: float
):
    """Record OpsRequest completion."""
    ops_request_phase_total.labels(engine=engine, type=ops_type, phase=phase).inc()
    ops_request_duration_seconds.labels(engine=engine, type=ops_type, phase=phase).observe(
        duration_seconds
    )
