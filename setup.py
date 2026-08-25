import argparse
import json
import os
import zipfile
from subprocess import run, PIPE, CalledProcessError

PROJECT_NAME = os.environ.get("TF_VAR_project_name", "clearkey-video-pipeline")

def refresh_lambda_function():
    # Remove existing zip file if it exists
    if os.path.exists('./lambda_function.zip'):
        os.remove('./lambda_function.zip')
    
    # Create new zip file from lambda_function.py
    with zipfile.ZipFile('./lambda_function.zip', 'w') as zipf:
        zipf.write('./lambda_function.py')

def apply_terraform_config(db_password, clear_key_test_value):
    try:
        terraform_environment = os.environ.copy()
        terraform_environment["TF_VAR_db_password"] = db_password
        terraform_environment["TF_VAR_clear_key_test_value"] = clear_key_test_value
        run(
            ["terraform", "apply", "--auto-approve"],
            check=True,
            env=terraform_environment
        )

        # Extract outputs from terraform output command
        cdn_secure_domain = run(["terraform", "output", "-raw", "cdn_secure_domain"], capture_output=True, text=True).stdout.strip()
        database_endpoint = run(["terraform", "output", "-raw", "database_endpoint"], capture_output=True, text=True).stdout.strip()
        ecr_repository_url = run(["terraform", "output", "-raw", "ecr_repository_url"], capture_output=True, text=True).stdout.strip()
        egress_bucket = run(["terraform", "output", "-raw", "egress_bucket"], capture_output=True, text=True).stdout.strip()
        load_balancer_dns = run(["terraform", "output", "-raw", "load_balancer_dns"], capture_output=True, text=True).stdout.strip()
        source_bucket = run(["terraform", "output", "-raw", "source_bucket"], capture_output=True, text=True).stdout.strip()

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
    # login to docker
    login_command = f'aws ecr get-login-password --region eu-west-2 | sudo docker login --username AWS --password-stdin {repo_url}'
    run(login_command, shell=True, check=True)

def build_and_push_docker_image(repo_url):
    # build docker image
    build_command = 'sudo docker build -t clearkey-license-server .'
    run(build_command, shell=True, check=True)
    
    # tag docker image
    tag_command = f'sudo docker tag clearkey-license-server:latest {repo_url}:latest'
    run(tag_command, shell=True, check=True)
    
    # push docker image
    push_command = f'sudo docker push {repo_url}:latest'
    run(push_command, shell=True, check=True)

def create_drm_cluster():

    # Run the AWS ECS command
    ecs_command = f"aws ecs update-service --cluster {PROJECT_NAME}-services-cluster --service {PROJECT_NAME}-api-service --force-new-deployment --region eu-west-2"
    result = run(ecs_command, shell=True, capture_output=True, text=True)

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

def run_aws_ecs_task(ecs_service_details, terraform_output, db_password, clear_key_test_value):
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
                {"name": "DB_USER", "value": "db_admin"},
                {"name": "DB_PASSWORD", "value": db_password},
                {"name": "DB_HOST", "value": terraform_output.get('database_endpoint')},
                {"name": "DB_NAME", "value": "license_db"},
                {"name": "CLEAR_KEY_TEST_VALUE", "value": clear_key_test_value}
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
        "--region", "eu-west-2"
    ]

    run(command, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run with dynamic database and ClearKey test details.')
    parser.add_argument('db_password', type=str, help='Database password')
    parser.add_argument('clear_key_test_value', type=str, help='32-character hex ClearKey test value')
    args = parser.parse_args()
    if args.db_password is None:
        parser.error("The 'db_password' argument is required.")
        exit(1)
    
    refresh_lambda_function()
    terraform_output = apply_terraform_config(args.db_password, args.clear_key_test_value)
    if terraform_output:
        print(terraform_output)
    else:
        raise SystemExit("Failed to get Terraform outputs.")

    ecr_repo_url = terraform_output['ecr_repository_url']
    login_to_docker(ecr_repo_url)
    build_and_push_docker_image(ecr_repo_url)
    ecs_service_details = create_drm_cluster()
    run_aws_ecs_task(ecs_service_details, terraform_output, args.db_password, args.clear_key_test_value)

    print("Setup complete")