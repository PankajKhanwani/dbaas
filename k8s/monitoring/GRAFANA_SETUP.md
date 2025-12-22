# Grafana Dashboard Setup for KubeDB Database Monitoring

This guide explains how to set up the Grafana dashboard for monitoring all KubeDB databases.

## Dashboard Overview

The dashboard provides comprehensive monitoring for all database metrics:
- **CPU, Memory, Disk I/O**: Resource utilization
- **Queries per second (QPS)**: Database performance
- **Replication lag**: HA cluster health
- **Index usage**: Database optimization metrics
- **Slow queries**: Performance issues
- **Node health (HA)**: High availability status
- **Backup job success/failure**: Backup monitoring
- **Storage consumption & growth**: Capacity planning

## Prerequisites

1. Prometheus installed and scraping KubeDB database metrics
2. Grafana installed and configured with Prometheus datasource
3. KubeDB databases with monitoring enabled

## Installation

### Option 1: Import via Grafana UI

1. Open Grafana UI (typically `http://localhost:3000`)
2. Navigate to **Dashboards** → **Import**
3. Click **Upload JSON file**
4. Select `grafana-dashboard-database.json`
5. Configure the Prometheus datasource (should be named "prometheus")
6. Click **Import**

### Option 2: Import via API

```bash
# Set Grafana credentials
GRAFANA_URL="http://localhost:3000"
GRAFANA_USER="admin"
GRAFANA_PASSWORD="admin"

# Import dashboard
curl -X POST \
  -H "Content-Type: application/json" \
  -d @grafana-dashboard-database.json \
  "$GRAFANA_URL/api/dashboards/db" \
  -u "$GRAFANA_USER:$GRAFANA_PASSWORD"
```

### Option 3: Kubernetes ConfigMap (Auto-discovery)

1. Copy the dashboard JSON into `grafana-dashboard-configmap.yaml`
2. Apply the ConfigMap:
   ```bash
   kubectl apply -f k8s/monitoring/grafana-dashboard-configmap.yaml
   ```
3. Grafana will auto-discover the dashboard (if configured for auto-discovery)

### Option 4: Using Import Script

```bash
cd k8s/monitoring
./import-dashboard.sh
```

Or with custom settings:
```bash
GRAFANA_URL="http://grafana.example.com" \
GRAFANA_USER="admin" \
GRAFANA_PASSWORD="your-password" \
./import-dashboard.sh
```

## Configuration

### Database ID Variable

The dashboard uses a variable `$db_id` to filter metrics by database ID. The variable:
- Extracts database IDs from pod names using regex: `/.*-(db-[a-z0-9]+).*/`
- Automatically discovers all databases from Kubernetes pods
- Allows filtering to a specific database or viewing all databases

### Prometheus Datasource

Ensure your Prometheus datasource in Grafana:
- Is named "prometheus" (or update the dashboard JSON)
- Has UID "prometheus" (or update the dashboard JSON)
- Can scrape metrics from Kubernetes pods

### Metric Labels

The dashboard queries metrics using:
- `pod=~"$db_id.*"` - Filters pods by database ID (pattern matching on pod names)
- `pod_label_database_id="$db_id"` - Filters by database-id label (if available on pods)
- `instance=~"$db_id.*"` - Filters database exporter instances
- `db_id="$db_id"` - Direct database ID matching (for custom metrics)

### Important: Pod Labeling

For the dashboard to work correctly, ensure pods are labeled with `database-id`:

**Option 1: Automatic (if KubeDB propagates labels)**
- KubeDB resources are created with `database-id` label
- This should propagate to pods automatically

**Option 2: Manual Labeling via Service**
- Create a service that adds labels to pods based on database resource name
- Or use a mutating webhook to add labels

**Option 3: Use Resource Name Pattern**
- The dashboard also supports pod name pattern matching
- Pods are typically named: `{kubedb_resource_name}-{replica-index}`
- Database ID can be extracted from resource name if naming convention includes it

## Metric Queries

### Resource Metrics (CPU, Memory, Disk)
- Uses Kubernetes cAdvisor metrics: `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `container_fs_*`
- Filters by pod name pattern matching database ID

### Database-Specific Metrics
- **MongoDB**: `mongodb_ss_opcounters`, `mongodb_mongod_replset_*`, `mongodb_dbstats_*`
- **PostgreSQL**: `pg_stat_database_*`, `pg_replication_*`, `pg_stat_user_indexes_*`
- **MySQL**: `mysql_global_status_*`, `mysql_slave_status_*`
- **Redis**: `redis_commands_processed_total`, `redis_*`
- **Elasticsearch**: `elasticsearch_*`

### Backup Metrics
- Requires custom metric: `kubedb_backup_job_status{db_id="..."}`
- If not available, this panel will show "No data"

## Customization

### Adding Custom Metrics

To add custom metrics to the dashboard:

1. Edit `grafana-dashboard-database.json`
2. Add a new panel in the `panels` array
3. Use PromQL queries with `$db_id` variable
4. Re-import the dashboard

### Changing Time Range

Default time range is "Last 1 hour". To change:
- Edit dashboard JSON: `"time": {"from": "now-6h", "to": "now"}`
- Or use Grafana UI: Dashboard settings → Time options

### Alerting

The dashboard includes an alert for replication lag:
- **Condition**: Replication lag > 10 seconds for 5 minutes
- **Action**: Configure notification channels in Grafana

To add more alerts:
1. Edit panel → Alert tab
2. Configure conditions and notifications

## Troubleshooting

### No Data Showing

1. **Check Prometheus is scraping**: Verify pods have `prometheus.io/scrape: "true"` annotation
2. **Check metric names**: Verify metric names match your Prometheus exporters
3. **Check database ID variable**: Ensure regex pattern matches your pod naming convention
4. **Check time range**: Ensure data exists for selected time range

### Database ID Not Appearing

1. **Check pod labels**: Verify pods are labeled correctly
2. **Update regex**: Modify the regex in the variable definition to match your naming pattern
3. **Manual entry**: You can manually type database ID in the variable dropdown

### Metric Names Don't Match

Different database exporters use different metric names. Update queries in the dashboard to match your exporter:
- MongoDB: Percona MongoDB Exporter, MongoDB Exporter
- PostgreSQL: postgres_exporter
- MySQL: mysqld_exporter
- Redis: redis_exporter

## Example Prometheus Scrape Config

```yaml
scrape_configs:
  - job_name: 'kubernetes-pods'
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: true
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        action: replace
        regex: ([^:]+)(?::\d+)?;(\d+)
        replacement: $1:$2
        target_label: __address__
```

## Support

For issues or questions:
1. Check Grafana logs: `kubectl logs -n monitoring <grafana-pod>`
2. Check Prometheus targets: Prometheus UI → Status → Targets
3. Verify metric availability: Prometheus UI → Graph → Run queries manually

