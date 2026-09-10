#!/usr/bin/env bash
# Remove everything the ECS deploy created. Run this when John is done.
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
NAME="${APP_NAME:-curbside}"

echo "tearing down ${NAME} in ${REGION}…"

# Service first - it holds the ALB target and the ENI.
aws ecs update-service --cluster "$NAME" --service "$NAME" --desired-count 0 \
  --region "$REGION" >/dev/null 2>&1 || true
aws ecs delete-service --cluster "$NAME" --service "$NAME" --force \
  --region "$REGION" >/dev/null 2>&1 || true
echo "  service deleted"

ALB_ARN=$(aws elbv2 describe-load-balancers --names "${NAME}-alb" \
          --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "$REGION" 2>/dev/null || echo "")
if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" --region "$REGION" >/dev/null 2>&1 || true
  echo "  load balancer deleted (this is the main hourly charge)"
fi

TG_ARN=$(aws elbv2 describe-target-groups --names "${NAME}-tg" \
         --query 'TargetGroups[0].TargetGroupArn' --output text --region "$REGION" 2>/dev/null || echo "")
[ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ] && {
  # The ALB must finish deleting before its target group will release.
  sleep 20
  aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$REGION" >/dev/null 2>&1 || true
}

aws ecs delete-cluster --cluster "$NAME" --region "$REGION" >/dev/null 2>&1 || true
aws ecr delete-repository --repository-name "$NAME" --force --region "$REGION" >/dev/null 2>&1 || true
aws secretsmanager delete-secret --secret-id "${NAME}/gemini" \
  --force-delete-without-recovery --region "$REGION" >/dev/null 2>&1 || true
aws logs delete-log-group --log-group-name "/ecs/${NAME}" --region "$REGION" >/dev/null 2>&1 || true

aws iam delete-role-policy --role-name "${NAME}-exec" --policy-name read-secret 2>/dev/null || true
aws iam detach-role-policy --role-name "${NAME}-exec" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy 2>/dev/null || true
aws iam delete-role --role-name "${NAME}-exec" 2>/dev/null || true
# Leftovers from the App Runner attempt.
aws iam delete-role-policy --role-name "${NAME}-task" --policy-name read-secret 2>/dev/null || true
aws iam delete-role --role-name "${NAME}-task" 2>/dev/null || true
aws iam detach-role-policy --role-name "${NAME}-ecr-access" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess 2>/dev/null || true
aws iam delete-role --role-name "${NAME}-ecr-access" 2>/dev/null || true

# The security group cannot go until the ENIs are released.
sleep 30
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text --region "$REGION")
SG=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values=$VPC \
     Name=group-name,Values=${NAME}-sg --query 'SecurityGroups[0].GroupId' \
     --output text --region "$REGION" 2>/dev/null || echo "")
[ -n "$SG" ] && [ "$SG" != "None" ] && {
  aws ec2 delete-security-group --group-id "$SG" --region "$REGION" 2>/dev/null \
    || echo "  security group still in use - retry in a minute: aws ec2 delete-security-group --group-id $SG"
}

echo "done - billing stops once the load balancer finishes deleting."
