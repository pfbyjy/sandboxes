# Kubernetes Environment Plan — Local (kind/minikube)

## Goal
- Implement a `KubernetesEnvironment` that runs trials on a local Kubernetes cluster (kind or minikube) with parity to Docker semantics: long‑running pod, `exec`, file upload/download, and proper verifier flow.
- Support building images from the task’s `environment/` Dockerfile without a registry by loading images directly into the cluster.
- Provide deterministic image caching keyed by a content hash, with `force_build` to bypass cache.

## Repository Changes
- Add `KUBERNETES` to `src/sandboxes/models/environment_type.py`.
- Create `src/sandboxes/environments/kubernetes.py` defining `class KubernetesEnvironment(BaseEnvironment)`.
  - `type() -> EnvironmentType.KUBERNETES`
  - `is_mounted = False`
  - `_validate_definition()`
  - Lifecycle: `start(force_build)`, `stop(delete)`
  - IO: `upload_file`, `upload_dir`, `download_file`, `download_dir`
  - `exec(command, cwd, env, timeout_sec)`
- Register in `src/sandboxes/environments/factory.py` by adding `KubernetesEnvironment` to `_ENVIRONMENTS`.

## Assumptions & Prerequisites
- Tools available on host: `kubectl`, `docker`, and either `kind` or `minikube`.
- Cluster access via current kubeconfig/context; no registry required.

## Image Build & Caching
- Build context: the task’s `environment/` directory.
- Compute a deterministic `build_hash` for cache keys:
  - Walk `environment/` deterministically (sorted paths), hash relative paths and file contents (SHA‑256), combine to a single hex digest.
  - Optional future improvement: honor `.dockerignore`.
- Image tag format: `<task-name>:<hash12>` where `hash12` is the first 12 chars of the digest.
- Cache policy:
  - If `--no-force-build` and `docker image inspect <image>` succeeds, reuse image.
  - Else, rebuild with `docker buildx build -t <image> <context> --load`.
  - Labels (optional): `org.sbx.env_hash=<hash>`, `org.sbx.task=<task_name>`, `org.sbx.env=<environment_name>`.

## Cluster Handling (kind/minikube)
- Select cluster mode:
  - If `cluster` kwarg provided (`kind|minikube|auto`) use it.
  - Else auto‑detect: prefer kind if `kind get clusters` returns non‑empty; else minikube if `minikube status` is Running; otherwise error.
- Load image into cluster:
  - kind: `kind load docker-image <image> [--name <kind_cluster>]`.
  - minikube: `minikube image load <image> [-p <profile>]`.

## Pod Lifecycle
- Name: `sb-<session-id>` sanitized to DNS‑1123 and truncated to 63 chars.
- Namespace: `namespace` kwarg (default `default`).
- Manifest (applied via `kubectl apply -f -`):
  - `apiVersion: v1`, `kind: Pod`, `restartPolicy: Never`.
  - Container `main`:
    - `image: <tagged image>`
    - `command: ["sh", "-c", "sleep infinity"]`
    - `workingDir`: parse from Dockerfile `WORKDIR` (fallback `/`).
    - Resources: defaults (requests: `cpu=0.5`, `memory=512Mi`; limits: `cpu=2`, `memory=4Gi`) with kwarg overrides.
- Wait ready: `kubectl wait --for=condition=Ready pod/<name> --timeout=<build_timeout_sec>s`.
- Prepare log dirs in container: `mkdir -p /logs/agent /logs/verifier` (via `exec`).

## Exec & File Transfer
- `exec(command, cwd, env, timeout_sec)`:
  - Wrap with `bash -ic`.
  - Prepend `cd {cwd or workdir}` and inline `timeout {timeout_sec}` if provided.
  - Invoke: `kubectl exec <pod> -c main -- bash -ic "..."`.
  - Capture stdout/stderr/return code with `asyncio.create_subprocess_exec` and return `ExecResult`.
- `upload_file/dir` and `download_file/dir`:
  - Use `kubectl cp` both directions.
  - Ensure target directory exists (`mkdir -p`) before copying directories into the pod.
  - Add retries (tenacity) for transient errors.

## Config Surface (Environment Kwargs)
- `cluster`: `auto|kind|minikube` (default `auto`).
- `namespace`: Kubernetes namespace (default `default`).
- `kind_cluster`: kind cluster name (optional).
- `minikube_profile`: minikube profile (optional).
- `cpu_request`, `cpu_limit`: e.g., `0.5`, `2`.
- `memory_request`, `memory_limit`: e.g., `512Mi`, `4Gi`.
- `image_platform`: passed to buildx (e.g., `linux/amd64`) when building.

## Drift & Force Rebuild
- Context drift changes `build_hash` and thus image tag → automatic rebuild.
- Base image drift: use `--force-build` (maps to `TrialConfig.environment.force_build`), building even when the tag exists; `docker buildx --no-cache` when forced.

## Validation Steps
1) kind:
   - `kind create cluster` (if needed).
   - `sb trials start -t examples/tasks/hello-world --environment-type kubernetes --environment-kwarg cluster=kind`.
2) minikube:
   - `minikube start` (if needed).
   - `sb trials start -t examples/tasks/hello-world --environment-type kubernetes --environment-kwarg cluster=minikube`.
3) Verify `result.json` shows reward `1.0` and trial directories contain agent/verifier logs.

Note on autoscaling and remote parity
- The local plan targets single-node kind/minikube and does not include autoscaling. To exercise the fully remote path (ECR/EKS/CodeBuild) while developing locally, use `sb cloud run` with `cluster=remote`; it pushes to ECR and pulls on the cluster, matching production semantics.

## Risks & Notes
- `.dockerignore` not honored initially may rebuild more often than Docker would; acceptable v1.
- Multi‑arch: if host is arm64 but container nodes are amd64, pass `image_platform=linux/amd64` to buildx.
- `kubectl cp` quirks on large directories or symlinks; mitigate with retries and pre‑creating directories.

Note on I/O staging
- S3-staged inputs/outputs are a remote (EKS) optimization. For local kind/minikube, `kubectl cp` remains the default and sufficient for small/medium batches.
