# Deployment Guide — ClinicalTriage-Env

Single EC2 instance, Docker Compose, Nginx reverse proxy. No ECS, no Kubernetes.

---

## Prerequisites

| Requirement | Version | Install |
|------------|---------|---------|
| Ubuntu | 22.04 LTS | — |
| Docker Engine | 24.x+ | [docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/) |
| Docker Compose | v2.20+ | Included with Docker Engine |
| AWS CLI v2 | Latest | [docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Open ports | 22 (SSH), 80 (HTTP), 443 (HTTPS) | EC2 Security Group |

---

## EC2 Instance Setup (One-Time)

### 1. Launch EC2 instance

- **AMI**: Ubuntu Server 22.04 LTS
- **Instance type**: `t3.medium` or larger (frontend build needs ~1.5 GB RAM)
- **Storage**: 20 GB gp3 EBS root volume
- **IAM role**: Attach a role with the following policies:
  - `AmazonSSMReadOnlyAccess` (for secret loading via `load-secrets.sh`)
  - A custom inline policy for CloudWatch:
    ```json
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:log-group:/triage/*"
    }
    ```
  - `AmazonEC2ContainerRegistryReadOnly` (to pull from ECR)
- **Security Group inbound rules**:
  - Port 22 from your IP
  - Port 80 from 0.0.0.0/0
  - Port 443 from 0.0.0.0/0

### 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu && newgrp docker
```

### 3. Clone the repository

```bash
sudo mkdir -p /opt/triage
sudo chown ubuntu:ubuntu /opt/triage
git clone https://github.com/Adarsh469/pulse.ai.git /opt/triage
cd /opt/triage
```

---

## Basic Deployment (Build on EC2)

```bash
cd /opt/triage

# Configure environment
cp .env.example .env
nano .env   # set HF_TOKEN if needed

# Build and start
docker compose up -d --build

# Verify
docker compose ps
curl http://localhost/api/health   # → {"status":"ok"}
```

Open `http://<EC2-PUBLIC-IP>/` in your browser.

---

## Production Deployment (ECR Images — Recommended)

Building images on-instance is slow. Push from a dev machine, pull on EC2.

### On your dev machine

```bash
export ECR_REGISTRY=<account-id>.dkr.ecr.us-east-1.amazonaws.com
export AWS_REGION=us-east-1
export IMAGE_TAG=$(git rev-parse --short HEAD)

make ecr-create   # create repos (once)
make push         # build + tag + push to ECR
```

### On EC2

```bash
cd /opt/triage

# Set ECR vars in .env
echo "ECR_REGISTRY=<account-id>.dkr.ecr.us-east-1.amazonaws.com" >> .env
echo "IMAGE_TAG=<same-git-sha>"                                   >> .env
echo "AWS_REGION=us-east-1"                                       >> .env

# Pull and start (no build on this machine)
make deploy-aws
```

---

## Load Secrets from SSM (Recommended)

Store `HF_TOKEN` in SSM instead of plain text in `.env`:

```bash
# Store in SSM (run from dev machine with IAM permissions)
aws ssm put-parameter \
  --name "/triage/HF_TOKEN" \
  --value "hf_YOUR_TOKEN_HERE" \
  --type "SecureString" \
  --region us-east-1

# On EC2: load secrets into .env
cd /opt/triage
make load-secrets
# Then start/restart the stack
docker compose up -d
```

---

## Enable HTTPS with Certbot (Let's Encrypt)

> Requires a domain name pointed at your EC2's Elastic IP.

### 1. Point your domain at the EC2 instance

Assign an **Elastic IP** to the instance and create an A record.

### 2. Start stack in HTTP mode first

```bash
docker compose -f docker-compose.yml -f docker-compose.override.aws.yml up -d
```

### 3. Issue the certificate

```bash
make ssl-init DOMAIN=yourdomain.com EMAIL=you@example.com
```

### 4. Activate HTTPS nginx config

```bash
make ssl-activate DOMAIN=yourdomain.com
```

This replaces `nginx/nginx.conf` with the HTTPS config and reloads nginx. The Certbot container renews automatically every 12 hours.

---

## CloudWatch Logging (AWS Deployment)

Use the AWS-specific compose override to ship logs to CloudWatch:

```bash
cd /opt/triage
docker compose -f docker-compose.yml -f docker-compose.override.aws.yml up -d --build
```

Log groups created automatically (IAM role required):
- `/triage/backend`
- `/triage/frontend`
- `/triage/nginx`

View in AWS Console → CloudWatch → Log Groups → `/triage/*`

---

## Auto-Start on EC2 Reboot (Systemd)

```bash
cd /opt/triage
make install-service
```

Verify:
```bash
sudo systemctl status triage-compose
```

After this, Docker Compose starts automatically on every instance boot/reboot.

---

## Updating the Application

```bash
cd /opt/triage
git pull
# For ECR-based deploys:
make push IMAGE_TAG=$(git rev-parse --short HEAD)   # from dev machine
make deploy-aws IMAGE_TAG=<sha>                      # on EC2

# For on-instance builds:
docker compose up -d --build
```

---

## Stopping and Restarting

```bash
make down      # stop (preserves volumes)
make restart   # restart all services
make logs      # tail all logs
make ps        # container status
```

---

## Troubleshooting

### Containers not starting

```bash
docker compose ps
docker compose logs
```

### Backend unhealthy

```bash
docker compose exec backend curl -f http://localhost:7860/health
docker compose logs backend
```

### 502 Bad Gateway

Wait 30–60 seconds. Nginx retries until backend+frontend are healthy.

```bash
docker inspect triage-backend | grep -A3 '"Health"'
docker inspect triage-frontend | grep -A3 '"Health"'
```

### Certbot renewal failing

```bash
docker compose -f docker-compose.yml -f docker-compose.override.aws.yml logs certbot
```

### CloudWatch logs not appearing

Verify the IAM role has `logs:PutLogEvents` and the EC2 instance profile is attached:

```bash
curl -s http://169.254.169.254/latest/meta-data/iam/info | python3 -m json.tool
```

---

## Architecture

```
Internet
    │
    ▼ :80 / :443
  Nginx (triage-nginx)       ← single entry point
    ├─── /api/* ──► Backend (triage-backend) :7860   FastAPI + uvicorn
    └─── /*      ──► Frontend (triage-frontend) :3000  Next.js standalone
                          │
                          └─ server-side rewrites /api/* → backend:7860
```

---

## Remaining Limitations

| Limitation | Recommendation |
|-----------|---------------|
| Sessions in-memory | Backend restart clears active sessions |
| Single uvicorn worker | Increase `--workers` or use gunicorn for load |
| No log rotation | Add Docker `json-file` log-opt `max-size` |
| CORS `allow_origins=["*"]` | Restrict to your domain in `server.py` |
