# Customer evaluation: Copper rover on Admiral

## Demonstration scope

A native Linux ARM64 Copper 1.3.1 warehouse-rover simulation. Copper runs sensor, planner and safety-controller tasks; the browser only visualizes observations. This workload needs no GPU, camera or physical robot and demonstrates no CUDA acceleration.

## Quick evaluation

From a clean checkout with ARM64-capable Docker Buildx, Python 3.12+ and Node:

```sh
python3 -m unittest discover -s tests -v
node --test www/*.test.js
docker buildx build --platform linux/arm64 --load \
  --build-arg IMAGE_REVISION="$(git rev-parse HEAD)" -t copper-rover:evaluation .
python3 tests/check_image.py copper-rover:evaluation
docker run --rm --name copper-rover-demo -p 127.0.0.1:8080:8080 \
  -v copper-rover-evaluation:/data copper-rover:evaluation
```

Open the local port 8080. Show live robot state first. After a complete episode and native verification, open **Verified replay**, pause/scrub and download the recording. Replay re-executes planner/controller tasks from recorded source observations and rejects mismatches. Browser playback itself does not execute Copper.

The exact existing ARM64 release was re-exercised on October 27, 2026: 800-cycle downloaded native replay and 1200-cycle long replay both had zero mismatches; four goals, zero collisions; deliberately altered expected output was rejected. Service/engine used UID/GID 10001 and NoNewPrivs=1; restart restored the verified recording. This is local ARM64 Docker evidence, not Jetson or fleet-scale evidence. See [PUBLISHED.md](../PUBLISHED.md) for the immutable runtime image and original release provenance.

## Admiral deployment

1. Register an approved private registry credential by reference, never inline.
2. Create a customer-scoped Configuration with an immutable Linux ARM64 image digest, persistent `/data`, port 8080/tcp, and no privileged mode, host namespaces or hardware access.
3. Add the named customer EdgeWire service below if remote browser access is needed.
4. Save the prior Configuration selection, canary one approved device and verify exact applied digest, advancing telemetry, complete native replay and persisted data before promotion.
5. Promote that same immutable image to further approved devices. Measure rollout capacity and concurrent operation separately; fleet scale has not been benchmarked by this demo.

```yaml
spec:
  edgewire:
    services:
      - name: web
        protocol: tcp
        port: 8080
```

This is only the service fragment, not a complete Admiral API creation payload. Use your own organization/device/configuration/registry identifiers; historical operator IDs are not customer defaults.

```sh
admrl edgewire forward --org "$ADMRL_ORG" --no-login \
  "$ADMRL_DEVICE" web 127.0.0.1:18080
```

Keep that process running and open `http://127.0.0.1:18080`. The service target is **web**, not **8080**. The app is read-only but unauthenticated, so keep it behind authenticated Admiral access or a trusted evaluation network—not a public listener.

## Persistence, failure and rollback

Use `/data` as persistent storage. Defaults retain four runs within 128 MiB; export evidence before retention removes it. The supervisor exits nonzero on stalled recording or failed replay, so Admiral can recover the workload. A new image must preserve the same volume destination. Roll back by reselecting the saved prior Configuration version and verifying application progress; do not assume a completed rollout remains reversible by rollout ID.

## What is ready to share

Source, lockfiles, build recipe, tests and replay instructions are provided for evaluation. GitHub and GHCR are private and require separate read/pull access. Obtain the customer's GitHub accounts/team and Admiral organization/device permissions before inviting them. No customer invitations or fleet-wide rollout are performed by these instructions.

The evaluation demonstrates a repeatable deployment/evidence workflow, not physical-robot safety, hard-real-time guarantees, customer hardware acceptance, or a quantified fleet-scale deployment-speed improvement. Preserve LICENSE/NOTICE and third-party branding rights.
