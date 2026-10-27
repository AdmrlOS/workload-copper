#!/usr/bin/env python3
"""Create one new, unassigned Configuration; never modify a fleet or credential."""
import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ORG = "7bd93e9d-c05a-4bbd-961f-afe73d8913e5"
CREDENTIAL = "8927588f-297a-424a-acae-6fc87e4a1de6"


def build_configuration(image: str) -> dict[str, Any]:
    if not re.fullmatch(r"ghcr\.io/admrlos/workload-copper@sha256:[0-9a-f]{64}", image):
        raise ValueError("an exact immutable workload-copper GHCR digest is required")
    return dict(name="copper-autonomy",
                description="Copper 1.3.1 ARM64 simulated warehouse autonomy with verified native replay; CPU-only, no actuators",
                tags=["copper", "arm64", "autonomy", "simulation"],
                spec=dict(image=image, desiredState="RUNNING", registry_credential_id=CREDENTIAL,
                          command=[], environment={"PORT": "8080", "COPPER_STEPS": "800",
                                                   "COPPER_KEEP_RUNS": "4", "COPPER_MAX_BYTES": "134217728"},
                          mounts=[], ports=[dict(port=8080, protocol="tcp")],
                          volumes={"data": dict(source="", destination="/data", options=["rbind", "rw"])},
                          options=dict(tty=False, all_devices_allowed=False, host_namespaces=[],
                                       host_resolvconf=False, host_hosts_file=False, read_only_root=False,
                                       ephemeral_root=False, privileged=False, capabilities=[],
                                       new_privileges=False, parent_cgroup_devices=False,
                                       exposeDBus=False, disableHostBluetooth=False)))


def normalized_spec(value, path=()):
    """Canonicalize unordered collections, preserving all fields and command order."""
    if isinstance(value, dict):
        value = dict(value)
        # Go's BSON omitempty loses empty command/mount slices; JSON reloads them as null.
        # An absent/empty host namespace list means isolated networking in both forms.
        empty_lists = ("command", "mounts") if not path else ("host_namespaces",) if path == ("options",) else ()
        for key in empty_lists:
            if value.get(key) is None:
                value[key] = []
        return {key: normalized_spec(item, (*path, key)) for key, item in value.items()}
    if isinstance(value, list):
        items = [normalized_spec(item, path) for item in value]
        if path in {("ports",), ("mounts",), ("devices",),
                    ("options", "capabilities"), ("options", "host_namespaces")} or \
                (len(path) == 3 and path[0] == "volumes" and path[-1] == "options"):
            items.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return items
    return value


def verify_readback(document, payload):
    expected = json.dumps(normalized_spec(payload["spec"]), sort_keys=True, allow_nan=False)
    actual = json.dumps(normalized_spec(document.get("spec")), sort_keys=True, allow_nan=False)
    if actual != expected:
        # Do not include response values: environment fields could contain secrets.
        raise RuntimeError("read-back configuration spec differs from the full requested spec")
    if document.get("metadata", {}).get("name") != payload["name"]:
        raise RuntimeError("read-back Configuration name differs from the requested name")
    if document.get("status", {}).get("fleetUsage"):
        raise RuntimeError("new Configuration unexpectedly has fleet usage")


def api(path, payload=None):
    headers = {"X-Organization-ID": ORG,
               "X-API-Token-ID": os.environ["ADMRL_API_TOKEN_ID"],
               "X-API-Secret-Key": os.environ["ADMRL_API_SECRET_KEY"],
               "User-Agent": "Mozilla/5.0 Copper-Admiral-Demo", "Content-Type": "application/json"}
    request = urllib.request.Request("https://api.admrl.co/v1" + path, headers=headers,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?")
    parser.add_argument("--apply", action="store_true", help="Create a NEW Configuration, with no fleet assignment")
    parser.add_argument("--credential-check", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/admiral-configuration.json"))
    args = parser.parse_args()
    credential_response = api("/registry-credentials/" + CREDENTIAL)
    credential = credential_response.get("data", credential_response)
    if credential.get("name") != "GHCR- Alex PAT" or credential.get("registry") != "ghcr.io":
        raise RuntimeError("the expected Alex GHCR credential could not be confirmed")
    print(json.dumps({key: credential.get(key) for key in ("id", "name", "registry", "username", "has_password")}))
    if args.credential_check and not args.image:
        return
    payload = build_configuration(args.image or "")
    if not args.apply:
        print(json.dumps(payload, indent=2))
        return
    listed = json.loads(subprocess.run(["admrl", "get", "configurations", "--org", ORG,
                                       "--no-login", "-o", "json"], check=True, capture_output=True, text=True).stdout)
    if any(row["name"] == payload["name"] for row in listed["items"]):
        raise RuntimeError("copper-autonomy already exists; refusing to overwrite or create a duplicate")
    created = api("/configurations", payload)
    metadata = created.get("data", created)
    configuration_id = metadata["id"]
    document = api("/configurations/" + configuration_id + "/document")
    document = document.get("data", document)
    verify_readback(document, payload)
    # Save only metadata and non-secret configuration. The server returns a credential reference, not the PAT.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps(dict(id=configuration_id, name=document["metadata"]["name"],
                         image=document["spec"]["image"], fleetUsage=document.get("status", {}).get("fleetUsage", []))))


if __name__ == "__main__":
    main()
