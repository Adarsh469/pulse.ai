# ── Makefile — ClinicalTriage-Env deployment targets ─────────────────────────
# Supports local builds and AWS ECR image publishing.
# Set ECR_REGISTRY + AWS_REGION before running ECR targets.
#
# Quick start (ECR):
#   export ECR_REGISTRY=123456789012.dkr.ecr.us-east-1.amazonaws.com
#   export AWS_REGION=us-east-1
#   make ecr-login build push deploy

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Configuration ──────────────────────────────────────────────────────────────
APP_NAME     ?= triage
IMAGE_TAG    ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo "latest")
AWS_REGION   ?= us-east-1
ECR_REGISTRY ?=  # e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com

BACKEND_IMAGE  = $(APP_NAME)-backend
FRONTEND_IMAGE = $(APP_NAME)-frontend

BACKEND_ECR  = $(ECR_REGISTRY)/$(BACKEND_IMAGE)
FRONTEND_ECR = $(ECR_REGISTRY)/$(FRONTEND_IMAGE)

COMPOSE      = docker compose
COMPOSE_AWS  = docker compose -f docker-compose.yml -f docker-compose.override.aws.yml

# ── Help ───────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Local development ──────────────────────────────────────────────────────────
.PHONY: up down restart logs ps
up: ## Start all services locally (no AWS overrides)
	$(COMPOSE) up -d --build

down: ## Stop all services
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

logs: ## Tail logs for all services
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

# ── Docker build (local tags) ──────────────────────────────────────────────────
.PHONY: build
build: ## Build backend and frontend images locally
	docker build -f Dockerfile.backend -t $(BACKEND_IMAGE):$(IMAGE_TAG) -t $(BACKEND_IMAGE):latest .
	docker build -f frontend/Dockerfile -t $(FRONTEND_IMAGE):$(IMAGE_TAG) -t $(FRONTEND_IMAGE):latest ./frontend
	@echo "Built: $(BACKEND_IMAGE):$(IMAGE_TAG)  $(FRONTEND_IMAGE):$(IMAGE_TAG)"

# ── AWS ECR ────────────────────────────────────────────────────────────────────
.PHONY: ecr-login ecr-create push deploy-aws

ecr-login: ## Authenticate Docker to ECR
	@test -n "$(ECR_REGISTRY)" || (echo "ERROR: ECR_REGISTRY is not set" && exit 1)
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_REGISTRY)

ecr-create: ## Create ECR repositories (run once)
	@test -n "$(ECR_REGISTRY)" || (echo "ERROR: ECR_REGISTRY is not set" && exit 1)
	aws ecr create-repository --repository-name $(BACKEND_IMAGE)  --region $(AWS_REGION) 2>/dev/null || true
	aws ecr create-repository --repository-name $(FRONTEND_IMAGE) --region $(AWS_REGION) 2>/dev/null || true
	@echo "ECR repositories ready."

push: build ecr-login ## Build and push images to ECR
	docker tag $(BACKEND_IMAGE):$(IMAGE_TAG)  $(BACKEND_ECR):$(IMAGE_TAG)
	docker tag $(BACKEND_IMAGE):$(IMAGE_TAG)  $(BACKEND_ECR):latest
	docker tag $(FRONTEND_IMAGE):$(IMAGE_TAG) $(FRONTEND_ECR):$(IMAGE_TAG)
	docker tag $(FRONTEND_IMAGE):$(IMAGE_TAG) $(FRONTEND_ECR):latest
	docker push $(BACKEND_ECR):$(IMAGE_TAG)
	docker push $(BACKEND_ECR):latest
	docker push $(FRONTEND_ECR):$(IMAGE_TAG)
	docker push $(FRONTEND_ECR):latest
	@echo "Pushed to ECR: tag=$(IMAGE_TAG)"

deploy-aws: ## Pull ECR images on EC2 and restart services (run on EC2)
	@test -n "$(ECR_REGISTRY)" || (echo "ERROR: ECR_REGISTRY is not set" && exit 1)
	ECR_REGISTRY=$(ECR_REGISTRY) IMAGE_TAG=$(IMAGE_TAG) \
	  $(COMPOSE_AWS) pull
	ECR_REGISTRY=$(ECR_REGISTRY) IMAGE_TAG=$(IMAGE_TAG) \
	  $(COMPOSE_AWS) up -d --remove-orphans
	$(COMPOSE_AWS) ps

# ── Secrets ────────────────────────────────────────────────────────────────────
.PHONY: load-secrets
load-secrets: ## Fetch secrets from AWS SSM and write .env (run as root on EC2)
	sudo bash deploy/load-secrets.sh

# ── SSL ────────────────────────────────────────────────────────────────────────
.PHONY: ssl-init ssl-activate
ssl-init: ## Issue initial Certbot certificate (set DOMAIN and EMAIL first)
	@test -n "$(DOMAIN)" || (echo "ERROR: DOMAIN is not set. Run: make ssl-init DOMAIN=example.com EMAIL=you@example.com" && exit 1)
	@test -n "$(EMAIL)"  || (echo "ERROR: EMAIL is not set" && exit 1)
	$(COMPOSE_AWS) run --rm certbot certonly \
	  --webroot -w /var/www/certbot \
	  --email $(EMAIL) \
	  --agree-tos --no-eff-email \
	  -d $(DOMAIN)
	@echo "Certificate issued. Run: make ssl-activate DOMAIN=$(DOMAIN)"

ssl-activate: ## Swap in HTTPS nginx config and reload
	@test -n "$(DOMAIN)" || (echo "ERROR: DOMAIN is not set" && exit 1)
	sed "s/YOUR_DOMAIN/$(DOMAIN)/g" nginx/nginx.ssl.conf > nginx/nginx.conf
	$(COMPOSE_AWS) exec nginx nginx -t
	$(COMPOSE_AWS) exec nginx nginx -s reload
	@echo "HTTPS enabled for $(DOMAIN)"

# ── Systemd ────────────────────────────────────────────────────────────────────
.PHONY: install-service
install-service: ## Install and enable the triage systemd service (run as root)
	sudo cp deploy/triage.service /etc/systemd/system/triage-compose.service
	sudo sed -i "s|/opt/triage|$(shell pwd)|g" /etc/systemd/system/triage-compose.service
	sudo systemctl daemon-reload
	sudo systemctl enable triage-compose
	sudo systemctl start  triage-compose
	sudo systemctl status triage-compose

# ── Cleanup ────────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove stopped containers and dangling images
	docker compose down --remove-orphans
	docker image prune -f
