#!/usr/bin/env python3
"""Run Terraform with temporary credentials from an MFA-protected role."""

import argparse
import getpass
import json
import os
import subprocess
import sys


def assume_role(args):
    token_code = getpass.getpass("MFA code: ")
    source_environment = os.environ.copy()
    for variable in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        source_environment.pop(variable, None)

    command = [
        "aws",
        "sts",
        "assume-role",
        "--profile",
        args.source_profile,
        "--role-arn",
        args.role_arn,
        "--role-session-name",
        args.session_name,
        "--serial-number",
        args.mfa_serial,
        "--token-code",
        token_code,
        "--region",
        args.region,
        "--output",
        "json",
    ]

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=source_environment,
    )
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit("Unable to assume the deployment role.")

    credentials = json.loads(result.stdout)["Credentials"]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_")
    }
    environment["AWS_ACCESS_KEY_ID"] = credentials["AccessKeyId"]
    environment["AWS_SECRET_ACCESS_KEY"] = credentials["SecretAccessKey"]
    environment["AWS_SESSION_TOKEN"] = credentials["SessionToken"]
    environment["AWS_REGION"] = args.region
    environment["AWS_DEFAULT_REGION"] = args.region

    identity = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if identity.returncode != 0:
        print(identity.stderr.strip(), file=sys.stderr)
        raise SystemExit("The assumed-role credentials are not valid.")

    return environment


def main():
    account_id = os.environ.get("AWS_ACCOUNT_ID")
    role_name = os.environ.get("AWS_ROLE_NAME")
    mfa_device_name = os.environ.get("AWS_MFA_DEVICE_NAME")
    missing_settings = [
        name
        for name, value in {
            "AWS_ACCOUNT_ID": account_id,
            "AWS_ROLE_NAME": role_name,
            "AWS_MFA_DEVICE_NAME": mfa_device_name,
        }.items()
        if not value
    ]
    if missing_settings:
        raise SystemExit(
            "Set these environment variables before running terraform_mfa.py: "
            + ", ".join(missing_settings)
        )

    parser = argparse.ArgumentParser(
        description="Run Terraform using a fresh MFA-backed deployment-role session."
    )
    parser.add_argument("terraform_command", choices=["plan", "apply", "destroy", "import"])
    parser.add_argument("terraform_args", nargs=argparse.REMAINDER)
    parser.add_argument("--source-profile", default="kieron")
    parser.add_argument(
        "--role-arn",
        default=f"arn:aws:iam::{account_id}:role/{role_name}",
    )
    parser.add_argument(
        "--mfa-serial",
        default=f"arn:aws:iam::{account_id}:mfa/{mfa_device_name}",
    )
    parser.add_argument("--session-name", default="terraform")
    parser.add_argument("--region", default="eu-west-2")
    args = parser.parse_args()

    environment = assume_role(args)
    terraform = ["terraform", args.terraform_command, *args.terraform_args]
    completed = subprocess.run(terraform, env=environment)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
