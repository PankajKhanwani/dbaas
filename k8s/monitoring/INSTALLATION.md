# Grafana + Prometheus Installation Guide

Complete monitoring stack for KubeDB DBaaS Platform.

## Quick Installation

### One-Command Install (Recommended)

```bash
cd /Users/coredge.io/kube-db/k8s/monitoring
./install-monitoring.sh
```

This will:
- ✅ Install Prometheus for metrics collection
- ✅ Install Grafana for visualization
- ✅ Configure Prometheus as Grafana datasource
- ✅ Optionally import the KubeDB dashboard
- ✅ Display access URLs and credentials

## Manual Installation

### Step 1: Install Prometheus

```bash
kubectl apply -f prometheus-deployment.yaml
```

Verify:
```bash
kubectl get pods -n monitoring -l app=prometheus
```

Access Prometheus:
- **NodePort**: `http://<node-ip>:30900`
- **Internal**: `http://prometheus-server.monitoring.svc.cluster.local`

### Step 2: Install Grafana

```bash
kubectl apply -f grafana-deployment.yaml
```

Verify:
```bash
kubectl get pods -n monitoring -l app=grafana
```

Access Grafana:
- **NodePort**: `http://<node-ip>:30300`
- **LoadBalancer**: Check with `kubectl get svc grafana-loadbalancer -n monitoring`
- **Port-forward**: `kubectl port-forward -n monitoring svc/grafana 3000:3000`

**Default Credentials:**
- Username: `admin`
- Password: `admin`

### Step 3: Import Dashboard

#### Option A: Using Script
```bash
cd /Users/coredge.io/kube-db/k8s/monitoring

# With port-forward
kubectl port-forward -n monitoring svc/grafana 3000:3000 &
./import-dashboard.sh
```

#### Option B: Manual Import via UI
1. Login to Grafana
2. Go to **Dashboards** → **Import**
3. Click **Upload JSON file**
4. Select `grafana-dashboard-database.json`
5. Click **Import**

#### Option C: Using kubectl ConfigMap
```bash
kubectl apply -f grafana-dashboard-configmap.yaml
```
(Requires Grafana sidecar for dashboard auto-discovery)

## What Gets Installed

### Prometheus Stack
- **Namespace**: `monitoring`
- **Components**:
  - Prometheus Server (deployment)
  - Service Account with RBAC
  - ConfigMap for scrape configs
  - NodePort Service (port 30900)

### Grafana Stack
- **Namespace**: `monitoring`
- **Components**:
  - Grafana Server (deployment)
  - PersistentVolumeClaim (10Gi)
  - ConfigMap for Prometheus datasource
  - NodePort Service (port 30300)
  - LoadBalancer Service

### Dashboard Features
- 13 monitoring panels:
  1. CPU Usage (%)
  2. Memory Usage (GB)
  3. Disk I/O Read (MB/s)
  4. Disk I/O Write (MB/s)
  5. Queries Per Second (QPS)
  6. Replication Lag (seconds)
  7. Index Usage
  8. Slow Queries
  9. Node Health (HA)
  10. Backup Job Success/Failure
  11. Storage Consumption (GB)
  12. Storage Growth Rate (GB/hour)
  13. Active Connections

## Configuration

### Change Grafana Password

```bash
kubectl exec -it -n monitoring deployment/grafana -- grafana-cli admin reset-admin-password <new-password>
```

### Update Prometheus Scrape Configs

Edit the ConfigMap:
```bash
kubectl edit configmap prometheus-config -n monitoring
```

Restart Prometheus:
```bash
kubectl rollout restart deployment/prometheus-server -n monitoring
```

### Persist Grafana Data

The deployment uses PersistentVolumeClaim. To use a specific StorageClass:

```bash
# Edit grafana-deployment.yaml, update the PVC section:
spec:
  storageClassName: <your-storage-class>
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

## Exposing to Internet

### Using Ingress

Create an Ingress:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grafana-ingress
  namespace: monitoring
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - grafana.yourdomain.com
    secretName: grafana-tls
  rules:
  - host: grafana.yourdomain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: grafana
            port:
              number: 3000
```

Apply:
```bash
kubectl apply -f grafana-ingress.yaml
```

### Using LoadBalancer (Production)

The `grafana-loadbalancer` service is already configured. Get the external IP:

```bash
kubectl get svc grafana-loadbalancer -n monitoring -w
```

## Verification

### Check All Components

```bash
kubectl get all -n monitoring
```

### Test Prometheus

```bash
kubectl port-forward -n monitoring svc/prometheus-server 9090:80
# Open http://localhost:9090
```

### Test Grafana

```bash
kubectl port-forward -n monitoring svc/grafana 3000:3000
# Open http://localhost:3000
# Login: admin/admin
```

### Test Dashboard

1. Login to Grafana
2. Go to **Dashboards**
3. Select **KubeDB Database Monitoring**
4. Select a database ID from dropdown
5. Verify metrics are displaying

## Troubleshooting

### Grafana Pod Not Starting

```bash
kubectl describe pod -n monitoring -l app=grafana
kubectl logs -n monitoring -l app=grafana
```

### No Metrics Showing

Check Prometheus targets:
```bash
kubectl port-forward -n monitoring svc/prometheus-server 9090:80
# Open http://localhost:9090/targets
```

Verify pods are scraped:
```bash
# Check if pods have prometheus.io/scrape annotation
kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.prometheus\.io/scrape}{"\n"}{end}'
```

### Dashboard Not Loading

Check datasource configuration:
```bash
kubectl exec -it -n monitoring deployment/grafana -- cat /etc/grafana/provisioning/datasources/prometheus.yaml
```

## Uninstallation

```bash
kubectl delete -f grafana-deployment.yaml
kubectl delete -f prometheus-deployment.yaml
kubectl delete namespace monitoring
```

## Security Recommendations

1. **Change default password** immediately after installation
2. **Enable HTTPS** using Ingress with TLS
3. **Configure authentication** (OAuth, LDAP, etc.)
4. **Set up RBAC** for Grafana users
5. **Enable audit logging**

## Production Recommendations

1. **Persistent Storage**: Use reliable StorageClass (not local)
2. **High Availability**: Run multiple Grafana replicas
3. **Backup**: Regular backups of Grafana database
4. **Monitoring**: Monitor the monitoring stack itself
5. **Resource Limits**: Adjust based on your load
6. **Retention**: Configure Prometheus retention based on needs

## Next Steps

After installation:

1. ✅ Access Grafana and change default password
2. ✅ Verify all panels are showing data
3. ✅ Configure alerting rules (optional)
4. ✅ Set up notifications (Slack, email, etc.)
5. ✅ Create additional custom dashboards
6. ✅ Configure backup strategy

## Support

For issues or questions, check:
- Grafana logs: `kubectl logs -n monitoring -l app=grafana`
- Prometheus logs: `kubectl logs -n monitoring -l app=prometheus`
- KubeDB operator logs: `kubectl logs -n kubedb -l app.kubernetes.io/name=kubedb`
