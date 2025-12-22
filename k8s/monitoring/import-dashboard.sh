#!/bin/bash
# Script to import Grafana dashboard for KubeDB database monitoring

set -e

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-admin}"
DASHBOARD_FILE="${DASHBOARD_FILE:-grafana-dashboard-database.json}"

echo "Importing Grafana dashboard from $DASHBOARD_FILE..."

# Check if file exists
if [ ! -f "$DASHBOARD_FILE" ]; then
    echo "Error: Dashboard file $DASHBOARD_FILE not found"
    exit 1
fi

# Get Grafana API key or use basic auth
if [ -n "$GRAFANA_API_KEY" ]; then
    AUTH_HEADER="Authorization: Bearer $GRAFANA_API_KEY"
else
    AUTH_HEADER="Authorization: Basic $(echo -n $GRAFANA_USER:$GRAFANA_PASSWORD | base64)"
fi

# Prepare dashboard JSON (Grafana API expects dashboard object wrapped)
DASHBOARD_JSON=$(cat "$DASHBOARD_FILE" | jq '{
  dashboard: .dashboard,
  overwrite: true,
  inputs: []
}')

# Import dashboard
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  -H "Content-Type: application/json" \
  -H "$AUTH_HEADER" \
  -d "$DASHBOARD_JSON" \
  "$GRAFANA_URL/api/dashboards/db")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" -eq 200 ]; then
    echo "✓ Dashboard imported successfully!"
    echo "Dashboard URL: $GRAFANA_URL$(echo $BODY | jq -r '.url')"
else
    echo "✗ Failed to import dashboard (HTTP $HTTP_CODE)"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
    exit 1
fi

