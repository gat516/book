from gateway_trial import render
import pytest
import yaml

ACCOUNT = "00000000-0000-4000-8000-000000000001"


def test_private_keyless_trial_and_narrow_rollout():
    resources, patches = render("registry/gateway:trial-123", "registry/python:trial-123", ACCOUNT)
    assert set(patches) == {"askai", "pipeline"}
    assert not any(item["kind"] in {"Secret", "Ingress", "Namespace"} for item in resources)
    config = next(item for item in resources if item["kind"] == "ConfigMap")
    policy = yaml.safe_load(config["data"]["config.yaml"])
    assert policy["server"]["mode"] == "admission"
    assert policy["redis"]["on_failure"] == "closed"
    assert set(policy["tenants"]) == {ACCOUNT}
    assert "credentials" not in policy["tenants"][ACCOUNT]
    assert policy["tenants"][ACCOUNT]["limits"]["deepseek"]["deepseek-v4-flash"]["capacity"] == 2
    for item in resources:
        if item["kind"] == "Deployment":
            pod = item["spec"]["template"]["spec"]
            assert not pod["automountServiceAccountToken"]
            assert not pod["enableServiceLinks"]
            assert "envFrom" not in pod["containers"][0]
        if item["kind"] == "Service":
            assert item["spec"].get("type", "ClusterIP") == "ClusterIP"
    policies = {item["metadata"]["name"]: item["spec"] for item in resources if item["kind"] == "NetworkPolicy"}
    assert policies["allow-gateway-redis"]["egress"] == []
    assert all("ipBlock" not in str(rule) for rule in policies["allow-gateway-admission"]["egress"])
    for name, patch in patches.items():
        container, = patch["spec"]["template"]["spec"]["containers"]
        assert container["name"] == name
        assert set(container) == {"name", "image", "env"}


@pytest.mark.parametrize("image", ["registry/gateway:latest", "registry/gateway", "registry/gateway:main"])
def test_mutable_or_missing_tags_rejected(image):
    with pytest.raises(ValueError):
        render(image, "registry/python:123", ACCOUNT)
