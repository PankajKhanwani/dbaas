# KubeDB Database Monitoring with Grafana

This directory contains Grafana dashboard configuration for comprehensive KubeDB database monitoring.

## Files

- **`grafana-dashboard-database.json`** - Complete Grafana dashboard JSON with all metrics
- **`grafana-dashboard-configmap.yaml`** - Kubernetes ConfigMap template for auto-discovery
- **`import-dashboard.sh`** - Script to import dashboard via Grafana API
- **`GRAFANA_SETUP.md`** - Detailed setup and configuration guide

## Quick Start

1. **Import the dashboard:**
   ```bash
   cd k8s/monitoring
   ./import-dashboard.sh
   ```

2. **Select Database ID:**
   - Use the dropdown at the top of the dashboard
   - Select a specific database ID or "All" to view all databases

3. **View Metrics:**
   - All panels automatically filter by selected database ID
   - Metrics update every 30 seconds

## Dashboard Panels

The dashboard includes 13 panels covering all requested metrics:

1. **CPU Usage (%)** - CPU utilization per pod
2. **Memory Usage (GB)** - Memory consumption per pod
3. **Disk I/O Read (MB/s)** - Read throughput
4. **Disk I/O Write (MB/s)** - Write throughput
5. **Queries Per Second (QPS)** - Database query rate
6. **Replication Lag (seconds)** - HA replication delay (with alert)
7. **Index Usage** - Index count and usage
8. **Slow Queries (per second)** - Performance issues
9. **Node Health (HA)** - Healthy vs total replicas
10. **Backup Job Success/Failure** - Backup status tracking
11. **Storage Consumption (GB)** - Current storage usage
12. **Storage Growth Rate (GB/hour)** - Capacity planning

## Database ID Filtering

The dashboard uses a variable `$db_id` that:
- Extracts database IDs from Kubernetes pod labels or names
- Supports pattern matching: `db-*` format
- Allows filtering to specific database or viewing all

## Metric Sources

Metrics are queried from Prometheus using:
- **Kubernetes cAdvisor**: CPU, Memory, Disk I/O, Storage
- **Database Exporters**: QPS, Replication Lag, Index Usage, Slow Queries
- **Custom Metrics**: Backup job status (if available)

## Troubleshooting

See `GRAFANA_SETUP.md` for detailed troubleshooting guide.

## Customization

To customize the dashboard:
1. Import it into Grafana
2. Make changes via Grafana UI
3. Export and save as new version
4. Or edit the JSON directly and re-import

