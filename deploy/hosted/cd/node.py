"""Execute one application release on the existing k3s node (§15).

Installed as a restricted SSM document, not a general shell command. Database
changes require a separate migration release; Redis, secrets and ingress stay put.
"""
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

DEPLOYMENTS = {
    "textproc": "textproc", "ingest-api": "ingest-api", "scraper": "scraper",
    "pipeline": "python", "askai": "python", "reader-api": "reader-api", "web": "web",
}
CRONJOBS = ("portable-backup", "account-cleanup")
ROOT = Path("/root/qireadr/releases")


def run(args, body=None):
    result = subprocess.run(args, input=body, text=True, capture_output=True, timeout=360)
    if result.returncode:
        # Never echo commands, registry passwords, Kubernetes secrets or pod logs.
        raise RuntimeError(f"{args[0]} operation failed: {result.stderr[:500]}")
    return result.stdout


def kube(*args, body=None):
    return run(["k3s", "kubectl", "-n", "book", *args], body)


def get(kind, name):
    return json.loads(kube("get", kind, name, "-o", "json"))


def image_path(kind):
    prefix = "/spec/jobTemplate/spec/template" if kind == "cronjob" else "/spec/template"
    return prefix + "/spec/containers/0/image"


def container_image(resource, kind):
    spec = resource["spec"]["jobTemplate"]["spec"] if kind == "cronjob" else resource["spec"]
    containers = spec["template"]["spec"]["containers"]
    if len(containers) != 1:
        raise RuntimeError("Release expects one application container per workload")
    return containers[0]["image"]


def replace_image(kind, name, image, allowed):
    current = get(kind, name)
    if container_image(current, kind) not in allowed:
        raise RuntimeError(f"{name} changed since this release; refusing to overwrite it")
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
        {"op": "replace", "path": image_path(kind), "value": image},
    ]
    kube("patch", kind, name, "--type=json", "-p", json.dumps(patch))


def health():
    request = urllib.request.Request(CONFIG["health_url"], headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    if data.get("status") != "ok" or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", data.get("version", "")):
        raise RuntimeError("Invalid application health response")
    return data["version"]


def verify(version, expected):
    for name, image in expected.items():
        if container_image(get("deployment", name), "deployment") != image:
            raise RuntimeError(f"Unexpected image for {name}")
        kube("rollout", "status", f"deployment/{name}", "--timeout=180s")
    for attempt in range(12):
        try:
            if health() == version:
                return
        except (OSError, ValueError, RuntimeError):
            pass
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError("Public health/version verification failed")


def refresh_registry():
    password = run(["aws", "ecr", "get-login-password", "--region", CONFIG["region"]]).strip()
    host = CONFIG["registry"].split("/")[0]
    auth = base64.b64encode(("AWS:" + password).encode()).decode()
    data = json.dumps({"auths": {host: {"auth": auth}}})
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "ecr-pull", "namespace": "book"},
              "type": "kubernetes.io/dockerconfigjson", "data": {".dockerconfigjson": base64.b64encode(data.encode()).decode()}}
    kube("apply", "-f", "-", body=json.dumps(secret))


def save(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def suspend_jobs(suspended):
    for name, value in suspended.items():
        kube("patch", "cronjob", name, "--type=merge", "-p", json.dumps({"spec": {"suspend": value}}))


def rollback(state, path):
    refresh_registry()
    # Check every workload before changing any, so a newer release is never
    # partially reverted when one workload has changed independently.
    for kind, resources in state["previous"].items():
        for name, image in resources.items():
            current = container_image(get(kind, name), kind)
            if current not in {image, state["desired"][kind][name]}:
                raise RuntimeError(f"{name} belongs to a different release")
    for kind, resources in state["previous"].items():
        for name, image in resources.items():
            desired = state["desired"][kind][name]
            replace_image(kind, name, image, {image, desired})
    verify(state["previous_version"], state["previous"]["deployment"])
    suspend_jobs(state["suspended"])
    state["status"] = "rolled_back"
    save(path, state)
    print("Previous application release restored and verified")


def release(action, commit, db_tree):
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{40}", db_tree):
        raise ValueError("Full commit and database tree hashes required")
    if action not in {"deploy", "verify", "rollback"}:
        raise ValueError("Unknown release action")
    if db_tree != CONFIG["approved_db_tree"]:
        raise RuntimeError("Database files changed: complete a manual migration release before updating the CD baseline")
    directory = ROOT / ("cd-" + commit)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "release.json"
    state = json.loads(path.read_text()) if path.exists() else None
    if action == "rollback":
        if state is None:
            print("No application changes were started; nothing to restore")
            return
        rollback(state, path)
        return
    if action == "verify":
        if not state or state["status"] != "deployed":
            raise RuntimeError("Release has not completed")
        verify(commit, state["desired"]["deployment"])
        return
    if state:
        if state["status"] == "deployed":
            verify(commit, state["desired"]["deployment"])
            return
        if state["status"] != "rolled_back":
            raise RuntimeError("This release was interrupted; restore it before retrying")
        verify(state["previous_version"], state["previous"]["deployment"])
        path.rename(directory / ("release-" + str(time.time_ns()) + ".json"))

    images = {}
    for image in sorted(set(DEPLOYMENTS.values())):
        response = json.loads(run(["aws", "ecr", "describe-images", "--region", CONFIG["region"],
                                   "--repository-name", "private-books/" + image,
                                   "--image-ids", "imageTag=" + commit]))
        digest = response["imageDetails"][0]["imageDigest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError("Invalid image digest")
        images[image] = CONFIG["registry"] + "/" + image + "@" + digest
    previous = {"deployment": {}, "cronjob": {}}
    suspended = {}
    for name in DEPLOYMENTS:
        resource = get("deployment", name)
        if resource["spec"].get("replicas", 1) < 1:
            raise RuntimeError(f"{name} is stopped; finish maintenance before releasing")
        previous["deployment"][name] = container_image(resource, "deployment")
    for name in CRONJOBS:
        resource = get("cronjob", name)
        previous["cronjob"][name] = container_image(resource, "cronjob")
        suspended[name] = resource["spec"].get("suspend", False)
    previous_version = health()
    verify(previous_version, previous["deployment"])
    refresh_registry()
    state = {"status": "deploying", "commit": commit, "previous": previous,
             "previous_version": previous_version, "suspended": suspended,
             "desired": {"deployment": {name: images[image] for name, image in DEPLOYMENTS.items()},
                         "cronjob": {name: images["python"] for name in CRONJOBS}}}
    save(path, state)
    try:
        suspend_jobs({name: True for name in CRONJOBS})
        jobs = json.loads(kube("get", "jobs", "-o", "json"))["items"]
        if any(job.get("status", {}).get("active", 0) for job in jobs):
            raise RuntimeError("Maintenance job is active; retry after it finishes")
        for kind, resources in state["desired"].items():
            for name, image in resources.items():
                replace_image(kind, name, image, {previous[kind][name]})
        verify(commit, state["desired"]["deployment"])
        suspend_jobs(suspended)
        state["status"] = "deployed"
        save(path, state)
        print("All seven application deployments and both maintenance jobs updated")
    except Exception:
        try:
            rollback(state, path)
        except Exception:
            state["status"] = "rollback_failed"
            save(path, state)
            raise RuntimeError("Release and rollback failed; inspect workloads before resuming maintenance") from None
        raise


if __name__ == "__main__":
    CONFIG = json.loads(os.environ["BOOK_CD_CONFIG"])
    os.umask(0o077)
    with open("/var/lock/qireadr-release.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release(os.environ["BOOK_CD_ACTION"], os.environ["BOOK_CD_COMMIT"], os.environ["BOOK_CD_DB_TREE"])
