#!/bin/bash
# VPS Setup Script for Falcon Quant Platform
# Run this once on your VPS to set up Docker and the application

set -e

echo "======================================"
echo "Falcon Quant Platform - VPS Setup"
echo "======================================"

# Check if running as non-root (recommended)
if [[ $EUID -eq 0 ]]; then
   echo "⚠️  Please do not run this script as root"
   echo "Run as a regular user with sudo privileges"
   exit 1
fi

echo "✅ Checking prerequisites..."

# Update system
echo "📦 Updating system packages..."
sudo apt-get update
sudo apt-get upgrade -y

# Install Docker
echo "🐳 Installing Docker..."
if ! command -v docker &> /dev/null; then
    sudo apt-get install -y docker.io docker-compose
    sudo usermod -aG docker $USER
    echo "✅ Docker installed"
else
    echo "✅ Docker already installed"
fi

# Install Git
echo "📝 Installing Git..."
if ! command -v git &> /dev/null; then
    sudo apt-get install -y git
    echo "✅ Git installed"
else
    echo "✅ Git already installed"
fi

# Install curl
echo "🔗 Installing curl..."
sudo apt-get install -y curl wget

# Create application directory
echo "📂 Creating application directory..."
APP_DIR="$HOME/falcon-quant"
mkdir -p $APP_DIR
cd $APP_DIR
echo "✅ Application directory: $APP_DIR"

# Clone repository (if not already cloned)
if [ ! -d ".git" ]; then
    echo "📥 Cloning repository..."
    git clone https://github.com/YOUR-USERNAME/Trading.git .
    echo "✅ Repository cloned"
else
    echo "✅ Repository already exists"
fi

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo "⚙️  Creating .env file..."
    cat > .env << 'EOF'
# Database Configuration
DATABASE_URL=mysql+pymysql://root:secure_password_123@mysql:3306/falcon_db
MYSQL_ROOT_PASSWORD=secure_password_123
MYSQL_DATABASE=falcon_db

# Redis Configuration
REDIS_URL=redis://redis:6379

# API Configuration
JWT_SECRET=your_32_character_jwt_secret_key_12345
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8000

# Application Environment
ENVIRONMENT=production
DEBUG=False

# Zerodha Configuration (Optional - for live trading)
ZERODHA_API_KEY=your_api_key_here
ZERODHA_API_SECRET=your_api_secret_here
EOF
    
    echo "⚠️  Please edit .env with your configuration:"
    echo "   nano .env"
    echo ""
else
    echo "✅ .env file already exists"
fi

# Create docker-compose override for production
if [ ! -f "docker-compose.override.yml" ]; then
    echo "⚙️  Creating docker-compose.override.yml..."
    cat > docker-compose.override.yml << 'EOF'
version: '3.8'

services:
  api:
    restart: always
    environment:
      - ENVIRONMENT=production
      - DEBUG=False

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
    echo "✅ Production override created"
else
    echo "✅ docker-compose.override.yml already exists"
fi

# Initialize Docker Compose
echo "🚀 Starting services..."
docker-compose pull
docker-compose up -d

# Wait for MySQL to be ready
echo "⏳ Waiting for MySQL to be ready..."
for i in {1..30}; do
    if docker-compose exec -T mysql mysqladmin ping -u root -p$MYSQL_ROOT_PASSWORD 2>/dev/null; then
        echo "✅ MySQL is ready"
        break
    fi
    echo "Attempt $i/30 - Waiting..."
    sleep 2
done

# Run migrations
echo "📊 Running database migrations..."
docker-compose exec api python -m alembic upgrade head

# Health check
echo "🏥 Checking health..."
sleep 3
if curl -f http://localhost:8000/api/v1/health; then
    echo "✅ All services are healthy"
else
    echo "⚠️  Health check failed - please check logs:"
    echo "   docker-compose logs api"
fi

echo ""
echo "======================================"
echo "✅ Setup completed successfully!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Update .env with your configuration:"
echo "   nano .env"
echo ""
echo "2. Configure GitHub Secrets:"
echo "   - DEPLOY_HOST: $HOSTNAME"
echo "   - DEPLOY_USER: $USER"
echo "   - DEPLOY_SSH_KEY: (your SSH private key)"
echo ""
echo "3. Set up SSH key for GitHub:"
echo "   ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N \"\""
echo "   ssh-copy-id -i ~/.ssh/deploy_key.pub $USER@localhost"
echo ""
echo "4. View logs:"
echo "   docker-compose logs -f"
echo ""
echo "5. Manage services:"
echo "   docker-compose restart api"
echo "   docker-compose down"
echo ""
