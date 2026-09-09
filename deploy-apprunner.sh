#!/usr/bin/env bash
# Deploy the API to AWS App Runner.
#
# App Runner over ECS+ALB for a short demo: no load balancer to pay for
# (~$16/mo on its own), HTTPS included, and one command to tear down.
#
# State is container-local, so a restart clears generated scans and the user
# scans again. Acceptable for a short demo; mount a volume or sync to S3 if
# results need to survive.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${APP_NAME:-curbside}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${NAME}"

echo "account ${ACCOUNT}  region ${REGION}"

# --- 1. registry ---------------------------------------------------------
aws ecr describe-repositories --repository-names "$NAME" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" --region "$REGION" >/dev/null
echo "ecr ready: $REPO"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# --- 2. image ------------------------------------------------------------
# App Runner runs x86; build for it explicitly so an ARM laptop still works.
docker build --platform linux/amd64 -t "${NAME}:latest" .
docker tag "${NAME}:latest" "${REPO}:latest"
docker push "${REPO}:latest"
echo "pushed ${REPO}:latest"

# --- 3. secret -----------------------------------------------------------
: "${GEMINI_API_KEY:?export GEMINI_API_KEY before deploying}"
SECRET_ARN=$(aws secretsmanager create-secret \
  --name "${NAME}/gemini" --secret-string "$GEMINI_API_KEY" \
  --region "$REGION" --query ARN --output text 2>/dev/null \
  || aws secretsmanager update-secret --secret-id "${NAME}/gemini" \
       --secret-string "$GEMINI_API_KEY" --region "$REGION" \
       --query ARN --output text)
echo "secret: $SECRET_ARN"

# --- 4. roles ------------------------------------------------------------
# One role lets App Runner pull from ECR, the other lets the running task read
# the secret. Created idempotently so re-running the script is safe.
ACCESS_ROLE="${NAME}-ecr-access"
aws iam get-role --role-name "$ACCESS_ROLE" >/dev/null 2>&1 || {
  aws iam create-role --role-name "$ACCESS_ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"build.apprunner.amazonaws.com"},
    "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ACCESS_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  sleep 10
}

TASK_ROLE="${NAME}-task"
aws iam get-role --role-name "$TASK_ROLE" >/dev/null 2>&1 || {
  aws iam create-role --role-name "$TASK_ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"tasks.apprunner.amazonaws.com"},
    "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam put-role-policy --role-name "$TASK_ROLE" --policy-name read-secret \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",
      \"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${SECRET_ARN}\"}]}"
  sleep 10
}

ACCESS_ARN="arn:aws:iam::${ACCOUNT}:role/${ACCESS_ROLE}"
TASK_ARN="arn:aws:iam::${ACCOUNT}:role/${TASK_ROLE}"

# --- 5. service ----------------------------------------------------------
# Keep one instance warm. App Runner pauses idle instances, and a block scan
# runs as a background task - pausing mid-render would strand it.
CONFIG_NAME="${NAME}-always-on"
CONFIG_ARN=$(aws apprunner list-auto-scaling-configurations --region "$REGION" \
  --auto-scaling-configuration-name "$CONFIG_NAME" \
  --query 'AutoScalingConfigurationSummaryList[0].AutoScalingConfigurationArn' \
  --output text 2>/dev/null || true)

if [ -z "$CONFIG_ARN" ] || [ "$CONFIG_ARN" = "None" ]; then
  CONFIG_ARN=$(aws apprunner create-auto-scaling-configuration \
    --auto-scaling-configuration-name "$CONFIG_NAME" \
    --min-size 1 --max-size 2 --max-concurrency 20 \
    --region "$REGION" \
    --query 'AutoScalingConfiguration.AutoScalingConfigurationArn' --output text)
fi
echo "autoscaling: $CONFIG_ARN"

cat > /tmp/apprunner.json <<JSON
{
  "ServiceName": "${NAME}",
  "SourceConfiguration": {
    "AuthenticationConfiguration": { "AccessRoleArn": "${ACCESS_ARN}" },
    "AutoDeploymentsEnabled": false,
    "ImageRepository": {
      "ImageIdentifier": "${REPO}:latest",
      "ImageRepositoryType": "ECR",
      "ImageConfiguration": {
        "Port": "8000",
        "RuntimeEnvironmentVariables": {
          "CURBSIDE_SOURCE": "indiana",
          "CURBSIDE_BUDGET": "20.00",
          "CURBSIDE_WORKERS": "5",
          "CURBSIDE_CORS_ORIGINS": "*",
          "CURBSIDE_FROM_NAME": "Heartland Driveway Co.",
          "CURBSIDE_FROM_LINE1": "1400 N Meridian St",
          "CURBSIDE_FROM_CITY": "Indianapolis",
          "CURBSIDE_FROM_STATE": "IN",
          "CURBSIDE_FROM_ZIP": "46202"
        },
        "RuntimeEnvironmentSecrets": { "GEMINI_API_KEY": "${SECRET_ARN}" }
      }
    }
  },
  "InstanceConfiguration": {
    "Cpu": "1024", "Memory": "2048", "InstanceRoleArn": "${TASK_ARN}"
  },
  "AutoScalingConfigurationArn": "${CONFIG_ARN}",
  "HealthCheckConfiguration": {
    "Protocol": "HTTP", "Path": "/health", "Interval": 20, "Timeout": 5
  }
}
JSON

EXISTING=$(aws apprunner list-services --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${NAME}'].ServiceArn" --output text)

if [ -n "$EXISTING" ]; then
  echo "updating existing service"
  aws apprunner start-deployment --service-arn "$EXISTING" --region "$REGION" >/dev/null
  ARN="$EXISTING"
else
  ARN=$(aws apprunner create-service --cli-input-json file:///tmp/apprunner.json \
        --region "$REGION" --query 'Service.ServiceArn' --output text)
fi

echo "waiting for the service to come up (3-5 min)…"
for _ in $(seq 1 60); do
  ST=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" \
       --query 'Service.Status' --output text)
  [ "$ST" = "RUNNING" ] && break
  [ "$ST" = "CREATE_FAILED" ] && { echo "failed - check the App Runner console"; exit 1; }
  sleep 15
done

URL=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" \
      --query 'Service.ServiceUrl' --output text)
echo
echo "  API   https://${URL}"
echo "  docs  https://${URL}/docs"
echo
echo "  point the front end at it:  VITE_API_BASE=https://${URL}"
echo "  tear down:                  ./destroy-apprunner.sh"
