#!/bin/bash
set -e

echo "=========================================="
echo "   FALCON TRADER — DEPLOYMENT SCRIPT"
echo "=========================================="

# Pull latest code
echo "[1/5] Pulling latest code from GitHub..."
git pull origin master

# Check .env exists
if [ ! -f .env ]; then
    echo ""
    echo "ERROR: .env file not found!"
    echo "Run:  cp .env.example .env  then fill in your credentials."
    exit 1
fi

# Build containers
echo "[2/5] Building Docker images..."
docker compose build --no-cache

# Stop existing containers gracefully
echo "[3/5] Stopping existing containers..."
docker compose down

# Start all services
echo "[4/5] Starting all services..."
docker compose up -d

# Health check
echo "[5/5] Waiting for services to start..."
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
