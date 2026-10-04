#!/usr/bin/env bash
#
# deploy.sh — build the RecipeMate web client and publish it to S3 + CloudFront.
#
# Steps:
#   1. Build the SPA (npm run build -> frontend/dist).
#   2. Sync the build to the S3 bucket, deleting removed objects (--delete).
#   3. Invalidate the CloudFront distribution so viewers get the new bundle.
#
# This is a manual deploy helper; CI (.github/workflows/deploy-frontend.yml)
# runs the equivalent steps on merge to main. It requires AWS credentials in
# the environment (via `aws configure`, SSO, or an assumed role) with
# permission to write the bucket and create CloudFront invalidations.
#
# Usage:
#   BUCKET=my-web-bucket \
#   DIST_ID=E1234567890ABC \
#   AWS_REGION=us-east-1 \
#     ./frontend/scripts/deploy.sh
#
# The bucket name and distribution id come from the WebStack CloudFormation
# outputs (RecipeMate-<Env>-Web: BucketName, DistributionId). Run this from the
# repo root or anywhere — paths are resolved relative to this script.
#
# Required environment variables:
#   BUCKET      S3 bucket name (WebStack `BucketName` output).
#   DIST_ID     CloudFront distribution id (WebStack `DistributionId` output).
# Optional:
#   AWS_REGION  AWS region (falls back to the ambient AWS CLI configuration).

set -euo pipefail

# Resolve the frontend directory relative to this script so the build output
# path is correct regardless of the caller's working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${FRONTEND_DIR}/dist"

: "${BUCKET:?Set BUCKET to the target S3 bucket name (WebStack BucketName output)}"
: "${DIST_ID:?Set DIST_ID to the CloudFront distribution id (WebStack DistributionId output)}"

# Build an array of extra args so an unset AWS_REGION doesn't pass an empty flag.
region_args=()
if [[ -n "${AWS_REGION:-}" ]]; then
  region_args=(--region "${AWS_REGION}")
fi

echo "==> Building the web client (npm run build)"
npm --prefix "${FRONTEND_DIR}" run build

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "error: build output not found at ${DIST_DIR}" >&2
  exit 1
fi

echo "==> Syncing ${DIST_DIR} -> s3://${BUCKET} (with --delete)"
aws s3 sync "${DIST_DIR}" "s3://${BUCKET}" --delete "${region_args[@]}"

echo "==> Invalidating CloudFront distribution ${DIST_ID}"
aws cloudfront create-invalidation \
  --distribution-id "${DIST_ID}" \
  --paths "/*" \
  "${region_args[@]}"

echo "==> Done. New build is live once the invalidation completes."
