# RecipeMate

RecipeMate is a Python/Flask API for home cooks who want a searchable digital cookbook built from recipe photos and screenshots. Upload an image and GPT-4o extracts structured data automatically — ingredients, steps, cook time, and tags — so you can search and browse your collection by what you have on hand, filter by cook time or tools, and keep a personal log of every time you make a dish.

## Features

- Upload a recipe photo or screenshot; async GPT-4o extraction saves structured data
- Natural-language search ("I have chicken and garlic — what can I make?")
- Filter/browse by ingredient, cook time, tags, and tools
- Cook logs with notes, photos, and star ratings

## Quick Start

```bash
git clone <repo>
cd recipemate
cp .env.example .env
# edit .env — see docs/configuration.md for details
docker-compose up
# run migrations once containers are up:
docker-compose exec api alembic upgrade head
# verify:
curl http://localhost:5000/health
```

## Documentation

- [Architecture](docs/architecture.md)
- [API Reference](docs/api.md)
- [Data Model](docs/data-model.md)
- [Local Development](docs/local-development.md)
- [Configuration Reference](docs/configuration.md)

## Tech Stack

Flask, PostgreSQL, Redis, S3/CloudFront, SQS + Lambda, OpenAI GPT-4o, Amazon Cognito, AWS CDK (Python)

## License

MIT
