#!/usr/bin/env bash
# Deploy the API to ECS Fargate behind an Application Load Balancer.
#
# App Runner is unavailable on a free-plan account (SubscriptionRequiredException),
# so this is the equivalent using primitives every account has: Fargate for the
# container, an ALB for HTTPS-capable public access.
#
# State is container-local. A task restart clears generated scans and the user
# scans again - acceptable for a short demo.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${APP_NAME:-curbside}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${NAME}"

echo "account ${ACCOUNT}  region ${REGION}"

# --- 1. image ------------------------------------------------------------
aws ecr describe-repositories --repository-names "$NAME" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" --region "$REGION" >/dev/null

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null

# Fargate runs x86; build for it explicitly so an ARM laptop still works.
docker build --platform linux/amd64 -t "${NAME}:latest" .
docker tag "${NAME}:latest" "${REPO}:latest"
docker push "${REPO}:latest" >/dev/null
echo "image: ${REPO}:latest"

# --- 2. secret -----------------------------------------------------------
: "${GEMINI_API_KEY:?export GEMINI_API_KEY before deploying}"
SECRET_ARN=$(aws secretsmanager create-secret --name "${NAME}/gemini" \
  --secret-string "$GEMINI_API_KEY" --region "$REGION" \
  --query ARN --output text 2>/dev/null \
  || aws secretsmanager update-secret --secret-id "${NAME}/gemini" \
       --secret-string "$GEMINI_API_KEY" --region "$REGION" \
       --query ARN --output text)
echo "secret: ${SECRET_ARN}"

# --- 3. roles ------------------------------------------------------------
# Two roles: the execution role lets ECS pull the image and read the secret at
# launch; the task role is what the running container itself would use.
EXEC_ROLE="${NAME}-exec"
if ! aws iam get-role --role-name "$EXEC_ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$EXEC_ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$EXEC_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  ROLE_CREATED=1
fi
# Reading the secret is not covered by the managed policy.
aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name read-secret \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",
    \"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${SECRET_ARN}\"}]}"
EXEC_ARN="arn:aws:iam::${ACCOUNT}:role/${EXEC_ROLE}"
# IAM is eventually consistent; a fresh role is not usable immediately.
[ "${ROLE_CREATED:-0}" = "1" ] && { echo "waiting for IAM to propagate…"; sleep 15; }

# --- 4. network ----------------------------------------------------------
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text --region "$REGION")
# An ALB needs two AZs; take the first two public subnets.
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
          Name=map-public-ip-on-launch,Values=true \
          --query 'Subnets[:2].SubnetId' --output text --region "$REGION")
SUB1=$(echo $SUBNETS | cut -d' ' -f1); SUB2=$(echo $SUBNETS | cut -d' ' -f2)
echo "vpc ${VPC}  subnets ${SUB1} ${SUB2}"

# Dedicated security group: 80 open to the world, which the ALB needs.
SG=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values=$VPC \
     Name=group-name,Values=${NAME}-sg --query 'SecurityGroups[0].GroupId' \
     --output text --region "$REGION" 2>/dev/null || echo "None")
if [ "$SG" = "None" ] || [ -z "$SG" ]; then
  SG=$(aws ec2 create-security-group --group-name "${NAME}-sg" \
       --description "curbside demo" --vpc-id "$VPC" \
       --query GroupId --output text --region "$REGION")
  aws ec2 authorize-security-group-ingress --group-id "$SG" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 --region "$REGION" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" \
    --protocol tcp --port 8000 --source-group "$SG" --region "$REGION" >/dev/null
fi
echo "security group ${SG}"

# --- 5. load balancer ----------------------------------------------------
ALB_ARN=$(aws elbv2 describe-load-balancers --names "${NAME}-alb" \
          --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "$REGION" 2>/dev/null || echo "")
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN=$(aws elbv2 create-load-balancer --name "${NAME}-alb" \
    --subnets "$SUB1" "$SUB2" --security-groups "$SG" --scheme internet-facing \
    --type application --query 'LoadBalancers[0].LoadBalancerArn' \
    --output text --region "$REGION")
fi

TG_ARN=$(aws elbv2 describe-target-groups --names "${NAME}-tg" \
         --query 'TargetGroups[0].TargetGroupArn' --output text --region "$REGION" 2>/dev/null || echo "")
if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  # target-type ip is required for Fargate's awsvpc networking.
  TG_ARN=$(aws elbv2 create-target-group --name "${NAME}-tg" \
    --protocol HTTP --port 8000 --vpc-id "$VPC" --target-type ip \
    --health-check-path /health --health-check-interval-seconds 30 \
    --healthy-threshold-count 2 --unhealthy-threshold-count 5 \
    --query 'TargetGroups[0].TargetGroupArn' --output text --region "$REGION")
fi

if ! aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$REGION" \
     --query 'Listeners[0].ListenerArn' --output text 2>/dev/null | grep -q arn; then
  aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
    --default-actions Type=forward,TargetGroupArn="$TG_ARN" --region "$REGION" >/dev/null
fi
echo "load balancer ready"

# --- 6. task definition --------------------------------------------------
aws logs create-log-group --log-group-name "/ecs/${NAME}" --region "$REGION" >/dev/null 2>&1 || true

cat > /tmp/taskdef.json <<JSON
{
  "family": "${NAME}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "${EXEC_ARN}",
  "containerDefinitions": [{
    "name": "api",
    "image": "${REPO}:latest",
    "essential": true,
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environment": [
      {"name": "CURBSIDE_SOURCE", "value": "indiana"},
      {"name": "CURBSIDE_BUDGET", "value": "20.00"},
      {"name": "CURBSIDE_WORKERS", "value": "5"},
      {"name": "CURBSIDE_CORS_ORIGINS", "value": "*"},
      {"name": "CURBSIDE_FROM_NAME", "value": "Heartland Driveway Co."},
      {"name": "CURBSIDE_FROM_LINE1", "value": "1400 N Meridian St"},
      {"name": "CURBSIDE_FROM_CITY", "value": "Indianapolis"},
      {"name": "CURBSIDE_FROM_STATE", "value": "IN"},
      {"name": "CURBSIDE_FROM_ZIP", "value": "46202"}
    ],
    "secrets": [{"name": "GEMINI_API_KEY", "valueFrom": "${SECRET_ARN}"}],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/${NAME}",
        "awslogs-region": "${REGION}",
        "awslogs-stream-prefix": "api"
      }
    }
  }]
}
JSON

TASK_ARN=$(aws ecs register-task-definition --cli-input-json file:///tmp/taskdef.json \
           --region "$REGION" --query 'taskDefinition.taskDefinitionArn' --output text)
echo "task definition: ${TASK_ARN##*/}"

# --- 7. service ----------------------------------------------------------
aws ecs create-cluster --cluster-name "$NAME" --region "$REGION" >/dev/null 2>&1 || true

EXISTS=$(aws ecs describe-services --cluster "$NAME" --services "$NAME" --region "$REGION" \
         --query 'services[0].status' --output text 2>/dev/null || echo "NONE")

if [ "$EXISTS" = "ACTIVE" ]; then
  echo "updating existing service"
  aws ecs update-service --cluster "$NAME" --service "$NAME" \
    --task-definition "$TASK_ARN" --force-new-deployment --region "$REGION" >/dev/null
else
  aws ecs create-service --cluster "$NAME" --service-name "$NAME" \
    --task-definition "$TASK_ARN" --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUB1,$SUB2],securityGroups=[$SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=${TG_ARN},containerName=api,containerPort=8000" \
    --health-check-grace-period-seconds 120 \
    --region "$REGION" >/dev/null
fi

DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
      --query 'LoadBalancers[0].DNSName' --output text --region "$REGION")

echo
echo "waiting for the task to become healthy (2-4 min)…"
for _ in $(seq 1 40); do
  H=$(aws elbv2 describe-target-health --target-group-arn "$TG_ARN" --region "$REGION" \
      --query 'TargetHealthDescriptions[0].TargetHealth.State' --output text 2>/dev/null || echo "none")
  [ "$H" = "healthy" ] && break
  sleep 15
done

echo
if [ "${H:-}" = "healthy" ]; then
  echo "  API   http://${DNS}"
  echo "  docs  http://${DNS}/docs"
  echo
  echo "  front end:"
  echo "    cd web"
  echo "    sed -i \"s|REPLACE_WITH_ALB_DNS|${DNS}|\" vercel.json"
  echo "    npm run build && npx vercel deploy --prod"
  echo
  echo "  (vercel.json proxies /api to the ALB. Pointing the browser straight"
  echo "   at http://${DNS} would be blocked as mixed content from an HTTPS page.)"
  echo
  echo "  tear down:  ./destroy-ecs.sh"
else
  echo "  target still ${H:-unknown} - check:"
  echo "    aws logs tail /ecs/${NAME} --follow --region ${REGION}"
  echo "  ALB DNS: http://${DNS}"
fi
