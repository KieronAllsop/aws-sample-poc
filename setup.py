import argparse
import getpass
import json
import os
import re
import subprocess
import zipfile
from types import SimpleNamespace
from subprocess import run, CalledProcessError

from terraform_mfa import assume_role

PROJECT_NAME = os.environ.get("TF_VAR_project_name", "clearkey-video-pipeline")
AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID")
AWS_ROLE_NAME = os.environ.get("AWS_ROLE_NAME")
AWS_MFA_DEVICE_NAME = os.environ.get("AWS_MFA_DEVICE_NAME")
ROLE_ENVIRONMENT = None


def assume_deployment_role():
    account_id = os.environ.get("AWS_ACCOUNT_ID")
    role_name = os.environ.get("AWS_ROLE_NAME")
    mfa_device_name = os.environ.get("AWS_MFA_DEVICE_NAME")
    required_settings = {
        "AWS_ACCOUNT_ID": account_id,
        "AWS_ROLE_NAME": role_name,
        "AWS_MFA_DEVICE_NAME": mfa_device_name,
    }
    missing_settings = [name for name, value in required_settings.items() if not value]
    if missing_settings:
        raise SystemExit(
            "Set these environment variables before running setup.py: "
            + ", ".join(missing_settings)
        )

    return assume_role(SimpleNamespace(
        source_profile=os.environ.get("AWS_SOURCE_PROFILE", "kieron"),
        role_arn=f"arn:aws:iam::{account_id}:role/{role_name}",
        mfa_serial=f"arn:aws:iam::{account_id}:mfa/{mfa_device_name}",
        session_name="setup",
        region=AWS_REGION,
    ))

def refresh_lambda_function():
    # Remove existing zip file if it exists
    if os.path.exists('./lambda_function.zip'):
        os.remove('./lambda_function.zip')
    
    # Create new zip file from lambda_function.py
    with zipfile.ZipFile('./lambda_function.zip', 'w') as zipf:
        zipf.write('./lambda_function.py')

def apply_terraform_config():
    try:
        run(
            ["terraform", "apply", "--auto-approve"],
            check=True,
            env=ROLE_ENVIRONMENT
        )

        # Extract outputs from terraform output command
        def terraform_output(name):
            return run(
                ["terraform", "output", "-raw", name],
                capture_output=True,
                text=True,
                check=True,
                env=ROLE_ENVIRONMENT,
            ).stdout.strip()

        cdn_secure_domain = terraform_output("cdn_secure_domain")
        database_endpoint = terraform_output("database_endpoint")
        ecr_repository_url = terraform_output("ecr_repository_url")
        egress_bucket = terraform_output("egress_bucket")
        load_balancer_dns = terraform_output("load_balancer_dns")
        source_bucket = terraform_output("source_bucket")

        return {
            "cdn_secure_domain": cdn_secure_domain,
            "database_endpoint": database_endpoint,
            "ecr_repository_url": ecr_repository_url,
            "egress_bucket": egress_bucket,
            "load_balancer_dns": load_balancer_dns,
            "source_bucket": source_bucket
        }
    except CalledProcessError as e:
        print(f"Error running terraform apply: {e}")
        return None

def login_to_docker(repo_url):
    # Keep the AWS credentialed command separate from sudo's environment handling.
    aws_process = subprocess.Popen(
        ["aws", "ecr", "get-login-password", "--region", AWS_REGION],
        stdout=subprocess.PIPE,
        env=ROLE_ENVIRONMENT,
    )
    docker_process = run(
        ["sudo", "docker", "login", "--username", "AWS", "--password-stdin", repo_url],
        stdin=aws_process.stdout,
        check=False,
        env=ROLE_ENVIRONMENT,
    )
    aws_process.stdout.close()
    aws_return_code = aws_process.wait()
    if aws_return_code != 0 or docker_process.returncode != 0:
        raise SystemExit("ECR login failed; refresh the MFA session and try again.")

def build_and_push_docker_image(repo_url):
    # build docker image
    build_command = 'sudo docker build -t clearkey-license-server .'
    run(build_command, shell=True, check=True, env=ROLE_ENVIRONMENT)
    
    # tag docker image
    tag_command = f'sudo docker tag clearkey-license-server:latest {repo_url}:latest'
    run(tag_command, shell=True, check=True, env=ROLE_ENVIRONMENT)
    
    # push docker image
    push_command = f'sudo docker push {repo_url}:latest'
    run(push_command, shell=True, check=True, env=ROLE_ENVIRONMENT)

def create_drm_cluster():

    # Run the AWS ECS command
    ecs_command = f"aws ecs update-service --cluster {PROJECT_NAME}-services-cluster --service {PROJECT_NAME}-api-service --force-new-deployment --region {AWS_REGION}"
    result = run(ecs_command, shell=True, capture_output=True, text=True, env=ROLE_ENVIRONMENT)

    # Parse the JSON output
    try:
        json_output = json.loads(result.stdout)
        service_details = {
            "cluster_arn": json_output.get('service', {}).get('clusterArn'),
            "task_definition": json_output.get('service', {}).get('taskDefinition'),
            "subnets": json_output.get('service', {}).get('deployments', [{}])[0].get('networkConfiguration', {}).get('awsvpcConfiguration', {}).get('subnets', []),
            "security_groups": json_output.get('service', {}).get('deployments', [{}])[0].get('networkConfiguration', {}).get('awsvpcConfiguration', {}).get('securityGroups', [])
        }
    except json.JSONDecodeError:
        service_details = {
            "cluster_arn": None,
            "task_definition": None,
            "subnets": [],
            "security_groups": []
        }

    return service_details

def run_aws_ecs_task(ecs_service_details, terraform_output):
    # Create the ECS task definition
    network_config = {
        "awsvpcConfiguration": {
            "subnets": ecs_service_details.get('subnets'),
            "securityGroups": ecs_service_details.get('security_groups'),
            "assignPublicIp": "DISABLED"
        }
    }

    container_overrides = [
        {
            "name": "fastapi-server",
            "command": ["python", "create_tables.py"],
            "environment": [
                {"name": "S3_BUCKET", "value": terraform_output.get('egress_bucket')},
                {"name": "DB_HOST", "value": terraform_output.get('database_endpoint')},
                {"name": "DB_NAME", "value": "license_db"}
            ]
        }
    ]

    command = [
        "aws", "ecs", "run-task",
        "--cluster", ecs_service_details.get('cluster_arn'),
        "--task-definition", ecs_service_details.get('task_definition'),
        "--launch-type", "FARGATE",
        "--network-configuration", json.dumps(network_config),
        "--overrides", json.dumps({"containerOverrides": container_overrides}),
        "--region", AWS_REGION
    ]

    run(command, check=True, env=ROLE_ENVIRONMENT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run with dynamic database and ClearKey test details.')
    parser.add_argument("--account-id", required=True, help="AWS account ID")
    parser.add_argument("--role-name", required=True, help="Deployment role name")
    parser.add_argument("--mfa-device-name", required=True, help="MFA device name")
    parser.add_argument("--source-profile", default="kieron", help="AWS profile for the IAM user")
    parser.add_argument("--region", default=AWS_REGION, help="AWS region")
    parser.add_argument("--project-name", default=PROJECT_NAME, help="Terraform project name")
    parser.add_argument(
        "--db-password",
        help="Database password; omit to enter it securely when prompted",
    )
    parser.add_argument(
        "--clear-key-value",
        help="32-character hex ClearKey value; omit to enter it securely when prompted",
    )
    args = parser.parse_args()
    AWS_REGION = args.region
    PROJECT_NAME = args.project_name
    os.environ["AWS_ACCOUNT_ID"] = args.account_id
    os.environ["AWS_ROLE_NAME"] = args.role_name
    os.environ["AWS_MFA_DEVICE_NAME"] = args.mfa_device_name
    os.environ["AWS_SOURCE_PROFILE"] = args.source_profile
    os.environ["AWS_REGION"] = args.region
    os.environ["TF_VAR_project_name"] = args.project_name
    ROLE_ENVIRONMENT = assume_deployment_role()

    db_password = args.db_password or getpass.getpass("Database password: ")
    clear_key_value = args.clear_key_value or getpass.getpass("ClearKey value: ")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", clear_key_value):
        raise SystemExit("The ClearKey value must contain exactly 32 hexadecimal characters.")
    ROLE_ENVIRONMENT["TF_VAR_db_password"] = db_password
    ROLE_ENVIRONMENT["TF_VAR_clear_key_test_value"] = clear_key_value
    
    refresh_lambda_function()
    terraform_output = apply_terraform_config()
    if terraform_output:
        print(terraform_output)
    else:
        raise SystemExit("Failed to get Terraform outputs.")

    ecr_repo_url = terraform_output['ecr_repository_url']
    login_to_docker(ecr_repo_url)
    build_and_push_docker_image(ecr_repo_url)
    ecs_service_details = create_drm_cluster()
    run_aws_ecs_task(ecs_service_details, terraform_output)

    print("Setup complete")