# Local Development

This guide walks through setting up and running RecipeMate on your local machine using Docker Compose and LocalStack.

## Prerequisites

- Docker 24+ and Docker Compose v2 (`docker compose version` to verify)
- Python 3.11+ (only needed to run tests or Alembic outside Docker)
- An OpenAI API key (required for extraction and LLM search)
- AWS CLI (optional, for inspecting LocalStack resources)

## Setup

### 1. Clone and configure

```bash
git clone <repo-url>
cd recipemate
cp .env.example .env
```

Open .env and update:

- SECRET_KEY: generate with `python -c "import secrets; print(secrets.token_hex(32))"`
- OPENAI_API_KEY: your key from https://platform.openai.com/api-keys
- All other values can stay at their defaults (they point at Docker Compose services and LocalStack).

See docs/configuration.md for a full description of every variable.

### 2. Start services

```bash
docker-compose up
```

This starts four containers: api (Flask on port 5000), db (PostgreSQL 15 on 5432), redis (Redis 7 on 6379), localstack (port 4566, mocks S3/SQS/SNS).

### 3. Run database migrations

Once the api container logs `Running on http://0.0.0.0:5000`:

```bash
docker-compose exec api alembic upgrade head
```

Applies all migrations in migrations/versions/ in order.

To create a new migration after changing a model:

```bash
docker-compose exec api alembic revision --autogenerate -m "describe the change"
```

### 4. Verify the health check

```bash
curl http://localhost:5000/health
# Expected: {"status": "ok"}
```

### 5. Run tests

Inside Docker (recommended):

```bash
docker-compose exec api pytest
```

Local virtual environment (requires Postgres and Redis running):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

### 6. LocalStack setup notes

LocalStack mocks S3, SQS, and SNS. The app reads AWS_ENDPOINT_URL=http://localstack:4566 and redirects all SDK calls there.

LocalStack does not persist resources between restarts. Run these after docker-compose up:

```bash
# S3 bucket
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  aws --endpoint-url=http://localhost:4566 s3 mb s3://recipemate-images

# SQS extraction queue
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name recipemate-extraction

# SNS notifications topic
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  aws --endpoint-url=http://localhost:4566 sns create-topic --name recipemate-notifications
```

## Useful commands

| Task | Command |
| --- | --- |
| Start services | `docker-compose up` |
| Start in background | `docker-compose up -d` |
| View API logs | `docker-compose logs -f api` |
| Stop all services | `docker-compose down` |
| Destroy volumes (fresh DB) | `docker-compose down -v` |
| Open Postgres shell | `docker-compose exec db psql -U recipemate recipemate` |
| Open Redis CLI | `docker-compose exec redis redis-cli` |
| Alembic migrate | `docker-compose exec api alembic upgrade head` |
| Run tests | `docker-compose exec api pytest` |
