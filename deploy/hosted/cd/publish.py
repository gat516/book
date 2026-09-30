"""Build immutable images for the checked commit; reuse existing tags on retries."""
import json
import os
from pathlib import Path
import re
import subprocess

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())


def main():
    sha = os.environ["RELEASE_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Full commit SHA required")
    host = CONFIG["registry"].split("/")[0]
    password = subprocess.check_output(["aws", "ecr", "get-login-password", "--region", CONFIG["region"]])
    subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", host], input=password, check=True)
    for name in ("reader-api", "ingest-api", "scraper", "python", "web", "textproc"):
        exists = subprocess.run(["aws", "ecr", "describe-images", "--region", CONFIG["region"],
            "--repository-name", "private-books/" + name, "--image-ids", "imageTag=" + sha], capture_output=True, text=True)
        if exists.returncode == 0:
            print("Reusing immutable image:", name, flush=True)
            continue
        if "ImageNotFoundException" not in exists.stderr:
            raise RuntimeError(exists.stderr)
        args = ["docker", "build", "-t", CONFIG["registry"] + "/" + name + ":" + sha]
        if name in ("reader-api", "ingest-api", "scraper"):
            args += ["-f", "deploy/hosted/Dockerfile.go", "--build-arg", "SERVICE=" + name, "--build-arg", "VERSION=" + sha]
        else:
            dockerfile = "services/textproc/Dockerfile" if name == "textproc" else "deploy/hosted/Dockerfile." + name
            args += ["-f", dockerfile]
        subprocess.run(args + ["."], check=True)
        subprocess.run(["docker", "push", CONFIG["registry"] + "/" + name + ":" + sha], check=True)


if __name__ == "__main__":
    main()
