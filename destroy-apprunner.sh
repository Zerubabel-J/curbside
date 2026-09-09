#!/usr/bin/env bash
# Remove everything this demo created. Run it when John is done looking.
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
NAME="${APP_NAME:-curbside}"

ARN=$(aws apprunner list-services --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${NAME}'].ServiceArn" --output text)
[ -n "$ARN" ] && {
  echo "deleting service…"
  aws apprunner delete-service --service-arn "$ARN" --region "$REGION" >/dev/null
}

aws ecr delete-repository --repository-name "$NAME" --force --region "$REGION" >/dev/null 2>&1 || true
aws secretsmanager delete-secret --secret-id "${NAME}/gemini" \
  --force-delete-without-recovery --region "$REGION" >/dev/null 2>&1 || true
aws iam delete-role-policy --role-name "${NAME}-task" --policy-name read-secret 2>/dev/null || true
aws iam delete-role --role-name "${NAME}-task" 2>/dev/null || true
aws iam detach-role-policy --role-name "${NAME}-ecr-access" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess 2>/dev/null || true
aws iam delete-role --role-name "${NAME}-ecr-access" 2>/dev/null || true

echo "done - billing stops once the service finishes deleting."
