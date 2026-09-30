"""Invoke the fixed release document and wait for its actual result."""
import argparse
import json
from pathlib import Path
import subprocess
import time

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())


def aws(*args):
    return json.loads(subprocess.check_output(["aws", "--region", CONFIG["region"], *args], text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "verify", "rollback"))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--db-tree", required=True)
    args = parser.parse_args()
    response = aws("ssm", "send-command", "--instance-ids", CONFIG["instance"],
                   "--document-name", CONFIG["document"], "--timeout-seconds", "60",
                   "--parameters", json.dumps({"Action": [args.action], "Commit": [args.commit], "DbTree": [args.db_tree]}))
    command = response["Command"]["CommandId"]
    print("SSM command:", command, flush=True)
    for _ in range(180):
        time.sleep(5)
        try:
            result = aws("ssm", "get-command-invocation", "--command-id", command,
                         "--instance-id", CONFIG["instance"])
        except subprocess.CalledProcessError:
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        print(result.get("StandardOutputContent", ""))
        if result["Status"] != "Success":
            print(result.get("StandardErrorContent", ""))
            raise SystemExit("Release command ended with " + result["Status"])
        return
    raise SystemExit("SSM result timed out; check the command before starting another release")


if __name__ == "__main__":
    main()
