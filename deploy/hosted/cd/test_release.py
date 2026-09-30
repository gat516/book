import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configure
import frontend
import node

COMMIT = "a" * 40
DB = "b" * 40
DIGEST = "sha256:" + "c" * 64


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.resources = {}
        self.patches = []
        self.active_jobs = False
        self.fail_new = False
        self.fail_old = False
        for kind, names in (("deployment", node.DEPLOYMENTS), ("cronjob", node.CRONJOBS)):
            for name in names:
                pod = {"template": {"spec": {"containers": [{"image": "old/" + name}]}}}
                spec = {"jobTemplate": {"spec": pod}, "suspend": name == "account-cleanup"} if kind == "cronjob" else {**pod, "replicas": 1}
                self.resources[kind, name] = {"metadata": {"resourceVersion": "1"}, "spec": spec}
        for target, value in (("ROOT", self.root), ("CONFIG", {"approved_db_tree": DB, "registry": "registry/private-books", "region": "us-east-1"}),
                              ("get", self.get), ("kube", self.kube), ("run", self.run_command), ("health", self.health)):
            handle = patch.object(node, target, value, create=True)
            handle.start()
            self.addCleanup(handle.stop)
        sleeper = patch.object(node.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def get(self, kind, name):
        return copy.deepcopy(self.resources[kind, name])

    def run_command(self, args, body=None):
        if "describe-images" in args:
            return json.dumps({"imageDetails": [{"imageDigest": DIGEST}]})
        if "get-login-password" in args:
            return "test-only-password"
        self.fail("Unexpected external command")

    def kube(self, *args, body=None):
        if args[:2] == ("apply", "-f"):
            return ""
        if args[:2] == ("get", "jobs"):
            return json.dumps({"items": [{"status": {"active": int(self.active_jobs)}}]})
        if args[:2] == ("rollout", "status"):
            return ""
        if args[0] == "patch":
            kind, name = args[1:3]
            resource = self.resources[kind, name]
            value = json.loads(args[-1])
            if isinstance(value, dict):
                resource["spec"]["suspend"] = value["spec"]["suspend"]
            else:
                self.assertEqual(value[0]["value"], resource["metadata"]["resourceVersion"])
                spec = resource["spec"]["jobTemplate"]["spec"] if kind == "cronjob" else resource["spec"]
                spec["template"]["spec"]["containers"][0]["image"] = value[1]["value"]
                self.patches.append((kind, name))
            resource["metadata"]["resourceVersion"] = str(int(resource["metadata"]["resourceVersion"]) + 1)
            return ""
        self.fail(f"Unexpected Kubernetes command: {args}")

    def health(self):
        old = node.container_image(self.get("deployment", "reader-api"), "deployment").startswith("old/")
        if (old and self.fail_old) or (not old and self.fail_new):
            return "wrong-version"
        return "previous" if old else COMMIT

    def state(self):
        return json.loads((self.root / ("cd-" + COMMIT) / "release.json").read_text())

    def test_deploys_all_workloads_and_restores_original_cron_suspension(self):
        node.release("deploy", COMMIT, DB)
        self.assertEqual(len(self.patches), 9)
        self.assertEqual(self.state()["status"], "deployed")
        self.assertFalse(self.resources["cronjob", "portable-backup"]["spec"]["suspend"])
        self.assertTrue(self.resources["cronjob", "account-cleanup"]["spec"]["suspend"])
        node.release("deploy", COMMIT, DB)
        self.assertEqual(len(self.patches), 9, "Successful retries must not replace the rollback snapshot")

    def test_database_changes_cannot_modify_workloads(self):
        with self.assertRaisesRegex(RuntimeError, "Database files changed"):
            node.release("deploy", COMMIT, "d" * 40)
        self.assertEqual(self.patches, [])

    def test_failed_health_restores_every_previous_image(self):
        self.fail_new = True
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            node.release("deploy", COMMIT, DB)
        self.assertEqual(self.state()["status"], "rolled_back")
        for (kind, name), resource in self.resources.items():
            self.assertEqual(node.container_image(resource, kind), "old/" + name)

    def test_completed_rollback_can_retry_without_losing_history(self):
        self.fail_new = True
        with self.assertRaises(RuntimeError):
            node.release("deploy", COMMIT, DB)
        self.fail_new = False
        node.release("deploy", COMMIT, DB)
        self.assertEqual(self.state()["status"], "deployed")
        self.assertEqual(len(list((self.root / ("cd-" + COMMIT)).glob("release-*.json"))), 1)

    def test_wrong_restored_version_is_not_successful_recovery(self):
        node.release("deploy", COMMIT, DB)
        self.fail_old = True
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            node.release("rollback", COMMIT, DB)
        self.assertNotEqual(self.state()["status"], "rolled_back")

    def test_rollback_does_not_partially_overwrite_a_newer_release(self):
        node.release("deploy", COMMIT, DB)
        resource = self.resources["deployment", "web"]
        resource["spec"]["template"]["spec"]["containers"][0]["image"] = "newer/web"
        before = copy.deepcopy(self.resources)
        with self.assertRaisesRegex(RuntimeError, "different release"):
            node.release("rollback", COMMIT, DB)
        self.assertEqual(self.resources, before)

    def test_active_maintenance_prevents_new_images(self):
        self.active_jobs = True
        with self.assertRaisesRegex(RuntimeError, "Maintenance job"):
            node.release("deploy", COMMIT, DB)
        for (kind, name), resource in self.resources.items():
            self.assertEqual(node.container_image(resource, kind), "old/" + name)

    def test_missing_transaction_cannot_guess_rollback_target(self):
        node.release("rollback", COMMIT, DB)
        self.assertEqual(self.patches, [])


class FrontendTests(unittest.TestCase):
    def test_split_traffic_is_not_treated_as_a_single_rollback_target(self):
        with patch.object(frontend, "api", return_value={"deployments": [{"versions": [{"version_id": "one", "percentage": 50}]}]}):
            with self.assertRaisesRegex(RuntimeError, "one frontend version"):
                frontend.active_version()

    def test_rollback_preserves_a_different_frontend_release(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"previous_version": "old", "commit": COMMIT}))
            with patch.object(frontend, "STATE", state), patch.object(frontend, "active_version", return_value="newer"), \
                 patch.object(frontend, "api", return_value={"annotations": {"workers/tag": "unrelated"}}) as api:
                with self.assertRaisesRegex(RuntimeError, "outside this release"):
                    frontend.rollback()
                self.assertEqual(api.call_count, 1)

    def test_aws_role_cannot_invoke_arbitrary_shell_documents(self):
        _, trust, policy, document = configure.resources()
        conditions = trust["Statement"][0]["Condition"]["StringEquals"]
        self.assertEqual(conditions["token.actions.githubusercontent.com:sub"], "repo:gat516/book:ref:refs/heads/main")
        command = next(item for item in policy["Statement"] if "ssm:SendCommand" in item["Action"])
        self.assertNotIn("*", command["Resource"])
        self.assertTrue(any(item.endswith(":document/qireadr-release") for item in command["Resource"]))
        self.assertNotIn("AWS-RunShellScript", json.dumps(command))
        self.assertEqual(document["parameters"]["Commit"]["allowedPattern"], "^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
