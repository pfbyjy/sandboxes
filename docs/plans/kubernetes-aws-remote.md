# Kubernetes Environment Plan — Remote AWS (EKS + ECR + CodeBuild + S3)

## Goal
- Make "remote" fully remote with strong, opinionated defaults.
- Build images in AWS CodeBuild using task context uploaded to S3; push to ECR.
- Run trials on EKS pulling from ECR; no local build/push.
- Persist task definitions and (optionally) trial results/logs to S3 for UI/traceability.
- Provide simple CLI helpers: `sb cloud init --provider aws` and `sb cloud down --provider aws`.
 - Primary entrypoint: `sb cloud run -t <path>` to do everything (provision if needed, build, run, and link to logs) with no extra flags.

## Repository Changes
- Same code paths as local plan (single `KubernetesEnvironment` implementation), with additional AWS helpers invoked when `cluster=remote`.

## Assumptions & Prerequisites
- `aws` CLI configured with permissions for ECR/EKS/CodeBuild/S3 and to update kubeconfig.
- `kubectl` available on the host.
- Users do not need to pass builder/registry flags — sensible defaults are used with env overrides.

## CLI Surface (DX‑first)
- `sb cloud run -t <path>`
  - If `<path>` is a single task directory (Sandboxes layout), runs one remote trial.
  - If `<path>` is a directory containing tasks, runs a remote job over all valid tasks.
  - Optional: `-a/--agent`, `-m/--model` (defaults to `oracle` if omitted).
  - Optional: `--no-provision` (use existing infra), `--upload-results/--no-upload-results` (default: upload for remote).
- `sb cloud init --provider aws` — idempotent provision (EKS/ECR/S3/CodeBuild).
- `sb cloud down --provider aws` — teardown (safe by default; prompts to purge S3/ECR).

## One‑Time Provisioning (idempotent “ensure”) — `sb cloud init --provider aws`
- S3 bucket `abundant-sb-artifacts` with prefixes `tasks/`, `jobs/`, `logs/` (create if missing; tag).
- ECR repo `abundant-sb-sandboxes` (create if missing; tag).
- CodeBuild project `abundant-sb-build` (create if missing):
  - Environment: `aws/codebuild/standard:7.0`, privileged=true.
  - Role with least-privilege perms: ECR push to target repo; S3 GetObject on task zips; CloudWatch logs.
- EKS cluster `abundant-sb-eks` (create if missing via `eksctl`):
  - Managed node group (e.g., 2× `m6i.large`), tags on all resources.
  - Namespace `agents` ensured; ECR `imagePullSecret` ensured (IRSA later).
  - Kubeconfig updated (`aws eks update-kubeconfig`).
- Autoscaling (Karpenter):
  - Ensure IRSA (OIDC provider) for the cluster.
  - Create Karpenter controller IAM role (IRSA) and an EC2 InstanceProfile for nodes, both tagged with `sb:stack=abundant-sb`.
  - Tag subnets and security groups with `karpenter.sh/discovery=abundant-sb-eks`.
  - Install/upgrade Karpenter via Helm into `karpenter` namespace.
  - Helm values (v0.16.x): set `settings.clusterName`, `settings.clusterEndpoint`, `serviceAccount.create=false`, and `serviceAccount.name=karpenter` when the SA is pre‑created via `eksctl`.
  - Controller IAM (IRSA): grant `iam:PassRole` on the node role used by Karpenter (e.g., `abundant-sb-eks-node-role`). For better pricing behavior, also grant `pricing:GetProducts` and `ec2:DescribeSpotPriceHistory` (read‑only).
  - aws‑auth mapping: add the Karpenter node role to `kube-system/aws-auth` with groups `system:bootstrappers` and `system:nodes` so new nodes can join.
  - AMI family (v0.16.x CRD): supports `AL2`, `Ubuntu`, `Bottlerocket`, `Custom` (AL2023 not supported). On EKS 1.32, Bottlerocket is a reliable default for Karpenter nodes.
  - Apply provider CRDs + defaults (version‑aware):
    - Karpenter v0.16.x (what we used here) ships the `AWSNodeTemplate` (karpenter.k8s.aws/v1alpha1) and `Provisioner` (karpenter.sh/v1alpha5) CRDs. There is no `EC2NodeClass` in this version.
    - Newer Karpenter (v1beta) uses `EC2NodeClass` (karpenter.k8s.aws/v1beta1) and `Provisioner` (karpenter.sh/v1beta1).
    - Ensure the right CRDs exist before applying resources:
      - `kubectl api-resources | grep -i -E 'provisioner|awsnodetemplate|ec2nodeclass'`
    - For v0.16.x, create an AWSNodeTemplate and a Provisioner (minimal defaults below). For v1beta+, create an EC2NodeClass and a Provisioner. In either case:
      - Choose either `ttlSecondsAfterEmpty` or `consolidation.enabled` (mutually exclusive on v0.16.x).
      - Requirements for instance families (e.g., m6i/c6i), capacity types `[spot, on-demand]`.
      - Reasonable vCPU/memory limits to prevent runaway scale.

  Minimal manifests (v0.16.x)
  - AWSNodeTemplate (uses discovery tags and an instance profile name)
    ```yaml
    apiVersion: karpenter.k8s.aws/v1alpha1
    kind: AWSNodeTemplate
    metadata:
      name: abundant-sb
    spec:
      amiFamily: AL2
      subnetSelector:
        karpenter.sh/discovery: abundant-sb-eks
      securityGroupSelector:
        karpenter.sh/discovery: abundant-sb-eks
      instanceProfile: abundant-sb-eks-node
    ```
  - Provisioner (binds to the providerRef above)
    ```yaml
    apiVersion: karpenter.sh/v1alpha5
    kind: Provisioner
    metadata:
      name: default
    spec:
      providerRef:
        name: abundant-sb
      limits:
        resources:
          cpu: "64"
          memory: 256Gi
      # Choose one (mutually exclusive on v0.16.x):
      ttlSecondsAfterEmpty: 60
      # consolidation:
      #   enabled: true
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["m6i", "c6i"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
    ```
  - Verify they exist and are accepted:
    - `kubectl get awsnodetemplates` (v0.16.x), `kubectl get provisioners`
    - Pods Pending with `Insufficient cpu` should trigger a new node once these are present and the controller is healthy.
  - Required Helm values for a healthy controller:
    - `settings.clusterName=<abundant-sb-eks>`
    - `settings.clusterEndpoint=<EKS API endpoint>` (from `aws eks describe-cluster`)
    - `serviceAccount.create=false` (when SA/IRSA is created via `eksctl`) and `serviceAccount.name=karpenter`.
  - IAM permissions (controller read access):
    - Add read-only permissions for pricing and spot history to the controller’s role to remove log noise and enable price-aware decisions, e.g.:
      - `pricing:GetProducts`
      - `ec2:DescribeSpotPriceHistory`
    - These are not strictly required (Karpenter falls back to cached/static data) but recommended.

## Discovery & Reuse
- At start, rediscover by name; if missing, locate by tags (`sb:*`) or provision if allowed.
- Kube context is set/verified before any `kubectl` calls.

## Image Build, Push, and Caching (CodeBuild + S3)
- Hash `environment/` → `env_hash`. Upload `context.zip` to `s3://abundant-sb-artifacts/tasks/<task>/<env_hash>/context.zip`.
- Image URI: `<acct>.dkr.ecr.<region>.amazonaws.com/abundant-sb-sandboxes:<environment_name>-<env_hash12>`.
- If tag exists and `--no-force-build`, skip build.
- Otherwise start CodeBuild with source=S3 and inline buildspec:
  - `docker buildx build --platform ${PLATFORM:-linux/amd64} -t $IMAGE_URI --push .`
- Poll until `SUCCEEDED`.

Result metadata persisted in `result.json`:
- `s3_task_context_uri`: the S3 key used by CodeBuild for this image.
- `image_uri`: the exact ECR image used.
- `codebuild_build_id` and `codebuild_console_url`.

## Pod Lifecycle (EKS)
- Same manifest shape as local; ensure `imagePullSecrets` (or IRSA) in pod spec.
- Name, namespace (`agents`), readiness wait, and log dir preparation remain the same.

## Autoscaling Behavior
- `sb cloud run -t <dir>` submits all tasks; the client does not throttle concurrency.
- Karpenter adds nodes based on pod requests (we request ~0.5 CPU / 512Mi by default) and consolidates when idle.
- Without Karpenter (or Cluster Autoscaler), many pods will sit Pending on a fixed-size node group.

### Scale-down Strategy Tradeoffs (v0.16.x)
- `ttlSecondsAfterEmpty`: deletes a node N seconds after it becomes empty (no non‑DaemonSet pods).
  - Pros: deterministic cleanup; no evictions or pod movement; great for bursty batch loads.
  - Cons: a small straggler can keep a whole node alive; less packing efficiency.
- `consolidation.enabled: true`: actively evicts/moves pods to pack onto fewer nodes.
  - Pros: better steady‑state cost; reclaims underutilized nodes without waiting for idle.
  - Cons: introduces evictions; timing is heuristic (less predictable than TTL).
- On v0.16.x these are mutually exclusive; pick one to avoid conflicts.

Troubleshooting (Autoscaling)
- Controller crashloop: `panic: "" not a valid CLUSTER_ENDPOINT URL; CLUSTER_NAME is required`
  - Cause: chart values missing `settings.clusterName`/`settings.clusterEndpoint`, or wrong ServiceAccount.
  - Fix: `helm upgrade --install karpenter ... --set settings.clusterName=<cluster> --set settings.clusterEndpoint=<endpoint> --set serviceAccount.create=false` and/or `kubectl -n karpenter set env deploy/karpenter CLUSTER_NAME=<cluster> CLUSTER_ENDPOINT=<endpoint>`.
  - Verify: `kubectl -n karpenter rollout status deploy/karpenter` and controller logs show normal init.
- Controller runs but logs `UnauthorizedOperation` on pricing APIs
  - Cause: controller IAM role missing `pricing:GetProducts` / `ec2:DescribeSpotPriceHistory`.
  - Effect: non-fatal; falls back to cached/static pricing. Recommended to grant read-only permissions for optimal behavior.
- Pods Pending with `Insufficient cpu`
  - Cause: per-node reserved CPU (system DaemonSets, controllers) leaves < 500m free; fixed-size node group cannot scale.
  - Fix: ensure autoscaler is healthy and that the right provider CRDs + defaults are installed for your Karpenter version (AWSNodeTemplate/Provisioner for v0.16.x, or EC2NodeClass/Provisioner for v1beta+). Alternatively, temporarily increase node group desired size; remove crashlooping controllers; delete leftover sb-* pods.
- Provisioning failed: `no provisioners found`
  - Cause: no `Provisioner` resource exists or CRDs are missing/mismatched.
  - Fix: create `AWSNodeTemplate` + `Provisioner` (v0.16.x) or `EC2NodeClass` + `Provisioner` (v1+).
- Provisioning failed: `iam:PassRole` unauthorized when launching
  - Cause: controller IRSA role lacks permission to pass the node role.
  - Fix: grant `iam:PassRole` on the node role used by Karpenter (instance profile’s role).
- Node stuck NotReady with `NodeStatusNeverUpdated` in `kubectl describe node`
  - Cause: missing `aws-auth` mapRoles entry for the Karpenter node role.
  - Fix: add the node role to `kube-system/aws-auth` with groups `system:bootstrappers` and `system:nodes`.
- AL2023 AMI family rejected by webhook on v0.16.x
  - Cause: v0.16.x CRD does not support `AL2023`.
  - Fix: use `Bottlerocket` (recommended on EKS 1.32) or `AL2`/`Ubuntu`.
- "unknown capacity type capacity-block" log lines
  - Note: benign on v0.16.x for certain GPU/Trainium families; can be ignored.

## Batch-Scale Hardening (100+)

When bursting to dozens of concurrent trials, focus on avoiding ephemeral-storage pressure and minimizing external rate limits.

- Increase node root volume
  - v0.16.x: set `blockDeviceMappings` on `AWSNodeTemplate` to grow the root EBS volume (e.g., gp3 100Gi).
  - Example (partial):
    ```yaml
    apiVersion: karpenter.k8s.aws/v1alpha1
    kind: AWSNodeTemplate
    metadata:
      name: abundant-sb
    spec:
      blockDeviceMappings:
        - deviceName: /dev/xvda
          ebs:
            volumeSize: 100Gi
            volumeType: gp3
            iops: 3000
            throughput: 125
            deleteOnTermination: true
    ```
  - Drain + delete DiskPressure nodes to rotate onto larger disks.

- Make storage schedulable
  - Add `resources.requests.ephemeral-storage` (e.g., 8–16Gi) and a corresponding `limits.ephemeral-storage` on trial pods so the scheduler/Karpenter adds nodes before disks fill.

- Recycle burst nodes
  - Use `ttlSecondsAfterEmpty` (predictable) or `consolidation.enabled` (cost-aware) to reset disk state between bursts.

- Build stability (avoid Docker Hub 429)
  - Prefer ECR Public bases: pre-pull/retag in CodeBuild `pre_build` (e.g., `public.ecr.aws/docker/library/python:3.13-slim -> python:3.13-slim`).
  - Optional: pass `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` to CodeBuild and `docker login` when present.
  - Add a small retry/backoff around CodeBuild builds on `FAILED/TIMED_OUT`.

- Remote I/O robustness
  - Ensure S3/ECR/CodeBuild once via `sb cloud init`; do not "ensure" per trial.
  - Add retries/backoff on S3 `cp`/`presign` and fall back to `kubectl cp` for inputs if S3 fails.
  - On teardown, upload `/logs` to S3 best‑effort; if missing and the pod will be deleted, optionally capture `kubectl logs` before deletion.

- Operational fast-path
  - If nodes show `DiskPressure`/`Evicted` events: `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data && kubectl delete node <node>` to force replacement.
  - Delete `ContainerStatusUnknown` sb-* pods to reschedule impacted trials.

## Manual Updates (Stopgap)

Until IaC is in place, apply the following manual tweaks to stabilize large bursts:

- Increase Karpenter node root volume (v0.16.x)
  - Update the `AWSNodeTemplate` (name `abundant-sb`) to 100Gi gp3 root volume:
    ```bash
    kubectl apply -f - <<'YAML'
    apiVersion: karpenter.k8s.aws/v1alpha1
    kind: AWSNodeTemplate
    metadata:
      name: abundant-sb
    spec:
      blockDeviceMappings:
        - deviceName: /dev/xvda
          ebs:
            volumeSize: 100Gi
            volumeType: gp3
            iops: 3000
            throughput: 125
            deleteOnTermination: true
    YAML
    ```

- Recycle DiskPressure nodes and reschedule unknown pods
  ```bash
  # Identify nodes with recent DiskPressure events (optional)
  kubectl get nodes -o name | xargs -n1 -I{} sh -c 'echo {}; kubectl describe {} | rg -n "NodeHasDiskPressure|EvictionThresholdMet" || true'

  # Drain and delete a hot node (replace with your node name)
  kubectl drain ip-192-168-123-245.us-west-2.compute.internal \
    --ignore-daemonsets --delete-emptydir-data
  kubectl delete node ip-192-168-123-245.us-west-2.compute.internal

  # Delete sb-* pods stuck in ContainerStatusUnknown
  kubectl get pods | awk '/ContainerStatusUnknown/ {print $1}' | xargs -r kubectl delete pod
  ```

- Add ephemeral-storage defaults via LimitRange (namespace `default`)
  ```bash
  kubectl apply -f - <<'YAML'
  apiVersion: v1
  kind: LimitRange
  metadata:
    name: default-ephemeral-storage
    namespace: default
  spec:
    limits:
      - type: Container
        defaultRequest:
          ephemeral-storage: "8Gi"
        default:
          ephemeral-storage: "16Gi"
  YAML
  ```

- Provisioner TTL (v0.16.x) to recycle burst nodes when idle
  ```bash
  # Remove consolidation if present (mutually exclusive)
  kubectl patch provisioner default --type=json \
    -p='[{"op":"remove","path":"/spec/consolidation"}]' || true
  # Set a short TTL after empty
  kubectl patch provisioner default --type=merge -p '{"spec":{"ttlSecondsAfterEmpty":120}}'
  ```

- CodeBuild base image hardening (optional; avoids Docker Hub 429)
  - Export `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` in your environment and we will add `docker login` in pre_build on the next code update; for now, simply re-run failed builds or prefer tasks with ECR Public bases.

Logs and links (remote UX):
- Short term: we still download verifier reward and can optionally upload the whole `trial_dir` to S3 at the end.
- Mid term (preferred): stream pod logs to CloudWatch and provide deep links instead of local logs.
  - On `sb cloud init`, install a minimal Fluent Bit DaemonSet sending logs from pods named `sb-*` to a log group `/sb/eks/logs`.
  - Label/annotate trial pods so each trial’s stream is predictable: `/sb/eks/logs/<trial_name>`.
  - Include `pod_log_group`, `pod_log_stream`, and `pod_logs_url` in `result.json`, and print the URL during `sb cloud run`.

## S3 Artifacts (Tasks + Trials)

Goal: Persist task definitions (versioned) and trial outputs in S3 so a UI can visualize an exact task version and all runs of that version.

Task versioning (deterministic)
- Compute `task_hash` as SHA‑256 over a stable walk of the task directory including: `task.toml`, `instruction.md`, `environment/**`, `solution/**`, `tests/**`.
  - Walk paths in sorted order; hash each file’s relative path + contents; combine into one digest.
  - `task_version` = first 12 hex chars of the digest; `task_id` = `<task_name>@<task_version>`.
- Use `task_version` to tag images and to build S3 keys (see layout below).

S3 layout
- Bucket: `${SB_S3_BUCKET}` (default `abundant-sb-artifacts`).
- Prefixes:
  - `tasks/<dataset>/<task_name>/<task_version>/`
    - `task.zip` — zipped task files (`task.toml`, `instruction.md`, `environment/`, `solution/`, `tests/`).
    - `metadata.json` — `{ task_name, task_version, dataset, created_at, files_sha256 }`.
  - `trials/<job_id>/<trial_name>/` where `trial_name` includes task name and a short id.
    - `config.json`, `result.json` (augmented with task_version and S3 URIs)
    - `agent/` (logs), `verifier/` (logs incl. `ctrf.json`, `test-console-output.txt`), `output/` (artifacts)
    - Optional: `manifest.json` summarizing present files + sizes + checksums.
  - `jobs/<job_id>/result.json` — aggregated job result (already written locally, mirrored to S3).
  - Indexes for UI (optional but recommended):
    - `indexes/tasks/<task_name>/<task_version>/runs.json` — recent N runs with `{trial_name, job_id, started_at, reward, agent, model_name, s3_trial_uri}`.

CLI behavior (sb cloud run)
- `--upload-results/--no-upload-results` controls syncing to S3 (default: upload for remote).
- Single task: on completion, sync the task definition (if not present) and the trial directory to S3.
- Directory of tasks: sync each task def (by `task_version`) and each trial directory; write/update job‑level `result.json` under `jobs/<job_id>/`.

Result metadata (augmentations)
- Add to `result.json`:
  - `task_version`, `task_id`, `s3_task_uri`, `s3_trial_uri`, and when applicable `pod_log_group`, `pod_log_stream`, `pod_logs_url` (when CW logging is enabled).
  - Keep existing timing, rewards, exceptions, and agent/environment details.

Security/IAM
- S3: enable SSE‑S3 or SSE‑KMS (optional), and bucket policy scoped to CI/user roles.
- Writes occur from the host via AWS CLI/SDK (sb CLI) using your credentials; no in‑cluster writes required.

UI consumption (outline)
- List tasks by `s3://…/tasks/<dataset>/<task_name>/` and their `task_version` subfolders.
- For a selected version, fetch `metadata.json` and render `task.zip` contents alongside trial entries from `indexes/tasks/<task_name>/<task_version>/runs.json`.
- Trial details page pulls `result.json`, and links to `agent/`, `verifier/`, and `output/` objects.

## Exec & File Transfer
- Remote (cluster=remote): S3‑staged I/O replaces `kubectl cp`.
  - Inputs: package `solution/` and `tests/` as tar.gz, upload to S3 under `tasks/<task>/<task_version>/`, and fetch/unpack inside the pod via pre‑signed URLs (IRSA preferred when available).
  - Outputs: stream `tar -cz /logs` from the pod to S3 via the host’s AWS CLI, then the CLI mirrors S3 → local `jobs/<...>/<trial>/` so `agent/` and `verifier/` are present as today.
- Local (kind/minikube): unchanged; still uses `kubectl cp`.

## Config Surface (kept minimal)
- Users specify `cluster=remote` only.
- All other settings use defaults with env overrides: `SB_AWS_REGION`, `SB_STACK`, `SB_EKS_CLUSTER`, `SB_ECR_REPO`, `SB_S3_BUCKET`, `SB_CODEBUILD_PROJECT`, `SB_K8S_NAMESPACE`, `SB_IMAGE_PLATFORM`.
- Optional: `provision=false` to use an existing cluster; `namespace=...`; `image_pull_secret=...`.

## Drift & Force Rebuild
- Context changes → new `env_hash` and tag → new push, transparently handled.
- Base image drift: `--force-build` rebuilds/pushes regardless of existing tag.

## Validation Steps
- First run (one command with defaults; provisions if missing):
  - `sb cloud run -t tasks/imported/adv-log-analysis`
- Subsequent runs reuse infra and skip builds when context unchanged unless `--force-build`.


Karpenter scale-up sanity (v0.16.x)
- Confirm CRDs exist: `kubectl api-resources | grep -E 'provisioner|awsnodetemplate'`
- Ensure `AWSNodeTemplate` + `Provisioner` exist and reference discovery tags + instance profile.
- Controller uses `serviceAccount.name=karpenter`; IRSA role has `iam:PassRole` to the node role.
- `aws-auth` contains the node role mapping (system:bootstrappers, system:nodes).
- Create a pod that requests 4 vCPU/4Gi; a spot node should launch and the pod should become Running.

Scale-down behavior (v0.16.x)
- `ttlSecondsAfterEmpty` and `consolidation.enabled` are mutually exclusive; pick one:
  - Predictable: set `ttlSecondsAfterEmpty: 60` (remove consolidation).
  - Cost-aware: use `consolidation.enabled: true` (no TTL).

## Quick Autoscaling Evaluation

Single pod trigger (scale-up)
```bash
kubectl create ns karpenter-test || true
cat <<'YAML' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: inflate
  namespace: karpenter-test
spec:
  terminationGracePeriodSeconds: 0
  containers:
  - name: pause
    image: public.ecr.aws/eks-distro/kubernetes/pause:3.7
    resources:
      requests:
        cpu: "4"
        memory: "4Gi"
      limits:
        cpu: "4"
        memory: "4Gi"
YAML
# Watch node creation and pod schedule
kubectl -n karpenter-test get pod -w
kubectl get nodes -L karpenter.sh/capacity-type,karpenter.k8s.aws/instance-family
```

Batch of 10 (parallel scale-up)
```bash
cat <<'YAML' | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: inflate-10
  namespace: karpenter-test
spec:
  completions: 10
  parallelism: 10
  template:
    spec:
      restartPolicy: Never
      terminationGracePeriodSeconds: 0
      containers:
      - name: cpu
        image: public.ecr.aws/docker/library/busybox:latest
        command: ["sh","-c","echo hi; sleep 15"]
        resources:
          requests:
            cpu: "1"
            memory: "256Mi"
          limits:
            cpu: "1"
            memory: "256Mi"
YAML
kubectl -n karpenter-test get jobs,pods
kubectl get nodes -L karpenter.sh/capacity-type,karpenter.k8s.aws/instance-family
```

Cleanup
```bash
kubectl delete ns karpenter-test --wait=false
```

## Concurrency Guidance
- Control plane vs. worker autoscaling: Karpenter scales worker nodes; the EKS API server is managed by AWS. Design for burst without relying on the control plane for bulk data.
- `sb cloud run` uses task‑count concurrency by design so the cluster can autoscale. To support this safely, avoid `kubectl cp` for bulk I/O and use the S3‑staged pattern below.
- If you must mitigate flakiness temporarily during triage, you can cap client concurrency with `sb jobs start -n <N>`, but this is not the recommended steady‑state.

## High-Throughput Runtime I/O (Remote)
- Inputs (solution/tests):
  - Zip `solution/` and `tests/` and upload to S3. In the pod (init step or first command), download and unpack to `/solution` and `/tests` (via IRSA or pre-signed URLs).
- Outputs (logs/artifacts):
  - Tar `/logs` (agent + verifier) inside the pod and upload to S3 at the end. The CLI syncs from S3 to the local job directory.
- Result metadata:
  - Add `s3_task_uri` and `s3_trial_uri` into `result.json` for the visualizer.
- Benefit:
  - Removes heavy control-plane traffic and enables high burst concurrency reliably.

## Security & RBAC
- Image pulls:
  - Use either imagePullSecret in namespace or IRSA SA with `AmazonEC2ContainerRegistryReadOnly`.
- `kubectl exec` permissions:
  - Ensure the SA used by the pod allows exec/attach if your cluster policy requires it.
- Egress policies:
  - If the verifier installs packages or reaches out to network, ensure outbound is allowed; otherwise pre‑bake dependencies into the image.

### Image Pull (ErrImagePull) Troubleshooting
- Node IAM: attach `AmazonEC2ContainerRegistryReadOnly` to the Karpenter node InstanceProfile role.
- Network path: ensure NAT egress or VPC interface endpoints exist for `ecr.api`, `ecr.dkr`, and `s3` in the subnets used by Karpenter.
- Region alignment: cluster, ECR repo, and CodeBuild must be in the same region; verify the tag exists with `aws ecr describe-images`.
- Pod describe tips:
  - “no basic auth credentials” → node IAM permissions.
  - “manifest unknown” → tag not pushed/region mismatch.
  - TLS/timeout → NAT/VPC endpoints or transient ECR throttling.

### CodeBuild Build Troubleshooting
- Project environment must have `privilegedMode=true` for Docker builds (DinD).
- The CodeBuild role should allow ECR push (PowerUser) and S3 read, and write logs to CloudWatch.
- Find build logs: `aws codebuild list-builds-for-project` → `batch-get-builds` → open in CloudWatch; fix failures before retrying `sb cloud run`.

### Stragglers & Cleanup
- Scale-down policy: choose one on v0.16.x
  - `ttlSecondsAfterEmpty: <N>` (predictable cleanup after idle) or `consolidation.enabled: true` (cost-aware re-packing).
- Reap leftover pods after jobs: e.g., delete `sb-*` pods older than X minutes in the job’s namespace.

## Risks & Notes
- CodeBuild cold start latency adds seconds; acceptable for remote scale.
- Multi‑arch: default platform `linux/amd64`; allow override via env var.
- Large contexts: zipping `environment/` keeps uploads small. For Git-based tasks, a future path is CodeBuild cloning the repo directly.
- Teardown safety: keep S3 by default to preserve artifacts; require explicit purge.

## Teardown — `sb cloud down --provider aws`
- Uninstall Karpenter (Helm release), remove default NodeClass/Provisioner.
- Optionally delete the Karpenter IAM role and node InstanceProfile (found by tags).
- Delete EKS cluster (eksctl), CodeBuild project; optionally force-delete ECR repo and purge S3 (when explicitly requested).

## Migration to IaC
- Replace eksctl/imperative ensures with CloudFormation/CDK/Terraform stacks for:
  - EKS (cluster, node groups, OIDC), Karpenter IAM roles/InstanceProfiles, subnet/SG tags.
  - ECR repo (with lifecycle policy), CodeBuild project/role, S3 buckets.
- `sb cloud init/down` switch to stack create/update/delete while keeping the same DX and defaults.

### IaC Readiness (current state)
- Validated assumptions: EKS 1.32 in `us-west-2`, cluster `abundant-sb-eks`, OIDC enabled, IRSA in use for Karpenter.
- Working Karpenter config (v0.16.x):
  - IRSA role bound to SA `karpenter` (namespace `karpenter`).
  - Inline/managed perms: `iam:PassRole` on node role `abundant-sb-eks-node-role`; optional `pricing:GetProducts`, `ec2:DescribeSpotPriceHistory`.
  - Discovery tags on subnets and shared node security group: `karpenter.sh/discovery=abundant-sb-eks`.
  - aws-auth `mapRoles` includes the node role (system:bootstrappers, system:nodes).
  - Node AMI family recommended: `Bottlerocket` for EKS 1.32.
- Suggested Terraform stacks:
  - `network`: VPC (or reuse), public/private subnets, discovery tags.
  - `eks`: cluster, nodegroup, OIDC provider, aws-auth configmap (managed via addon).
  - `karpenter`: IRSA role + policy, InstanceProfile and node role, Helm release, CRDs, `AWSNodeTemplate` + `Provisioner` (or `EC2NodeClass`/`NodePool` if upgrading to v1).
  - `artifacts`: ECR repo, S3 buckets, CodeBuild project + role.

## Phased Rollout
1) Phase 1 (MVP): CodeBuild + S3 builds, ECR pulls, reward download; optional S3 upload of `trial_dir` after completion. Emit CodeBuild console URL.
2) Phase 2: CloudWatch log streaming for trial pods via Fluent Bit; print pod logs URL; remote runs stop relying on local log files.
3) Phase 3: Presentation UI consumes S3 task defs and S3/CloudWatch logs; `sb cloud run` prints a single dashboard URL.
