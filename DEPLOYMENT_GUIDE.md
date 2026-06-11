# Falcon Quant Platform - Deployment Guide

## Prerequisites

### 1. VPS Setup
- Ubuntu 24.04 LTS
- 4 vCPU, 8GB RAM, 100GB SSD
- Docker and Docker Compose installed
- SSH access configured

### 2. GitHub Secrets (Required)

Add these secrets to your GitHub repository (`Settings > Secrets and variables > Actions`):

```
DEPLOY_HOST              → IP or domain of your VPS
DEPLOY_USER              → SSH username (e.g., ubuntu)
DEPLOY_SSH_KEY           → Private SSH key for authentication
SLACK_WEBHOOK            → Slack webhook URL for notifications (optional)
```

#### Getting SSH Key for GitHub

```bash
# On your local machine
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""

# Copy public key to VPS
ssh-copy-id -i ~/.ssh/deploy_key.pub username@your-vps-ip

# Add private key to GitHub Secrets (entire contents of deploy_key)
cat ~/.ssh/deploy_key
```

---

## VPS Setup Instructions

### Step 1: Install Docker and Docker Compose

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose git

# Add user to docker group
sudo usermod -aG docker $USER
sudo newgrp docker
```

### Step 2: Create Application Directory

```bash
mkdir -p ~/falcon-quant
cd ~/falcon-quant
```

### Step 3: Clone Repository (First Time)

```bash
git clone https://github.com/YOUR-USERNAME/Trading.git .
```

### Step 4: Create Environment File

```bash
cat > .env << EOF
# Database
DATABASE_URL=mysql+pymysql://root:your_secure_password@mysql:3306/falcon_db
MYSQL_ROOT_PASSWORD=your_secure_password
MYSQL_DATABASE=falcon_db

# Redis
REDIS_URL=redis://redis:6379

# API Configuration
JWT_SECRET=your_jwt_secret_key_here
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8000

# Zerodha Configuration (Optional - for live trading)
ZERODHA_API_KEY=your_api_key
ZERODHA_API_SECRET=your_api_secret

# Application Settings
ENVIRONMENT=production
DEBUG=False
EOF
```

### Step 5: Create docker-compose override (Optional - Production)

```bash
cat > docker-compose.override.yml << EOF
version: '3.8'

services:
  api:
    image: ghcr.io/YOUR-USERNAME/Trading/api:latest
    environment:
      - ENVIRONMENT=production
      - DEBUG=False
    restart: always

  mysql:
    restart: always
    volumes:
      - mysql_data:/var/lib/mysql

  redis:
    restart: always
    volumes:
      - redis_data:/data

  nginx:
    restart: always
EOF
```

### Step 6: Initial Deployment

```bash
cd ~/falcon-quant

# Pull latest images and start services
docker-compose pull
docker-compose up -d

# Run migrations
docker-compose exec api python -m alembic upgrade head

# Verify services
docker-compose ps
curl http://localhost:8000/api/v1/health
```

---

## CI/CD Pipeline Workflow

### Trigger: Push to Main Branch

1. **Lint & Test** (Runs on every commit)
   - Ruff linting
   - Black formatting check
   - Pytest unit tests

2. **Build** (Only if tests pass)
   - Build Docker image
   - Push to GitHub Container Registry (GHCR)
   - Tag with commit SHA

3. **Deploy** (Only if build succeeds, on main branch)
   - SSH into VPS
   - Pull latest code
   - Pull latest Docker images
   - Start/restart services
   - Wait 5 seconds for startup

4. **Health Checks** (After deployment)
   - Check API /health endpoint
   - Retry up to 30 times (60 seconds total)
   - Verify database connection

5. **Notifications** (Slack)
   - Success: Deployment successful message
   - Failure: Deployment failed message

6. **Rollback** (Only if deploy fails)
   - SSH into VPS
   - Revert git to previous commit
   - Restart services with previous version

---

## Manual Deployment (Without CI/CD)

If you need to deploy manually:

```bash
# On VPS
cd ~/falcon-quant
git pull origin main
docker-compose pull
docker-compose up -d
sleep 5

# Check health
curl http://localhost:8000/api/v1/health

# View logs
docker-compose logs -f api
```

---

## Environment Variables Reference

### Database
- `DATABASE_URL` - MySQL connection string
- `MYSQL_ROOT_PASSWORD` - MySQL root password
- `MYSQL_DATABASE` - Database name

### Redis
- `REDIS_URL` - Redis connection string

### API
- `JWT_SECRET` - Secret key for JWT tokens (minimum 32 characters)
- `LOG_LEVEL` - DEBUG | INFO | WARNING | ERROR
- `API_HOST` - Bind address (default: 0.0.0.0)
- `API_PORT` - Bind port (default: 8000)

### Zerodha (Optional)
- `ZERODHA_API_KEY` - API key from Zerodha
- `ZERODHA_API_SECRET` - API secret from Zerodha

### Application
- `ENVIRONMENT` - development | staging | production
- `DEBUG` - True/False (must be False in production)

---

## Monitoring & Logs

### View Docker Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f api
docker-compose logs -f mysql

# Last 50 lines
docker-compose logs --tail=50
```

### Health Check Endpoint

```bash
curl http://your-vps-ip:8000/api/v1/health

# Response:
# {
#   "status": "UP",
#   "database": "UP",
#   "redis": "UP"
# }
```

### Docker Compose Commands

```bash
# Start services
docker-compose up -d

# Stop services
docker-compose down

# Restart a service
docker-compose restart api

# View running containers
docker-compose ps

# Execute command in container
docker-compose exec api python -m pytest tests/

# View resource usage
docker stats
```

---

## Troubleshooting

### Deployment Fails with "Health Check Failed"

**Cause:** API not responding
**Solution:**
```bash
# Check logs
docker-compose logs api

# Verify database connection
docker-compose exec api python -c "import sqlalchemy; print('DB OK')"

# Restart service
docker-compose restart api
```

### Database Connection Error

**Cause:** MySQL not ready
**Solution:**
```bash
# Wait for MySQL
docker-compose logs mysql

# Restart MySQL
docker-compose restart mysql

# Run migrations
docker-compose exec api python -m alembic upgrade head
```

### Docker Image Not Found

**Cause:** GitHub Container Registry authentication issue
**Solution:**
```bash
# Login to GHCR
echo $CR_PAT | docker login ghcr.io -u USERNAME --password-stdin

# Pull manually
docker pull ghcr.io/YOUR-USERNAME/Trading/api:latest
```

### Port Already in Use

**Cause:** Another service using port 3307 or 8000
**Solution:**
```bash
# Find process using port
lsof -i :3307
lsof -i :8000

# Kill process (not recommended for production)
kill -9 <PID>

# Or change port in docker-compose.yml
```

---

## Security Checklist

- [ ] Change all default passwords in `.env`
- [ ] Use strong JWT_SECRET (32+ characters)
- [ ] Enable SSH key-only authentication
- [ ] Configure firewall rules
- [ ] Regular backups of database
- [ ] Update Docker images regularly
- [ ] Monitor logs for errors
- [ ] Set up Slack alerts

---

## Backup Strategy

### Automated Backups (Recommended)

```bash
# Create backup script: ~/backup-db.sh
#!/bin/bash
BACKUP_DIR="/home/$USER/backups"
mkdir -p $BACKUP_DIR
docker-compose exec -T mysql mysqldump -u root -p$MYSQL_ROOT_PASSWORD falcon_db > $BACKUP_DIR/falcon_db_$(date +%Y%m%d_%H%M%S).sql

# Add to crontab for daily backup at 2 AM
# 0 2 * * * ~/backup-db.sh
```

### Manual Backup

```bash
# Backup database
docker-compose exec -T mysql mysqldump -u root -p$MYSQL_ROOT_PASSWORD falcon_db > backup.sql

# Restore database
docker-compose exec -T mysql mysql -u root -p$MYSQL_ROOT_PASSWORD falcon_db < backup.sql
```

---

## Next Steps

1. Set up GitHub Secrets
2. Prepare VPS with Docker
3. Push to main branch
4. Monitor first deployment in GitHub Actions
5. Set up Slack notifications
6. Configure backups
7. Monitor application logs regularly

For questions or issues, check the logs and GitHub Actions workflow details.
