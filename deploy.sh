#!/bin/bash
set -e

echo "=========================================="
echo "   FALCON TRADER — DEPLOYMENT SCRIPT"
echo "=========================================="

# Pull latest code
echo "[1/6] Pulling latest code from GitHub..."
git pull origin master

# Check .env exists
if [ ! -f .env ]; then
    echo ""
    echo "ERROR: .env file not found!"
    echo "Run:  cp .env.example .env  then fill in your credentials."
    exit 1
fi

# Build containers
echo "[2/6] Building Docker images..."
docker compose build --no-cache

# Stop existing containers gracefully
echo "[3/6] Stopping existing containers..."
docker compose down

# Start all services
echo "[4/6] Starting all services..."
docker compose up -d

# Health check
echo "[5/6] Waiting for services to start..."
sleep 15
docker compose ps

SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "=========================================="
echo "   DEPLOYMENT COMPLETE"
echo ""
echo "   API:       http://$SERVER_IP:8000/docs"
echo "   Dashboard: http://$SERVER_IP:8501"
echo "=========================================="

# Invariant check — re-verifies the specific bugs fixed in past audits haven't
# been silently reintroduced (see scripts/verify_invariants.py docstring).
# Runs against this checked-out repo and the just-started API, so it needs
# python3 + requests on the host, not just inside the containers.
echo ""
echo "[6/6] Verifying invariants..."
if command -v python3 >/dev/null 2>&1; then
    python3 scripts/verify_invariants.py --repo . --api http://localhost:8000/api/v1
else
    echo "python3 not found on host — skipping invariant check. Run it manually:"
    echo "  python3 scripts/verify_invariants.py"
fi
