# GitHub Actions Secrets Setup Checklist

## 📋 Secrets to Configure

Go to your GitHub repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add the following secrets:

### 1. DEPLOY_HOST
**Type:** Text  
**Description:** IP address or domain of your VPS  
**Example:** `192.168.1.100` or `api.falcon-quant.com`

```bash
# Get your VPS IP
echo "Your VPS IP: $(hostname -I)"
```

---

### 2. DEPLOY_USER
**Type:** Text  
**Description:** SSH username on your VPS  
**Example:** `ubuntu` or `ec2-user`

```bash
# Get your VPS username
whoami
```

---

### 3. DEPLOY_SSH_KEY
**Type:** Text (Multiline)  
**Description:** Private SSH key for authentication  
**⚠️ IMPORTANT: Keep this secret!**

#### Generate SSH Key (if you don't have one)

```bash
# On your local machine
ssh-keygen -t ed25519 -f ~/.ssh/falcon_deploy -N ""

# This creates:
# ~/.ssh/falcon_deploy (PRIVATE KEY - add to GitHub)
# ~/.ssh/falcon_deploy.pub (PUBLIC KEY - add to VPS)
```

#### Add Public Key to VPS

```bash
# Option 1: Using ssh-copy-id (easiest)
ssh-copy-id -i ~/.ssh/falcon_deploy.pub username@your-vps-ip

# Option 2: Manual
ssh username@your-vps-ip
mkdir -p ~/.ssh
cat >> ~/.ssh/authorized_keys << EOF
<paste contents of ~/.ssh/falcon_deploy.pub>
EOF
chmod 600 ~/.ssh/authorized_keys
```

#### Add Private Key to GitHub

```bash
# Display the private key
cat ~/.ssh/falcon_deploy

# Copy the entire output (including BEGIN and END lines)
# Paste into GitHub secret DEPLOY_SSH_KEY
```

**Expected format:**
```
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUtbm9uZQAAAAgAAAAgeF...
... (many lines) ...
-----END OPENSSH PRIVATE KEY-----
```

---

### 4. SLACK_WEBHOOK (Optional)
**Type:** Text  
**Description:** Slack webhook for deployment notifications  

#### Get Slack Webhook URL

1. Go to https://api.slack.com/apps
2. Create New App → From scratch
3. Name: "Falcon Quant Deployments"
4. Select your workspace
5. Go to **Incoming Webhooks**
6. Click **Add New Webhook to Workspace**
7. Select your channel (e.g., #deployments)
8. Click **Allow**
9. Copy the webhook URL

**Expected format:**
```
https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX
```

---

## ✅ Verification Checklist

### Check GitHub Secrets are Set

```bash
# View secret names (not values!)
gh secret list
```

**Expected output:**
```
DEPLOY_HOST              Updated 2026-06-11  
DEPLOY_SSH_KEY           Updated 2026-06-11  
DEPLOY_USER              Updated 2026-06-11  
SLACK_WEBHOOK            Updated 2026-06-11  
```

### Test SSH Connection (Before First Deploy)

```bash
# On your local machine
ssh -i ~/.ssh/falcon_deploy username@your-vps-ip

# Should connect without password
# If it prompts for password, your key isn't set up correctly
```

### Test VPS Readiness

```bash
# SSH into VPS
ssh -i ~/.ssh/falcon_deploy username@your-vps-ip

# Check Docker is installed
docker --version
docker-compose --version

# Check directory exists
ls -la ~/falcon-quant/

# Exit
exit
```

---

## 🔄 CI/CD Pipeline Flow

When you **push to main** branch:

```
Push → Lint & Test → Build Image → Deploy → Health Checks → Notify Slack
                                                    ↓
                                          If fails → Rollback
```

### Monitor Deployment

1. Go to **GitHub** → **Actions** tab
2. Click on the workflow run
3. Watch the logs in real-time
4. Check Slack for notifications

---

## 🚨 Troubleshooting

### "Permission denied (publickey)"
**Cause:** SSH key not set up correctly  
**Solution:**
```bash
# Verify public key is on VPS
ssh -i ~/.ssh/falcon_deploy username@your-vps-ip
grep "$(cat ~/.ssh/falcon_deploy.pub)" ~/.ssh/authorized_keys

# Should show your public key
```

### "Deployment failed - Health check failed"
**Cause:** API not starting properly  
**Solution:**
```bash
# SSH to VPS and check logs
ssh -i ~/.ssh/falcon_deploy username@your-vps-ip
cd ~/falcon-quant
docker-compose logs api
```

### Secrets not being used
**Cause:** Typo in secret name  
**Solution:**
- Check GitHub Actions job to see exact variable names used
- Secrets must match exactly (case-sensitive)
- Remove and re-add if unsure

---

## 🔐 Security Best Practices

1. **Never commit** `.env` or SSH keys to GitHub
2. **Rotate SSH keys** every 90 days
3. **Use strong passwords** (32+ characters for JWT_SECRET)
4. **Limit SSH key permissions:** `chmod 600 ~/.ssh/deploy_key`
5. **Restrict SSH access** via firewall to GitHub IPs
6. **Monitor deployment logs** for suspicious activity
7. **Keep secrets confidential** - don't share in public repos

---

## 🆘 Still Having Issues?

Check these in order:

1. Run `./scripts/vps-setup.sh` on your VPS
2. Verify all secrets are set in GitHub
3. Test SSH manually: `ssh -i ~/.ssh/falcon_deploy user@host`
4. Check VPS logs: `docker-compose logs`
5. Review GitHub Actions logs for detailed error messages

---

## 📝 Secret Template (Copy & Paste)

```
DEPLOY_HOST=
DEPLOY_USER=
DEPLOY_SSH_KEY=
SLACK_WEBHOOK=
```

Fill in and add each as a separate secret in GitHub.

---

## 📊 Deployment Status

After pushing to main:

✅ **Expected Sequence:**
- Lint & Test (2-3 minutes)
- Build Docker Image (3-5 minutes)
- Deploy to VPS (1-2 minutes)
- Health Checks (1 minute)
- Slack Notification

**Total Time:** ~7-15 minutes for first deployment

---

**Last Updated:** 2026-06-11  
**CI/CD Version:** 1.0  
**Status:** Production Ready
