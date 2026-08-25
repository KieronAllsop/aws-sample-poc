# ==============================================================================
# 0. TERRAFORM RUNTIME & PROVIDER INITIALIZATION
# ==============================================================================
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ==============================================================================
# 1. ARCHITECTURAL INPUT VARIABLES & DATA GENERATORS
# ==============================================================================
variable "aws_region" {
  type    = string
  default = "eu-west-2"
}

variable "project_name" {
  type    = string
  default = "clearkey-video-pipeline"
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "clear_key_test_value" {
  type        = string
  sensitive   = true
  description = "32-character hex ClearKey value used by this POC"
}

variable "allowed_origin" {
  type        = string
  default     = "http://localhost:8080"
  description = "Browser origin allowed to call the license service"
}

locals {
  principal_lookup = {
    "s3_token"     = "s3.amazonaws.com"
    "lambda_token" = "lambda.amazonaws.com"
  }
}

data "aws_caller_identity" "current" {}

resource "aws_secretsmanager_secret" "database" {
  name                    = "${var.project_name}/database"
  description             = "Credentials for the license database"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database" {
  # Terraform still records secret values in its state; keep that state private.
  secret_id = aws_secretsmanager_secret.database.id

  secret_string = jsonencode({
    username             = "db_admin"
    password             = var.db_password
    clear_key_test_value = var.clear_key_test_value
  })
}

# ==============================================================================
# 2. ISOLATED NETWORK TOPOLOGY (VPC Layer)
# ==============================================================================
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project_name}-vpc" }
}

resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-public-subnet-a" }
}

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "${var.aws_region}b"
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-public-subnet-b" }
}

resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_name}-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }
  tags = { Name = "${var.project_name}-public-rt" }
}

resource "aws_route_table_association" "a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

resource "aws_subnet" "private_a" {
  # These subnets intentionally have no internet route and are reserved for RDS.
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.11.0/24"
  availability_zone = "${var.aws_region}a"
  tags              = { Name = "${var.project_name}-private-subnet-a" }
}

resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.12.0/24"
  availability_zone = "${var.aws_region}b"
  tags              = { Name = "${var.project_name}-private-subnet-b" }
}

# ==============================================================================
# 3. FIREWALL STRUCTURES & SECURITY GROUPS
# ==============================================================================

# --- UNIFIED ALB SECURITY GROUP ---
# Handles traffic from public player engines, CloudFront, and MediaConvert global nodes.
resource "aws_security_group" "alb" {
  name        = "${var.project_name}-public-alb-sg"
  description = "Allows incoming public HTTP traffic from CloudFront edge and MediaConvert global nodes"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Allow all public incoming traffic on port 80"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # MediaConvert uses dynamic AWS global IP blocks to fire SPEKE requests
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- FARGATE TASK SECURITY GROUP ---
# Strictly limits container access on port 8000 to your backend ALB proxy interface.
resource "aws_security_group" "fargate" {
  name        = "${var.project_name}-fargate-sg"
  description = "Locks down the FastAPI server container port footprint to the ALB proxy"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Restrict ingress exclusively to the parent ALB link"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id] # References the unified ALB group securely
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- MANAGED DATABASE SECURITY GROUP ---
# Explicitly unblocks PostgreSQL incoming traffic originating from your Fargate tasks.
resource "aws_security_group" "database" {
  name        = "${var.project_name}-postgres-database-sg"
  description = "Protects the PostgreSQL database by limiting entry to active Fargate compute nodes"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Allow inbound PostgreSQL connections from active Fargate containers"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.fargate.id] # Only tasks running with Fargate SG can connect
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ==============================================================================
# 4. STORAGE INFRASTRUCTURE (S3 Buckets)
# ==============================================================================
resource "aws_s3_bucket" "source" {
  bucket        = "${var.project_name}-source-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "egress" {
  bucket        = "${var.project_name}-egress-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_cors_configuration" "egress_cors" {
  bucket = aws_s3_bucket.egress.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = [var.allowed_origin]
    expose_headers  = ["ETag", "Content-Length", "Access-Control-Allow-Origin"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_public_access_block" "source" {
  bucket                  = aws_s3_bucket.source.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "egress" {
  bucket                  = aws_s3_bucket.egress.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ==========================================
# 5. ROLES & ACCESS POLICIES (IAM)
# ==========================================

# --- MediaConvert Role ---
resource "aws_iam_role" "mediaconvert" {
  name = "${var.project_name}-mediaconvert-execution-role"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "mediaconvert.amazonaws.com"
      }
    }
  ]
}
EOF
}

resource "aws_iam_policy" "mediaconvert_s3" {
  name = "${var.project_name}-mediaconvert-s3-permissions"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:ListBucket"], Resource = [aws_s3_bucket.source.arn, "${aws_s3_bucket.source.arn}/*"] },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:ListBucket"], Resource = [aws_s3_bucket.egress.arn, "${aws_s3_bucket.egress.arn}/*"] }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "mediaconvert" {
  role       = aws_iam_role.mediaconvert.name
  policy_arn = aws_iam_policy.mediaconvert_s3.arn
}

# --- Lambda Trigger Role ---
resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-execution-role"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      }
    }
  ]
}
EOF
}

resource "aws_iam_policy" "lambda_exec" {
  name = "${var.project_name}-lambda-execution-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["mediaconvert:CreateJob"], Resource = "*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.mediaconvert.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.egress.arn}/*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda" {
  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.lambda_exec.arn
}

# --- ECS Fargate Execution Role ---
resource "aws_iam_role" "ecs_execution" {
  name = "${var.project_name}-ecs-execution-role"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      }
    }
  ]
}
EOF
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  # The execution role is used by ECS to pull images, write logs, and read secrets.
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_policy" "ecs_db_secrets_read" {
  name = "${var.project_name}-ecs-db-secrets-read-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.database.arn]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_db_secrets_attach" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = aws_iam_policy.ecs_db_secrets_read.arn
}

resource "aws_iam_policy" "fargate_s3_read" {
  name        = "${var.project_name}-fargate-s3-read-policy"
  description = "Provides the FastAPI proxy layer internal data access privileges"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:ListBucket"]
      Resource = [
        aws_s3_bucket.egress.arn,
        "${aws_s3_bucket.egress.arn}/*"
      ]
    }]
  })
}

resource "aws_iam_role" "fargate_task" {
  # The task role is used by the application code after the container starts.
  name = "${var.project_name}-fargate-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fargate_s3_attach" {
  role       = aws_iam_role.fargate_task.name
  policy_arn = aws_iam_policy.fargate_s3_read.arn
}

# ==============================================================================
# 6. SERVERLESS COMPUTE CORE & LOAD BALANCING (Fargate / ALB)
# ==============================================================================
resource "aws_ecr_repository" "api_repo" {
  name                 = "${var.project_name}-license-server"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_lb" "external" {
  name               = "${var.project_name}-api-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [aws_subnet.public_a.id, aws_subnet.public_b.id]
}

resource "aws_lb_target_group" "api" {
  name        = "${var.project_name}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path     = "/health"
    port     = "8000"
    matcher  = "200"
    interval = 30
    timeout  = 5
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.external.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_cloudwatch_log_group" "ecs_api_logs" {
  name              = "/ecs/${var.project_name}-api"
  retention_in_days = 7
}

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-services-cluster"
}

resource "aws_ecs_task_definition" "api_task" {
  family                   = "${var.project_name}-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"

  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.fargate_task.arn

  container_definitions = jsonencode([{
    name      = "fastapi-server"
    image     = "${aws_ecr_repository.api_repo.repository_url}:latest"
    essential = true
    portMappings = [{
      containerPort = 8000
      hostPort      = 8000
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_api_logs.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
    environment = [
      { name = "S3_BUCKET", value = aws_s3_bucket.egress.id },
      { name = "DB_HOST", value = aws_db_instance.license_db.address },
      { name = "DB_NAME", value = aws_db_instance.license_db.db_name },
      { name = "ALLOWED_ORIGIN", value = var.allowed_origin }
    ]
    secrets = [
      { name = "DB_USER", valueFrom = "${aws_secretsmanager_secret.database.arn}:username::" },
      { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.database.arn}:password::" },
      { name = "CLEAR_KEY_TEST_VALUE", valueFrom = "${aws_secretsmanager_secret.database.arn}:clear_key_test_value::" }
    ]
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${var.project_name}-api-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api_task.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.public_a.id, aws_subnet.public_b.id]
    security_groups  = [aws_security_group.fargate.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "fastapi-server"
    container_port   = 8000
  }

  depends_on = [
    aws_iam_role_policy_attachment.ecs_db_secrets_attach,
    aws_secretsmanager_secret_version.database
  ]
}

# ==============================================================================
# 7. AUTOMATION LAYERS & MEDIA INGESTION HANDLERS (Lambda)
# ==============================================================================
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

resource "aws_lambda_function" "transcoder_trigger" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.project_name}-trigger"
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime          = "python3.11"
  timeout          = 30
  environment {
    variables = {
      MEDIACONVERT_ENDPOINT    = "https://mediaconvert.${var.aws_region}.amazonaws.com"
      MEDIACONVERT_ROLE_ARN    = aws_iam_role.mediaconvert.arn
      EGRESS_BUCKET_NAME       = aws_s3_bucket.egress.id
      SPEKE_LICENSE_SERVER_URL = "https://${aws_cloudfront_distribution.cdn.domain_name}/get-clearkey"
    }
  }
}

resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.transcoder_trigger.function_name

  # Fetches the string directly from memory, hiding it from your shell
  principal  = local.principal_lookup["s3_token"]
  source_arn = aws_s3_bucket.source.arn
}

resource "aws_lambda_permission" "allow_egress_s3" {
  statement_id  = "AllowEgressS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.transcoder_trigger.function_name
  principal     = local.principal_lookup["s3_token"]
  source_arn    = aws_s3_bucket.egress.arn
}

resource "aws_s3_bucket_notification" "source_upload_trigger" {
  bucket = aws_s3_bucket.source.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.transcoder_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".mp4"
  }
  depends_on = [aws_lambda_permission.allow_s3]
}

resource "aws_s3_bucket_notification" "egress_manifest_trigger" {
  bucket = aws_s3_bucket.egress.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.transcoder_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".mpd"
  }

  depends_on = [aws_lambda_permission.allow_egress_s3]
}

# ==============================================================================
# 8. POST-DEPLOYMENT EXPORT INTERFACES (Outputs)
# ==============================================================================

output "ecr_repository_url" {
  value       = aws_ecr_repository.api_repo.repository_url
  description = "Target registry destination to execute Docker builds and pushes."
}

output "source_bucket" {
  value       = aws_s3_bucket.source.id
  description = "Drop raw .mp4 media container structures into this asset bin."
}

output "egress_bucket" {
  value       = aws_s3_bucket.egress.id
  description = "Secure encrypted DASH/CMAF packaging destination bin."
}

output "load_balancer_dns" {
  value       = "http://${aws_lb.external.dns_name}/get-clearkey"
  description = "Paste this direct endpoint directly into your Shaka Player initialization configuration string."
}

resource "aws_cloudfront_origin_access_control" "egress" {
  # CloudFront signs S3 requests so the egress bucket does not need public access.
  name                              = "${var.project_name}-egress-oac"
  description                       = "CloudFront access to the private egress bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "allow_cloudfront_read" {
  bucket = aws_s3_bucket.egress.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontServicePrincipalReadOnly"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.egress.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = "arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/${aws_cloudfront_distribution.cdn.id}"
        }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.egress]
}

# ==============================================================================
# 9. UNIFIED GLOBAL ACCELERATION & SECURE HTTPS EDGE (CloudFront CDN)
# ==============================================================================
resource "aws_cloudfront_distribution" "cdn" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "Unified secure streaming edge proxy gateway"
  price_class     = "PriceClass_100"

  # --- Origin A: Point straight to your private Egress S3 storage bucket ---
  origin {
    # S3 remains private; this origin is authorized through the OAC above.
    domain_name              = aws_s3_bucket.egress.bucket_regional_domain_name
    origin_id                = "S3-EgressStorage"
    origin_access_control_id = aws_cloudfront_origin_access_control.egress.id
  }

  # --- Origin B: Point straight to your backend Fargate ALB ---
  origin {
    domain_name = aws_lb.external.dns_name
    origin_id   = "ALB-ContainerAPI"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # Safe unencrypted transport between edge and container
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # --- Route 1 (Default): Serve all media files directly from private S3 ---
  default_cache_behavior {
    target_origin_id       = "S3-EgressStorage"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https" # Enforces secure HTTPS context for public media
    compress               = false

    forwarded_values {
      query_string = true
      headers      = ["Origin", "Access-Control-Request-Headers", "Access-Control-Request-Method", "Range"]
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  # --- Route 2: Intercept licensing path calls and pass them straight to Fargate ---
  ordered_cache_behavior {
    path_pattern     = "/get-clearkey"
    target_origin_id = "ALB-ContainerAPI"
    allowed_methods  = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods   = ["GET", "HEAD"]

    # MediaConvert SPEKE may initiate HTTP requests and rejects HTTPS redirects.
    viewer_protocol_policy = "allow-all"

    forwarded_values {
      query_string = true
      headers      = ["*"] # Forward all headers to preserve CORS/Auth/SPEKE XML context
      cookies { forward = "all" }
    }

    min_ttl     = 0
    default_ttl = 0 # Live API calls must never cache at the edge
    max_ttl     = 0
  }

  viewer_certificate {
    cloudfront_default_certificate = true # Automatically provisions a free valid SSL certificate
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }
}

# --- Emit the final un-cached secure distribution endpoint ---
output "cdn_secure_domain" {
  value       = "https://${aws_cloudfront_distribution.cdn.domain_name}"
  description = "Unified secure delivery path."
}

resource "aws_db_instance" "license_db" {
  allocated_storage      = 20
  max_allocated_storage  = 50
  engine                 = "postgres"
  engine_version         = "15"
  instance_class         = "db.t4g.micro" # Cost-effective burstable tier
  db_name                = "license_db"
  username               = "db_admin"
  password               = var.db_password
  skip_final_snapshot    = true
  publicly_accessible    = false
  vpc_security_group_ids = [aws_security_group.database.id] # Attaches your existing Fargate SG

  # Ensures the DB is placed in your private subnets
  db_subnet_group_name = aws_db_subnet_group.db_subnets.name
}

resource "aws_db_subnet_group" "db_subnets" {
  name = "${var.project_name}-db-subnet-group"
  subnet_ids = [
    aws_subnet.private_a.id,
    aws_subnet.private_b.id
  ]

  tags = {
    Name = "ClearKey DB Subnet Group"
  }
}

output "database_endpoint" {
  value       = aws_db_instance.license_db.address
  description = "The connection endpoint for the RDS database"
}

output "public_subnet_ids" {
  description = "IDs of the private subnets used by the database"
  value       = aws_db_subnet_group.db_subnets.subnet_ids
}