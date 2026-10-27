# Published Copper × Admiral demo

Published and independently checked on **27 October 2026**. This is a private,
Linux ARM64 workload, not a device rollout.

## Release identity

- Package: `ghcr.io/admrlos/workload-copper`
- Immutable tag: `sha-b7e9f03c5318e704e61103dda1d694eb7612d246`
- Convenience tag: `latest` (same digest at publication)
- Frozen implementation commit: `b7e9f03c5318e704e61103dda1d694eb7612d246`
- Manifest digest: `sha256:baa251eb91d75d195e2068d0a94c9318c1152759e20354fb9025cde2e7728d2a`
- Platform: `linux/arm64`
- Copper runtime: `1.3.1`; build/export dependencies are pinned separately in `Cargo.lock`.

Use the immutable reference for deployment:

```text
ghcr.io/admrlos/workload-copper@sha256:baa251eb91d75d195e2068d0a94c9318c1152759e20354fb9025cde2e7728d2a
```

Authenticated OCI retrieval and the GitHub package API confirmed the digest,
architecture, revision label and **private** visibility. An anonymous pull-token
request returned HTTP 401. The image was pulled back by digest and every packaged
source/UI checksum passed. No credentials are embedded in the image or repository.

## Admiral Configuration created

| Setting | Verified value |
| --- | --- |
| Organisation | `Admiral-Demo` / `7bd93e9d-c05a-4bbd-961f-afe73d8913e5` |
| Configuration | `copper-autonomy` |
| ID | `4d8886d1-fc65-4b64-8faa-7a4b695f0936` |
| Version / generation | `1` / `1` |
| Registry credential | Existing `GHCR- Alex PAT` / `8927588f-297a-424a-acae-6fc87e4a1de6` |
| Image | Immutable digest above |
| Desired state | `RUNNING` when assigned |
| HTTP listener | `8080/tcp` |
| Persistent volume | Named `data`, destination `/data`, `rbind,rw` |
| Episode | 800 cycles, 20 Hz target |
| Retention | Four run directories; 134217728-byte aggregate budget |
| Host hardware / network namespaces / TTY | Not requested |
| Fleet assignments | None |

The complete new Configuration was read back from Admiral and compared with the
requested spec, allowing only known empty-list serialization differences. The
Alex credential was referenced by ID, not copied or changed.

**No fleet selection, device restart or Jetson deployment was performed.** The
existing `orin-cuda-debug` fleet still selected Configuration
`9e598960-1a24-4275-956d-f1f06e3eb8e4` after provisioning. Creating this Configuration
does not prove the device has pulled the private image; that requires an approved
rollout and device-side acceptance.

## What passed on the exact release image

- Rust builder: seven unit tests and eleven integration tests, including native
  corruption/unclean-close/missing-slab rejection and real task invocation checks.
- Host Python suite: 27 tests; frontend suite: five tests. The runtime image also
  passed its packaged Python service/boot tests.
- Measured live cadence: **19.90 Hz** on local ARM64 Docker, not on the Jetson.
- Live 800-cycle mission: **four goals, zero collisions**.
- Downloaded native-log archive: safely extracted and re-executed with **800
  cycles, zero mismatches**.
- Longer native replay: **1200 cycles, zero mismatches**.
- Deliberately changed expected controller output: replay exited unsuccessfully
  with one mismatch; it was not marked verified.
- Supervisor and engine: effective UID/GID 10001, `NoNewPrivs=1`.
- Container restart: verified recording restored from the persistent volume.
- Independent native-integrity re-review: original corruption fixtures failed
  closed while valid 800/1200-cycle recordings verified.

Machine-readable evidence lives in the ignored local `artifacts/` directory:

- `release-image-acceptance.json`
- `registry-verification.json`
- `admiral-configuration.json`

These are validation artifacts, not files required by the runtime image.

## Demonstration

A local preview of the published digest runs as `copper-ui-preview` at
`http://127.0.0.1:18080`, using the persistent Docker volume
`copper-ui-preview-data`. It is bound to loopback, not exposed to the public network.

1. Open the dashboard in **Live** mode. Show the real Copper sensor → A* planner →
   safety controller → telemetry graph and the rover navigating static shelves
   and a moving obstacle. All displayed poses and commands come from the engine.
2. After an episode finishes, choose its verified recording and click **Verified
   replay**. Show the cycle count and zero-mismatch native verification result.
3. Pause and scrub: these are regenerated native replay frames. The browser is
   only displaying them, not running Copper itself.
4. Download the log archive. Use the native replay commands in `README.md` to
   reproduce the decisions offline using the same release image.
5. Explain the boundary: Admiral delivers and supervises a versioned edge
   workload and preserves its data; Copper runs, records and re-executes the
   robot's decisions. Physical sensor/actuator tasks can later replace the
   simulation boundary without turning the UI into the control system.

This is simulated CPU autonomy, not CUDA inference, a learned policy, a physical
robot demonstration or a hard-real-time performance claim. No runtime downloads,
ROS installation, GPU passthrough or robot hardware are required.

## Before a Jetson rollout

Get approval to change the fleet first. Refresh its current selection rather than
assuming the value above is still current. Then select `copper-autonomy` version
1 and complete the device-side checklist in `README.md`: exact digest applied,
healthy advancing telemetry, full verified mission, usable UI/download and
persistent recordings after restart. Measure Jetson CPU/memory/cycle timing there.

The dashboard is read-only but unauthenticated. Use a trusted demo network or
authenticated Admiral remote access, not a public internet listener.
