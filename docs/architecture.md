This document describes the system architecture, AWS services, deployment environments, async extraction flow, and LLM search design for RecipeMate.

## System Architecture

```
┌─────────────────┐     ┌─────────────────────┐
│   iOS App       │     │   Web Frontend      │
│   (SwiftUI)     │     │   (S3 + CloudFront) │
└────────┬────────┘     └──────────┬──────────┘
         │                         │
         └────────────┬────────────┘
                      │ HTTPS / REST
         ┌────────────▼────────────┐
         │   API Gateway           │
         │   (REST API)            │
         └────────────┬────────────┘
                      │
         ┌────────────▼────────────┐
         │   ECS Fargate           │
         │   (Flask REST API)      │
         └──────┬──────────┬───────┘
                │          │
    ┌───────────▼──┐   ┌───▼──────────────┐
    │  RDS Postgres│   │  S3              │
    │  (primary DB)│   │  (images)        │
    └───────────┬──┘   └──────────────────┘
                │
    ┌───────────▼──────────────┐
    │  ElastiCache Redis       │
    │  (search result cache,   │
    │   session store)         │
    └──────────────────────────┘
                │
    ┌───────────▼──────────────┐
    │  SQS + Lambda            │
    │  (async image extraction)│
    └───────────┬──────────────┘
                │
    ┌───────────▼──────────────┐
    │  OpenAI GPT-4o Vision    │
    │  (recipe extraction)     │
    └──────────────────────────┘
                │
    ┌───────────▼──────────────┐
    │  SNS → APNs              │
    │  (iOS push notifications)│
    └──────────────────────────┘
```

Amazon Cognito handles JWT auth for all API calls and the iOS share extension.

## AWS Services

| Service | Purpose |
| --- | --- |
| ECS Fargate | Runs Flask API container, auto-scales |
| RDS Postgres (t4g.micro) | Primary relational database |
| S3 | Recipe source images, cook log photos, thumbnails |
| CloudFront | CDN for S3 images and web frontend |
| API Gateway | HTTPS entry point, rate limiting |
| SQS | Async queue for image extraction jobs |
| Lambda | Processes SQS messages, calls vision API |
| SNS | Push notifications to iOS via APNs |
| ElastiCache Redis | Search result cache, session store |
| Cognito | User auth, JWT tokens |
| CDK (Python) | Infrastructure as code |

## Environments

| Environment | Notes |
| --- | --- |
| local | Docker Compose + LocalStack for AWS mocks |
| staging | Full AWS stack, small instance sizes |
| prod | Full AWS stack, auto-scaling enabled |

## Async Extraction Flow

1. Client calls POST /recipes/upload → receives a presigned S3 PUT URL (15-min TTL).
2. Client uploads image directly to S3 (bypasses the API server).
3. Client calls POST /recipes/extract with the returned S3 key.
4. API creates a recipe record with extraction_status='pending' and enqueues an SQS message with the S3 key and recipe ID.
5. Lambda picks up the SQS message, downloads the image, sends it to GPT-4o vision with a structured extraction prompt, and parses the response into ingredients, tools, instructions, and timing.
6. Lambda writes extracted data to the recipe record and sets extraction_status='complete' (or 'failed' on error).
7. Lambda publishes an SNS message routed through APNs to the user's iOS device.
8. Client polls GET /recipes/{id}/extraction-status or receives the push notification.

## LLM Search Design

The user submits a natural-language query to POST /search/llm. The API builds a lightweight index of the user's recipes (title, ingredient names, tags) and sends it with the query to GPT-4, which ranks and explains matches.

Results are returned as a list of recipe IDs with a match_explanation for each. Results are cached in Redis keyed by {user_id}:{sha256(query)} with a 5-minute TTL. If the LLM call fails, the endpoint falls back to heuristic filter search.
