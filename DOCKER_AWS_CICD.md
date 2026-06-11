# VOLUME 7

# DOCKER + AWS + CI/CD

Deployment Model
Single VPS initially
Microservice-ready architecture

---

# DOCKER COMPOSE
Services
* mysql
* redis
* market-data-service
* indicator-service
* strategy-service
* risk-service
* order-service
* broker-service
* portfolio-service
* reporting-service
* dashboard-service
* prometheus
* grafana
* nginx

---

# NETWORK DESIGN
Internet
↓
Nginx
↓
FastAPI
↓
Internal Services
↓
MySQL
Redis

---

# DOCKER VOLUMES
* mysql_data
* redis_data
* grafana_data
* logs
* backups

---

# ENVIRONMENT VARIABLES
* DATABASE_URL
* REDIS_URL
* ZERODHA_API_KEY
* ZERODHA_API_SECRET
* JWT_SECRET
* LOG_LEVEL

---

# INITIAL AWS ARCHITECTURE
AWS Lightsail
4 vCPU
8 GB RAM
100 GB SSD
Ubuntu 24.04
Docker
Docker Compose
MySQL
Redis
All services
Single machine

---

# SCALE-UP ARCHITECTURE
Internet
↓
Cloudflare
↓
AWS ALB
↓
EC2 Auto Scaling Group
↓
Container Services
↓
RDS MySQL
↓
ElastiCache Redis
↓
S3 Backups

---

# MONITORING
Prometheus Metrics:
CPU, RAM, Disk, API Latency, Signal Count, Order Count, PnL, Open Positions

---

# GRAFANA DASHBOARDS
Trading Dashboard
Infrastructure Dashboard
Risk Dashboard
Strategy Dashboard

---

# ALERTS
Telegram, Email, Slack

Triggers:
Order Failure, Daily Loss Limit, Server Down, Database Down, API Down, Memory > 90%, Disk > 85%

---

# CI/CD PIPELINE
GitHub
↓
Pull Request
↓
Unit Tests
↓
Integration Tests
↓
Docker Build
↓
Security Scan
↓
Deploy
↓
Health Check
↓
Success

---

# GITHUB ACTIONS PIPELINE

Step 1: Lint
ruff, black, mypy

Step 2: Unit Tests
pytest

Step 3: Build Docker Images

Step 4: Push Images
GitHub Container Registry

Step 5: Deploy
SSH into VPS
docker compose pull
docker compose up -d

Step 6: Health Checks
Verify: API, Database, Redis, Dashboard

---

# RELEASE STRATEGY
Development
↓
Staging
↓
Production
Never deploy directly to Production.

---

# DISASTER RECOVERY
Server Failure
Provision new VPS -> Restore latest backup -> Deploy containers
Recovery Target: Less than 1 hour

---

# SECURITY
HTTPS
Nginx Reverse Proxy
Fail2Ban
Firewall
SSH Keys Only
No Password Login
Encrypted Secrets
Environment Variables
Database Not Exposed Publicly
Redis Not Exposed Publicly

---

# DEPLOYMENT PHASES
MONTH 1 DEPLOYMENT: Laptop, Python, MySQL, Docker
MONTH 3 DEPLOYMENT: AWS Lightsail, Docker Compose, Single Server
MONTH 6 DEPLOYMENT: Add Prometheus, Grafana, Automated Backups, Telegram Alerts
MONTH 12 DEPLOYMENT: RDS MySQL, Redis Cluster, Load Balancer, Blue-Green Deployments, Multi-Region Backup