# sandbox

The worker image (`Dockerfile`, tag from `sandbox.image` in config, built with `codeit sandbox build`). The code that runs containers lives in `src/codeit/sandbox/`. See ADR-0009.

Pinned versions (build args): Playwright 1.63.0 (base image), Claude Code 2.1.282, OpenCode 1.18.32, uv 0.12.19. Target repos must pin `@playwright/test` to the same Playwright version.

The egress proxy (`compose.yaml`, `tinyproxy.conf`) lands in **M7**.
