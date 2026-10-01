"""Capture, verify and restore the Cloudflare frontend without logging credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
STATE = Path("/tmp/qireadr-frontend-release.json")
ORIGIN = "https://qireadr.com"
USER_AGENT = "qireadr-release-verifier/1.0"


def api(path, body=None):
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise ValueError("Invalid Cloudflare account ID")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/scripts/qireadr-web/" + path
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"], "Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("success"):
        raise RuntimeError("Cloudflare operation failed")
    return result["result"]


def active_version():
    deployments = api("deployments")["deployments"]
    if not deployments:
        raise RuntimeError("No existing frontend deployment")
    versions = deployments[0]["versions"]
    if len(versions) != 1 or versions[0]["percentage"] != 100:
        raise RuntimeError("Expected one frontend version serving all traffic")
    return versions[0]["version_id"]


def fetch(path):
    request = urllib.request.Request(ORIGIN + path, headers={"Cache-Control": "no-cache", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def index_digest(data):
    # Cloudflare Web Analytics injects this external beacon after serving assets.
    # Strip only that empty script tag; application markup and bundles stay exact.
    beacon = rb'''<script\b(?=[^>]*\bsrc=["']https://static\.cloudflareinsights\.com/beacon\.min\.js(?:/[A-Za-z0-9]+)?["'])(?=[^>]*\bdata-cf-beacon=)[^>]*>\s*</script>\s*'''
    return digest(re.sub(beacon, b"", data))


def wait_for_index(expected):
    for attempt in range(12):
        try:
            if index_digest(fetch("/")) == expected:
                return
        except OSError:
            pass
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError("Public frontend does not match the expected build")


def capture():
    state = {"previous_version": active_version(), "previous_index_sha256": index_digest(fetch("/")),
             "commit": os.environ["RELEASE_SHA"]}
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def deploy():
    state = json.loads(STATE.read_text())
    # Routes are provisioned separately. Publishing a version must not rewrite them.
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "wrangler.jsonl"
        subprocess.run(["npx", "wrangler", "versions", "upload", "--tag", state["commit"]],
            cwd=ROOT / "deploy/cloudflare", check=True,
            env={**os.environ, "WRANGLER_OUTPUT_FILE_PATH": str(output)})
        uploaded = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    versions = [entry["version_id"] for entry in uploaded if entry.get("type") == "version-upload"]
    if len(versions) != 1:
        raise RuntimeError("Expected exactly one uploaded frontend version")
    version = versions[0]
    metadata = api("versions/" + version)
    if metadata.get("annotations", {}).get("workers/tag") != state["commit"]:
        raise RuntimeError("Uploaded frontend version has the wrong release tag")
    if active_version() != state["previous_version"]:
        raise RuntimeError("Frontend changed outside this release; refusing to overwrite it")
    state["uploaded_version"] = version
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    api("deployments", {"strategy": "percentage", "versions": [
        {"version_id": version, "percentage": 100}]})
    print("Frontend version published through the existing route")


def verify():
    state = json.loads(STATE.read_text())
    current = active_version()
    metadata = api("versions/" + current)
    if metadata.get("annotations", {}).get("workers/tag") != state["commit"]:
        raise RuntimeError("Frontend version is not tagged with this release commit")
    state["deployed_version"] = current
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    build = ROOT / "services/web/dist"
    wait_for_index(index_digest((build / "index.html").read_bytes()))
    for asset in (build / "assets").iterdir():
        if asset.is_file() and digest(fetch("/assets/" + asset.name)) != digest(asset.read_bytes()):
            raise RuntimeError("Frontend asset mismatch: " + asset.name)
    for path in ("/api/auth/session", "/api/novels"):
        request = urllib.request.Request(ORIGIN + path, headers={"X-Account-ID": "forged", "X-Reader-ID": "forged", "User-Agent": USER_AGENT})
        try:
            urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            if error.code == 401 and "no-store" in error.headers.get("Cache-Control", ""):
                continue
        raise RuntimeError("Unauthenticated access boundary failed: " + path)
    print("Frontend version, HTML, assets and unauthenticated API checks passed")


def rollback():
    state = json.loads(STATE.read_text())
    current = active_version()
    if current != state["previous_version"]:
        metadata = api("versions/" + current)
        if metadata.get("annotations", {}).get("workers/tag") != state["commit"]:
            raise RuntimeError("Frontend changed outside this release; refusing to overwrite it")
        api("deployments", {"strategy": "percentage", "versions": [
            {"version_id": state["previous_version"], "percentage": 100}]})
    if active_version() != state["previous_version"]:
        raise RuntimeError("Previous frontend version was not restored")
    wait_for_index(state["previous_index_sha256"])
    print("Previous frontend restored and verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "deploy", "verify", "rollback"))
    args = parser.parse_args()
    {"capture": capture, "deploy": deploy, "verify": verify, "rollback": rollback}[args.action]()
