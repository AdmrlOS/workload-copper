# Copper × Admiral: replayable autonomy on Jetson

A hardware-free warehouse-rover demo packaged as one Linux ARM64 Admiral workload.

For customer evaluation and private access, start with [the customer guide](docs/CUSTOMER_GUIDE.md).
The historical deployment identifiers in `PUBLISHED.md` are operator provenance, not customer defaults.
The rover's decisions run in **Copper 1.3.1**, not in browser animation code. A
read-only mission-control UI displays the generated trajectory and verified replay.

**Scope:** this is a 2D simulated robot with CPU navigation/control. It does not
control actuators, access CAN, require a camera, use CUDA, or run a learned policy.
It is an original integration example inspired by Copper's replay-first workflow,
not a packaged application from the community-showcase video. A working local
ARM64 container does not by itself establish real Jetson hardware acceptance.

## What the demo shows

1. Copper executes the simulated sensor → planner → safety controller → telemetry
   task graph at a target of 20 Hz. The planner computes paths around warehouse
   obstacles rather than following a pre-baked browser trajectory.
2. The UI shows robot pose, target, computed path, command, safety state, and cycle
   timing from actual engine telemetry.
3. Each episode writes binary Copper recordings and live JSONL into persistent
   `/data/runs/<run-id>/`.
4. After each episode the native replay command reads the Copper recording,
   injects recorded source messages, re-executes the downstream tasks, and compares
   regenerated decisions. A mismatch fails the command and the workload; no CPU,
   mocked-data, or browser-only playback is substituted for that verification.
5. **Verified replay** in the UI visualizes those regenerated frames. Pause and
   scrub them, or download an archive containing the native logs, live/replay
   telemetry, verification report, and engine logs.

The distinction matters: browser scrubbing is a visualization of a completed
native replay, not a claim that the browser itself is executing Copper.

## Why the combination is useful

| Admiral | Copper |
| --- | --- |
| Device OS, fleet inventory and lifecycle | Robot task graph and control pipeline |
| Private OCI image delivery and versioned configuration | Typed messages and runtime scheduling |
| Persistent workload volumes | Binary recordings of Copper execution |
| Remote support and workload exec | Re-execution and comparison of decisions |

The demonstration story is: ship a versioned autonomy stack to an edge device,
keep its execution evidence outside the image, retrieve a recording, and reproduce
its decisions without reconnecting the robot's sensors or actuators.

## Local build and run

Docker Desktop on ARM64 is sufficient; Rust is compiled inside the pinned build
image. No Rust installation on the Jetson or Docker daemon inside Admiral is
needed. The runtime requires no network downloads.

```sh
docker buildx build --platform linux/arm64 --load \
  --build-arg IMAGE_REVISION="$(git rev-parse HEAD)" \
  -t workload-copper:development .
python3 -m unittest discover -s tests -v
node --test www/*.test.js
python3 tests/check_image.py workload-copper:development

docker run --rm --name copper-demo -p 127.0.0.1:8080:8080 \
  -v copper-demo-data:/data workload-copper:development
```

Open the local address on port 8080. The first verified replay becomes available
once the first 800-cycle episode finishes (about 40 seconds plus replay time).
The map and live state appear before that. The published image should always be
selected by immutable digest in Admiral; see `PUBLISHED.md` after publication.

## Container and persistence contract

- HTTP port: **8080/tcp**, `/` dashboard, `/api/state`, `/healthz`.
- Persistent writable volume: **`/data`**. Native logs must stay outside the
  replaceable root filesystem.
- Startup prepares only `/data` and `/data/runs`, then drops the service and engine
  to UID/GID **10001**, clears supplementary groups, and disallows new privileges.
- No privileged mode, host device access, NVIDIA driver mounts, ROS installation,
  SSH daemon, or Linux display stack is required.
- Normal stop interrupts only the active recording. Previously verified episodes
  are restored after restart; incomplete episodes are not offered as verified.
- Retention defaults to four run directories and a 128 MiB total budget. Old
  app-owned run directories are pruned; unrelated data is left alone. Export any
  recording you want to retain before it ages out. The active episode is protected
  from pruning; exceeding its budget fails explicitly.
- Health fails for stale live telemetry or an engine/replay failure. The supervisor
  exits nonzero if an engine command or replay verification fails.
- The UI is read-only and **unauthenticated**. Expose it only on a trusted demo
  network or through authenticated Admiral remote access, not on the public internet.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8080` | HTTP listener |
| `COPPER_STEPS` | `800` | Cycles per episode, 20–5000 |
| `COPPER_KEEP_RUNS` | `4` | Retained run directories, 1–16 |
| `COPPER_MAX_BYTES` | `134217728` | Total run budget, 16 MiB–1 GiB |
| `COPPER_DATA_DIR` | `/data` | Persistent directory, must be under `/data` |

## Record and replay manually

Run these inside the workload with Admiral exec, or in an isolated local
container. Use a new directory for each recording; do not overwrite an active run.

```sh
mkdir -p /data/manual
copper-rover record --log-base /data/manual/mission.copper \
  --telemetry /data/manual/live.jsonl --steps 1200
copper-rover replay --log-base /data/manual/mission.copper \
  --telemetry /data/manual/replay.jsonl \
  --expected /data/manual/live.jsonl \
  --report /data/manual/verification.json
```

Without `--realtime`, recording runs unpaced. Replaying is always offline and does
not advance the simulated world or touch physical I/O. Verification excludes
wall-clock timings but checks regenerated deterministic outputs.

## Admiral configuration and Jetson acceptance

The intended target is the NVIDIA Jetson Orin Nano in Admiral-Demo. Creating a
Configuration is separate from selecting it on a fleet; this project does not
silently replace the Jetson's current NixOS workload. Preserve any fleet changes
made after this build and get approval before a rollout.

The configuration must use the private GHCR image digest, the existing **Alex
PAT** registry credential reference (never put the PAT in this repo), persistent
`/data`, and the HTTP listener on port 8080. No host hardware devices are needed.
The applied configuration and exact registry/image verification are documented
in `PUBLISHED.md` after successful publication.

After an explicitly approved rollout, verify:

1. Admiral live state has the selected configuration version, `RUNNING`, the exact
   image digest, and `assignedConfigApplied=true`.
2. The device's `/healthz` succeeds and `/api/state` advances ticks.
3. A full episode completes, native replay reports zero mismatches, the robot
   reaches a goal, and collision count remains zero.
4. Open the device's dashboard, test live/replay/scrub/download, then restart and
   confirm verified recordings persist.
5. Record CPU/memory and cycle timing **on the Jetson**. Local Docker timing is not
   a claim of hard real-time scheduling or Jetson performance.

## Build provenance and license

Copper SDK and transitive Rust dependencies are locked in `Cargo.lock`; the Rust
and Debian base images are pinned by digest. APT tracks the Debian repositories,
so use the published image digest for exact deployed reproduction. The compiled
engine has no on-start crate or model downloads. Source and packaged checksums
are included under `/opt/copper/source` and `/opt/copper/packaged-sha256.txt`.

Apache-2.0; see `LICENSE` and `NOTICE`. Upstream reference:
https://github.com/copper-project/copper-rs and its Jetson/replay documentation.
