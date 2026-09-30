"""Render or apply the GitHub main-only AWS role and fixed SSM release document.

Use AWS_PROFILE=book python configure.py --apply once from the operator machine.
No credentials are exported to GitHub. Application infrastructure is not applied.
"""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())


def resources():
    account, region = CONFIG["account"], CONFIG["region"]
    provider = f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Federated": provider},
        "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": f"repo:{CONFIG['repository']}:ref:refs/heads/main"}}}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
            "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages"],
         "Resource": [f"arn:aws:ecr:{region}:{account}:repository/private-books/{name}" for name in
                      ("reader-api", "ingest-api", "scraper", "python", "web", "textproc")]},
        {"Effect": "Allow", "Action": ["ssm:SendCommand"], "Resource": [
            f"arn:aws:ssm:{region}:{account}:document/{CONFIG['document']}",
            f"arn:aws:ec2:{region}:{account}:instance/{CONFIG['instance']}"]},
        {"Effect": "Allow", "Action": ["ssm:GetCommandInvocation"], "Resource": "*"},
    ]}
    document = {"schemaVersion": "2.2", "description": "Deploy, verify or restore the qireadr application only",
        "parameters": {"Action": {"type": "String", "allowedValues": ["deploy", "verify", "rollback"]},
                       "Commit": {"type": "String", "allowedPattern": "^[0-9a-f]{40}$"},
                       "DbTree": {"type": "String", "allowedPattern": "^[0-9a-f]{40}$"}},
        "mainSteps": [{"action": "aws:runShellScript", "name": "release", "inputs": {"timeoutSeconds": "840",
            "runCommand": ["set -eu", "export BOOK_CD_ACTION='{{ Action }}'",
                           "export BOOK_CD_COMMIT='{{ Commit }}'", "export BOOK_CD_DB_TREE='{{ DbTree }}'",
                           "export BOOK_CD_CONFIG='" + json.dumps(CONFIG) + "'",
                           "python3 - <<'QIREADR_RELEASE_PY'\n" + (HERE / "node.py").read_text() + "\nQIREADR_RELEASE_PY"]}}]}
    return provider, trust, policy, document


def aws(*args, optional=False):
    result = subprocess.run(["aws", "--region", CONFIG["region"], *args], text=True, capture_output=True)
    if result.returncode:
        if optional and ("NoSuchEntity" in result.stderr or "InvalidDocument" in result.stderr):
            return None
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    provider, trust, policy, document = resources()
    if not args.apply:
        print(json.dumps({"trust": trust, "policy": policy, "document": document}, indent=2))
        return
    if aws("sts", "get-caller-identity")["Account"] != CONFIG["account"]:
        raise RuntimeError("Wrong AWS account")
    if aws("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", provider, optional=True) is None:
        aws("iam", "create-open-id-connect-provider", "--url", "https://token.actions.githubusercontent.com",
            "--client-id-list", "sts.amazonaws.com")
    role = CONFIG["role"]
    existing = aws("iam", "get-role", "--role-name", role, optional=True)
    if existing is None:
        aws("iam", "create-role", "--role-name", role, "--assume-role-policy-document", json.dumps(trust))
    else:
        aws("iam", "update-assume-role-policy", "--role-name", role, "--policy-document", json.dumps(trust))
    aws("iam", "put-role-policy", "--role-name", role, "--policy-name", "qireadr-release", "--policy-document", json.dumps(policy))
    existing = aws("ssm", "get-document", "--name", CONFIG["document"], optional=True)
    if existing is None:
        aws("ssm", "create-document", "--name", CONFIG["document"], "--document-type", "Command", "--content", json.dumps(document))
    elif json.loads(existing["Content"]) != document:
        updated = aws("ssm", "update-document", "--name", CONFIG["document"], "--document-version", "$LATEST", "--content", json.dumps(document))
        aws("ssm", "update-document-default-version", "--name", CONFIG["document"],
            "--document-version", updated["DocumentDescription"]["DocumentVersion"])
    print(f"AWS_DEPLOY_ROLE_ARN=arn:aws:iam::{CONFIG['account']}:role/{role}")


if __name__ == "__main__":
    main()
