#!/usr/bin/env python3
"""Render a private, opt-in admission trial; never applies or contacts AWS."""
import argparse
import re
from uuid import UUID

import yaml


def render(gateway_image: str, python_image: str, account: str):
    account = str(UUID(account))
    for image in (gateway_image, python_image):
        if not re.fullmatch(r"[a-zA-Z0-9./_-]+:[a-zA-Z0-9_.-]+", image) or image.rsplit(":", 1)[1] in {"latest", "main", "master"}:
            raise ValueError("immutable image tags required")
    resources = []
    def add(kind, name, spec=None, **extra):
        api = {"Deployment": "apps/v1", "NetworkPolicy": "networking.k8s.io/v1"}.get(kind, "v1")
        obj = {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": "book"}, **extra}
        if spec is not None:
            obj["spec"] = spec
        resources.append(obj)

    # §14.7: operator safety cap, NOT a claim about DeepSeek's provider limit.
    config = {
        "server": {"mode": "admission", "grpc_addr": "0.0.0.0:8081", "metrics_addr": "0.0.0.0:9090"},
        "redis": {"addr": "gateway-redis:6379", "on_failure": "closed", "replica_count": 1, "key_prefix": "qireadr-admission"},
        "admission": {"lease_timeout": "180s", "batch_lease_timeout": "26h", "sweeper_interval": "5s",
                      "batch_sweeper_interval": "5s", "hosted_local_leasing": {"enabled": False}},
        "tenants": {account: {"limits": {"deepseek": {"deepseek-v4-flash": {
            "controller": "semaphore", "cost_unit": "inflight_requests", "capacity": 2, "lease_timeout": "180s"}}}}},
        "providers": {"deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com"}},
        "router": {"max_fallbacks": 0, "cooldown_initial": "1s", "cooldown_max": "5s",
                   "default_candidates": [{"provider": "deepseek", "model": "deepseek-v4-flash"}]},
        "gpu": {"enabled": False},
    }
    add("ConfigMap", "gateway-admission-config", data={"config.yaml": yaml.safe_dump(config)})
    add("PersistentVolumeClaim", "gateway-redis-data", {"accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path", "resources": {"requests": {"storage": "1Gi"}}})
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    for name, image, args, port, memory, volume in [
        ("gateway-admission", gateway_image, ["-config", "/etc/gateway/config.yaml"], 8081, "128Mi",
         {"name": "config", "configMap": {"name": "gateway-admission-config"}}),
        ("gateway-redis", "redis:7.4-alpine", ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec",
          "--maxmemory", "64mb", "--maxmemory-policy", "noeviction", "--save", ""], 6379, "256Mi",
         {"name": "data", "persistentVolumeClaim": {"claimName": "gateway-redis-data"}}),
    ]:
        probe = {"grpc": {"port": port, "service": "llmgw.v1.Admission"}} if name == "gateway-admission" else {"tcpSocket": {"port": port}}
        container = {"name": name, "image": image, "args": args, "ports": [{"containerPort": port}],
            "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": memory}},
            "securityContext": security, "readinessProbe": {**probe, "periodSeconds": 5},
            "startupProbe": {**probe, "periodSeconds": 2, "failureThreshold": 60},
            "volumeMounts": [{"name": volume["name"], "mountPath": "/etc/gateway" if name == "gateway-admission" else "/data"}]}
        labels = {"app": name}
        add("Deployment", name, {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {"enableServiceLinks": False,
                "automountServiceAccountToken": False, "imagePullSecrets": [{"name": "ecr-pull"}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 999 if name == "gateway-redis" else 65532,
                    "runAsGroup": 999 if name == "gateway-redis" else 65532, "fsGroup": 999 if name == "gateway-redis" else 65532,
                    "seccompProfile": {"type": "RuntimeDefault"}}, "containers": [container], "volumes": [volume]}}})
        add("Service", name, {"selector": labels, "ports": [{"port": port}]})
        callers = ["pipeline", "askai"] if name == "gateway-admission" else ["gateway-admission"]
        ingress = [{"from": [{"podSelector": {"matchLabels": {"app": caller}}} for caller in callers],
                    "ports": [{"protocol": "TCP", "port": port}]}]
        egress = []
        if name == "gateway-admission":
            egress = [
                {"to": [{"podSelector": {"matchLabels": {"app": "gateway-redis"}}}], "ports": [{"protocol": "TCP", "port": 6379}]},
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                         "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                 "ports": [{"protocol": proto, "port": 53} for proto in ("UDP", "TCP")]},
            ]
        add("NetworkPolicy", "allow-" + name, {"podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress", "Egress"], "ingress": ingress, "egress": egress})
    # Strategic merge patches, NOT standalone deployments: preserve secrets, RLS,
    # replica counts, probes, service links and unrelated production configuration.
    patches = {name: {"spec": {"template": {"spec": {"containers": [{"name": name,
        "image": python_image, "env": [
            {"name": "LLM_GATEWAY_ADMISSION_ADDR", "value": "gateway-admission:8081"},
            {"name": "LLM_GATEWAY_ADMISSION_ACCOUNTS", "value": account},
        ]}]}}}} for name in ("askai", "pipeline")}
    return resources, patches


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--patch", choices=["askai", "pipeline"])
    args = parser.parse_args()
    resources, patches = render(args.gateway_image, args.python_image, args.account)
    print(yaml.safe_dump(patches[args.patch]) if args.patch else yaml.safe_dump_all(resources))
