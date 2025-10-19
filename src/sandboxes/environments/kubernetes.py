import asyncio
import random
import json
import os
import re
import shlex
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import tempfile
import zipfile
import tarfile

from sandboxes.environments.base import BaseEnvironment, ExecResult
from sandboxes.models.environment_type import EnvironmentType
from sandboxes.models.task.config import EnvironmentConfig
from sandboxes.models.trial.paths import EnvironmentPaths, TrialPaths
from sandboxes.cloud.defaults import CloudDefaults


class KubernetesEnvironment(BaseEnvironment):
    """Kubernetes backend targeting local clusters (kind or minikube).

    Notes:
    - is_mounted=False: on local clusters we copy via kubectl cp; on remote EKS we stage inputs/outputs via S3.
    - For image builds, we compute a content hash over `environment/` and tag images
      `<task-name>:<hash12>`. Force rebuild bypasses the cache.
    - kind is the default local cluster. minikube is optionally supported.
    """

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        *args: Any,
        **kwargs: Any,
    ):
        # Set fields used by _validate_definition before super().__init__ calls it
        self._image_override = kwargs.get("image")

        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            *args,
            **kwargs,
        )

        # Kubernetes options
        self._namespace = kwargs.get("namespace", "default")
        self._context = kwargs.get("context")
        self._kubeconfig = kwargs.get("kubeconfig")
        self._cluster_mode = (kwargs.get("cluster") or "auto").lower()
        self._active_cluster_mode: str | None = None
        self._kind_cluster = kwargs.get("kind_cluster")
        self._minikube_profile = kwargs.get("minikube_profile")

        # AWS remote options (EKS/ECR/S3/CodeBuild) with defaults
        cd = CloudDefaults()
        self._aws_region = kwargs.get("aws_region") or cd.region
        self._cluster_name = kwargs.get("cluster_name", cd.eks_cluster)
        self._ecr_repo = kwargs.get("ecr_repo", cd.ecr_repo)
        self._s3_bucket = kwargs.get("s3_bucket", cd.s3_bucket)
        self._codebuild_project = kwargs.get("codebuild_project", cd.codebuild_project)
        self._provision = bool(kwargs.get("provision", True))
        self._image_pull_secret = kwargs.get("image_pull_secret")
        # For remote builds we default to CodeBuild
        self._builder = (kwargs.get("builder") or ("codebuild" if (self._cluster_mode == "remote") else "local")).lower()
        # Upload results toggle (S3 mirror for remote runs)
        self._upload_results = bool(kwargs.get("upload_results", True))
        # S3 staging preference: None (auto-enable on remote), True, or False
        self._s3_staging_pref: bool | None = kwargs.get("s3_staging")

        # Optional task/job metadata for S3 keys
        self._task_name: str | None = kwargs.get("task_name")
        self._task_version: str | None = kwargs.get("task_version")
        self._job_id: str | None = kwargs.get("job_id")

        # Resources
        self._cpu_request = kwargs.get("cpu_request", "0.5")
        self._cpu_limit = kwargs.get("cpu_limit", "2")
        self._memory_request = kwargs.get("memory_request", "512Mi")
        self._memory_limit = kwargs.get("memory_limit", "4Gi")

        # Image selection/build
        self._image_platform = kwargs.get("image_platform") or cd.image_platform

        # Pod name derived from session_id
        self._pod_name = self._sanitize_name(f"sb-{self.session_id}")

        # Parse workdir from Dockerfile (used for cd in exec if cwd not provided)
        self._workdir = self._parse_workdir()

    @staticmethod
    def type() -> EnvironmentType:
        return EnvironmentType.KUBERNETES

    @property
    def is_mounted(self) -> bool:
        return False

    @property
    def _environment_definition_path(self) -> Path:
        return self.environment_dir / "Dockerfile"

    def _validate_definition(self):
        # If no image override and no prebuilt image configured, require Dockerfile
        if self._image_override is None and self.task_env_config.docker_image is None:
            if not self._environment_definition_path.exists():
                raise FileNotFoundError(
                    f"{self._environment_definition_path} not found. Provide an image via "
                    "task.environment.docker_image or --environment-kwarg image, or add a Dockerfile."
                )

    # ---------- Public API ----------
    async def start(self, force_build: bool):
        # Determine cluster mode
        cluster = await self._detect_cluster_mode(self._cluster_mode)
        self._active_cluster_mode = cluster

        # Remote cluster setup (EKS)
        if cluster == "remote":
            await self._ensure_eks_cluster()
            await self._ensure_eks_context()
            # Ensure namespace exists (skip for 'default')
            if self._namespace != "default":
                ns_check = await self._kubectl(["get", "namespace", self._namespace], check=False)
                if ns_check.return_code != 0:
                    await self._kubectl(["create", "namespace", self._namespace])
            # Ensure image pull secret if requested
            if self._image_pull_secret:
                await self._ensure_image_pull_secret()

        # Get or build image
        image = await self._resolve_image(cluster=cluster, force_build=force_build)

        # Apply Pod
        manifest = self._pod_manifest(image=image)
        await self._kubectl_apply(manifest)

        # Wait until ready
        await self._kubectl(
            [
                "wait",
                f"pod/{self._pod_name}",
                "--for=condition=Ready",
                f"--timeout={int(self.task_env_config.build_timeout_sec)}s",
            ]
        )

        # Prepare logging directories inside the pod
        await self.exec(
            f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}"
        )

    async def stop(self, delete: bool):
        # Attempt to upload logs to S3 and mirror locally before deletion
        try:
            await self._maybe_upload_and_mirror_logs()
        except Exception:
            # don't fail stop() if logs upload fails
            pass

        # Respect delete flag: only delete when requested.
        if not delete:
            return
        try:
            await self._kubectl(["delete", f"pod/{self._pod_name}", "--ignore-not-found=true"])
        except Exception:
            pass

    async def _maybe_upload_and_mirror_logs(self) -> None:
        if not self._use_s3_staging() or not self._upload_results:
            return
        await self._ensure_s3_bucket(self._s3_bucket)

        key = f"{self._trial_s3_prefix()}/logs.tar.gz"

        # Stream tar from pod to S3 via host AWS CLI (single stream, no kubectl cp)
        # Build the full kubectl command with context/namespace
        kubectl_parts = ["kubectl"]
        if self._context:
            kubectl_parts += ["--context", self._context]
        if self._namespace:
            kubectl_parts += ["-n", self._namespace]
        kubectl_parts += ["exec", f"pod/{self._pod_name}", "-c", "main", "--", "tar", "-cz", "-C", "/", "logs"]

        # Run the pipeline: (kubectl exec … tar …) | aws s3 cp - s3://bucket/key
        pipe_cmd = (
            " ".join(shlex.quote(p) for p in kubectl_parts)
            + f" | aws s3 cp - s3://{self._s3_bucket}/{key}"
        )
        await self._run_local(["bash", "-lc", pipe_cmd])

        # Mirror logs back to local jobs/<...>/<trial>/
        with tempfile.TemporaryDirectory() as td:
            local_tar = Path(td) / "logs.tar.gz"
            await self._aws(["s3", "cp", f"s3://{self._s3_bucket}/{key}", str(local_tar)])
            # Extract while stripping the leading 'logs/'
            with tarfile.open(local_tar, "r:gz") as tf:
                for member in tf.getmembers():
                    name = member.name.lstrip("./")
                    if name == "logs" or name.startswith("logs/"):
                        if name.startswith("logs/"):
                            member.name = name[len("logs/") :]
                            tf.extract(member, path=str(self.trial_paths.trial_dir))

    async def upload_file(self, source_path: Path | str, target_path: str):
        await self._kubectl_with_retries(
            [
                "cp",
                str(source_path),
                f"{self._pod_name}:{target_path}",
                "-c",
                "main",
            ]
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        # Prefer S3-staged fetch for remote clusters for /solution and /tests
        if self._use_s3_staging() and target_dir.rstrip("/") in {"/solution", "/tests"}:
            try:
                await self._upload_dir_via_s3(Path(source_dir), target_dir)
                return
            except Exception:
                # Soft-fallback to kubectl cp when S3 is unavailable or the image lacks fetch tools
                try:
                    await self._upload_dir_via_kubectl_cp(Path(source_dir), target_dir)
                    return
                except Exception:
                    # re-raise the original S3 exception to keep context for callers
                    raise

        # Default path: kubectl cp
        await self._upload_dir_via_kubectl_cp(Path(source_dir), target_dir)

    async def download_file(self, source_path: str, target_path: Path | str):
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        await self._kubectl_with_retries(
            [
                "cp",
                f"{self._pod_name}:{source_path}",
                str(target_path),
                "-c",
                "main",
            ]
        )

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        await self._kubectl_with_retries(
            [
                "cp",
                f"{self._pod_name}:{source_dir}",
                str(target_dir),
                "-c",
                "main",
            ]
        )

    # ---------- S3-staged I/O helpers ----------
    def _use_s3_staging(self) -> bool:
        """Return True if S3 staging should be used for this environment."""
        if self._active_cluster_mode == "remote":
            if self._s3_staging_pref is None:
                return True
            return bool(self._s3_staging_pref)
        return False

    def _task_s3_prefix(self) -> str:
        task = self._task_name or self.environment_name.replace("sb__", "")
        version = self._task_version or "unknown"
        return f"tasks/{task}/{version}"

    def _trial_s3_prefix(self) -> str:
        job = self._job_id or "single"
        return f"trials/{job}/{self.session_id}"

    async def _upload_dir_via_s3(self, source_dir: Path, target_dir: str) -> None:
        target = target_dir.strip("/")
        if target not in {"solution", "tests"}:
            raise ValueError(f"S3 staging only supports /solution or /tests, got {target_dir}")

        # Create tar.gz with top-level folder name (solution/ or tests/)
        with tempfile.TemporaryDirectory() as td:
            tar_path = Path(td) / f"{target}.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tf:
                tf.add(str(source_dir), arcname=target)

            key = f"{self._task_s3_prefix()}/{target}.tar.gz"
            # Best-effort: if upload fails (e.g., S3 endpoint not reachable), allow caller to fallback
            up = await self._aws(["s3", "cp", str(tar_path), f"s3://{self._s3_bucket}/{key}"], check=False)
            if up.return_code != 0:
                raise RuntimeError(up.stderr or up.stdout or "failed to upload inputs to S3")

        # Generate pre-signed GET URL and fetch/extract inside the pod
        presign = await self._aws(["s3", "presign", f"s3://{self._s3_bucket}/{key}"], check=False)
        url = (presign.stdout or "").strip()
        if presign.return_code != 0 or not url:
            raise RuntimeError("failed to generate pre-signed URL for S3 input")

        fetch_cmd = (
            "set -eo pipefail; "
            "CMD=''; if command -v curl >/dev/null 2>&1; then CMD='curl -fsSL'; "
            "elif command -v wget >/dev/null 2>&1; then CMD='wget -qO-'; fi; "
            "if [ -z \"$CMD\" ]; then echo 'Missing curl/wget in container' >&2; exit 1; fi; "
            f"$CMD {shlex.quote(url)} | tar -xz -C /"
        )
        res = await self.exec(fetch_cmd)
        if res.return_code != 0:
            raise RuntimeError(f"Failed to fetch and extract {target} via S3: {res.stderr or res.stdout}")

    async def _upload_dir_via_kubectl_cp(self, source_dir: Path, target_dir: str) -> None:
        # Ensure target dir exists inside the container
        await self.exec(f"mkdir -p {shlex.quote(target_dir)}")
        src = source_dir
        if src.is_dir():
            entries = list(src.iterdir())
            if not entries:
                return
            for entry in entries:
                await self._kubectl_with_retries(
                    [
                        "cp",
                        str(entry),
                        f"{self._pod_name}:{target_dir}/",
                        "-c",
                        "main",
                    ]
                )
        else:
            await self._kubectl_with_retries(
                [
                    "cp",
                    str(src),
                    f"{self._pod_name}:{target_dir}",
                    "-c",
                    "main",
                ]
            )

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        # Compose the inner shell command
        shell_cmd = ""
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            shell_cmd += exports + " "
        if cwd or self._workdir:
            shell_cmd += f"cd {shlex.quote(cwd or self._workdir or '/')} && "
        shell_cmd += command

        args = [
            "exec",
            f"pod/{self._pod_name}",
            "-c",
            "main",
            "--",
            "bash",
            "-lc",
            shell_cmd,
        ]

        return await self._kubectl(args, timeout_sec=timeout_sec, check=False)

    # ---------- Helpers ----------
    async def _detect_cluster_mode(self, mode: str) -> str:
        if mode == "remote":
            if not self._aws_region:
                raise RuntimeError(
                    "aws_region is required for cluster=remote. Set --environment-kwarg aws_region=..."
                )
            return "remote"
        if mode in {"kind", "minikube"}:
            # Fill cluster identifiers if possible
            if mode == "kind" and not self._kind_cluster:
                res = await self._run_local(["kind", "get", "clusters"], check=False)
                if res.stdout:
                    first = res.stdout.strip().splitlines()[0].strip()
                    if first:
                        self._kind_cluster = first
            if mode == "minikube" and not self._minikube_profile:
                # default profile is usually 'minikube'
                self._minikube_profile = "minikube"
            return mode
        # auto-detect: prefer kind, else minikube
        try:
            result = await self._run_local(["kind", "get", "clusters"], check=False)
            if result.return_code == 0 and result.stdout and result.stdout.strip():
                first = result.stdout.strip().splitlines()[0].strip()
                if first:
                    self._kind_cluster = first
                return "kind"
        except Exception:
            pass
        try:
            result = await self._run_local(["minikube", "status", "-o", "json"], check=False)
            if result.return_code == 0 and result.stdout:
                if not self._minikube_profile:
                    self._minikube_profile = "minikube"
                return "minikube"
        except Exception:
            pass
        # Default to kind if uncertain; user can override via kwargs
        # Also attempt to set a cluster name if present
        try:
            res = await self._run_local(["kind", "get", "clusters"], check=False)
            if res.stdout:
                first = res.stdout.strip().splitlines()[0].strip()
                if first:
                    self._kind_cluster = first
        except Exception:
            pass
        return "kind"

    async def _resolve_image(self, cluster: str, force_build: bool) -> str:
        # Prebuilt via kwargs or task config
        if self._image_override:
            image = self._image_override
        elif self.task_env_config.docker_image:
            image = self.task_env_config.docker_image
        else:
            # Compute deterministic tag
            build_hash = self._compute_build_hash(self.environment_dir)
            repo_base = self.environment_name.lower()
            tag = build_hash[:12]

            if cluster == "remote":
                # Build and push to ECR via CodeBuild + S3
                account_id = await self._aws_get_account_id()
                registry = f"{account_id}.dkr.ecr.{self._aws_region}.amazonaws.com"
                await self._ensure_ecr_repo(self._ecr_repo)
                image = f"{registry}/{self._ecr_repo}:{repo_base}-{tag}"
                exists = await self._ecr_image_exists(self._ecr_repo, f"{repo_base}-{tag}")
                if not exists or force_build:
                    await self._ensure_s3_bucket(self._s3_bucket)
                    s3_key = await self._zip_and_upload_context(self._s3_bucket, repo_base, tag)
                    await self._ensure_codebuild_project()
                    build_id, _ = await self._start_codebuild_build(
                        s3_bucket=self._s3_bucket,
                        s3_key=s3_key,
                        image_uri=image,
                        platform=self._image_platform,
                    )
                    await self._poll_codebuild_build(build_id)
            else:
                # Local build
                image = f"{repo_base}:{tag}"
                await self._build_local_image(image=image, force_build=force_build)

        # Load into cluster if local (kind/minikube)
        if cluster == "kind":
            await self._run_local(
                [
                    "kind",
                    "load",
                    "docker-image",
                    image,
                    *(["--name", self._kind_cluster] if self._kind_cluster else []),
                ]
            )
        elif cluster == "minikube":
            await self._run_local(
                [
                    "minikube",
                    "image",
                    "load",
                    image,
                    *(["-p", self._minikube_profile] if self._minikube_profile else []),
                ]
            )

        return image

    def _compute_build_hash(self, context_dir: Path) -> str:
        # Hash environment/ dir deterministically
        h = sha256()
        root = context_dir.resolve()
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                h.update(rel.encode())
                try:
                    h.update(path.read_bytes())
                except Exception:
                    # In case of unreadable files, include only path
                    pass
        return h.hexdigest()

    async def _build_local_image(self, image: str, force_build: bool) -> None:
        # Check if image exists
        if not force_build:
            res = await self._run_local(["docker", "image", "inspect", image], check=False)
            if res.return_code == 0:
                return

        cmd = [
            "docker",
            "build",
            "-t",
            image,
            str(self.environment_dir),
        ]
        if self._image_platform:
            cmd[1:1] = ["buildx"]  # switch to buildx
            cmd.insert(4, "--platform")
            cmd.insert(5, self._image_platform)
        if force_build:
            cmd.append("--no-cache")

        await self._run_local(cmd)

    def _pod_manifest(self, image: str) -> dict[str, Any]:
        # Minimal pod definition running sleep infinity
        container = {
            "name": "main",
            "image": image,
            "command": ["sh", "-c", "sleep infinity"],
            "imagePullPolicy": "IfNotPresent",
            "resources": {
                "requests": {"cpu": str(self._cpu_request), "memory": str(self._memory_request)},
                "limits": {"cpu": str(self._cpu_limit), "memory": str(self._memory_limit)},
            },
        }
        if self._workdir:
            container["workingDir"] = self._workdir

        pod_spec: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": self._pod_name, "namespace": self._namespace},
            "spec": {"restartPolicy": "Never", "containers": [container]},
        }
        if self._image_pull_secret:
            pod_spec["spec"]["imagePullSecrets"] = [{"name": self._image_pull_secret}]
        return pod_spec

    async def _kubectl_apply(self, manifest: dict[str, Any]) -> None:
        data = json.dumps(manifest)
        # Use -f - to read from stdin
        await self._kubectl(["apply", "-f", "-"], stdin=data.encode())

    # -------- AWS helpers (remote mode) --------
    async def _aws(self, args: list[str], check: bool = True) -> ExecResult:
        cmd = ["aws", *args, "--region", self._aws_region] if self._aws_region else ["aws", *args]
        return await self._run_local(cmd, check=check)

    async def _aws_get_account_id(self) -> str:
        res = await self._aws(["sts", "get-caller-identity", "--output", "json"]) 
        data = json.loads(res.stdout or "{}")
        account_id = data.get("Account")
        if not account_id:
            raise RuntimeError("Failed to get AWS account id via aws sts get-caller-identity")
        return account_id

    async def _ensure_ecr_repo(self, repo: str) -> None:
        # Check existence
        res = await self._aws(["ecr", "describe-repositories", "--repository-names", repo], check=False)
        if res.return_code == 0:
            return
        # Create
        await self._aws(["ecr", "create-repository", "--repository-name", repo])
        # Optional tagging
        try:
            acct = await self._aws_get_account_id()
            arn = f"arn:aws:ecr:{self._aws_region}:{acct}:repository/{repo}"
            await self._aws([
                "ecr",
                "tag-resource",
                "--resource-arn",
                arn,
                "--tags",
                "Key=sb:project,Value=sandboxes",
                "Key=sb:env,Value=kubernetes",
                f"Key=sb:name,Value={repo}",
            ], check=False)
        except Exception:
            pass

    async def _ecr_image_exists(self, repo: str, tag: str) -> bool:
        res = await self._aws([
            "ecr",
            "describe-images",
            "--repository-name",
            repo,
            "--image-ids",
            f"imageTag={tag}",
        ], check=False)
        return res.return_code == 0

    async def _ecr_docker_login(self, registry: str) -> None:
        # aws ecr get-login-password | docker login --username AWS --password-stdin <registry>
        pass_cmd = await self._run_local([
            "aws",
            "ecr",
            "get-login-password",
            *( ["--region", self._aws_region] if self._aws_region else [] ),
        ])
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            registry,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdin is not None:
            proc.stdin.write((pass_cmd.stdout or "").encode() if isinstance(pass_cmd.stdout, str) else (pass_cmd.stdout or b""))
            await proc.stdin.drain()
            proc.stdin.close()
        await proc.wait()
        if proc.returncode != 0:
            out = await proc.stdout.read() if proc.stdout else b""
            err = await proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(f"docker login to {registry} failed: {out.decode()} {err.decode()}")

    async def _build_and_push_ecr(self, image: str, force_build: bool) -> None:
        # Prefer buildx for platform control
        cmd = [
            "docker",
            "buildx",
            "build",
            "-t",
            image,
            str(self.environment_dir),
            "--push",
        ]
        if self._image_platform:
            cmd.extend(["--platform", self._image_platform])
        if force_build:
            cmd.append("--no-cache")
        await self._run_local(cmd)

    async def _build_with_kaniko(self, image: str, registry: str) -> None:
        """Build container image inside the remote cluster using Kaniko and push to ECR.

        This avoids local network bottlenecks by copying the build context into a
        temporary builder pod and running the Kaniko executor there.
        """
        # Create a temporary ECR docker-registry secret for Kaniko
        secret_name = self._sanitize_name(f"sb-ecr-dcfg-{self.session_id}")
        # Generate an ECR password and (re)create a docker-registry secret
        pass_res = await self._aws(["ecr", "get-login-password"])
        password = (pass_res.stdout or "").strip()
        # If secret exists, delete then create to refresh credentials
        chk = await self._kubectl(["get", "secret", secret_name], check=False)
        if chk.return_code == 0:
            await self._kubectl(["delete", "secret", secret_name])
        await self._kubectl([
            "create",
            "secret",
            "docker-registry",
            secret_name,
            "--docker-server",
            registry,
            "--docker-username",
            "AWS",
            "--docker-password",
            password,
        ])

        # Create a builder pod with Kaniko and an emptyDir workspace
        builder_name = self._sanitize_name(f"sb-build-{self.session_id}")
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": builder_name, "namespace": self._namespace},
            "spec": {
                "restartPolicy": "Never",
                "volumes": [
                    {"name": "work", "emptyDir": {}},
                    {
                        "name": "docker-config",
                        "secret": {
                            "secretName": secret_name,
                            "items": [{"key": ".dockerconfigjson", "path": "config.json"}],
                        },
                    },
                ],
                "containers": [
                    {
                        "name": "kaniko",
                        "image": "gcr.io/kaniko-project/executor:latest",
                        "command": ["sh", "-c", "sleep infinity"],
                        "volumeMounts": [
                            {"name": "work", "mountPath": "/workspace"},
                            {"name": "docker-config", "mountPath": "/kaniko/.docker"},
                        ],
                        "env": (
                            [{"name": "AWS_REGION", "value": self._aws_region}] if self._aws_region else []
                        ),
                    }
                ],
            },
        }
        await self._kubectl_apply(pod)
        # Wait until pod is ready
        await self._kubectl([
            "wait",
            f"pod/{builder_name}",
            "--for=condition=Ready",
            f"--timeout={int(self.task_env_config.build_timeout_sec)}s",
        ])

        # Copy build context into /workspace
        await self._kubectl([
            "cp",
            str(self.environment_dir) + "/.",
            f"{builder_name}:/workspace",
            "-c",
            "kaniko",
        ])

        # Run Kaniko to build and push
        res = await self._kubectl(
            [
                "exec",
                f"pod/{builder_name}",
                "-c",
                "kaniko",
                "--",
                "/kaniko/executor",
                "--context=/workspace",
                "--dockerfile=/workspace/Dockerfile",
                f"--destination={image}",
            ],
            check=False,
        )
        if res.return_code != 0:
            logs = await self._kubectl(["logs", builder_name, "-c", "kaniko"], check=False)
            await self._kubectl(["delete", f"pod/{builder_name}", "--ignore-not-found=true"], check=False)
            await self._kubectl(["delete", "secret", secret_name, "--ignore-not-found=true"], check=False)
            raise RuntimeError(
                f"Kaniko build failed: {res.stdout or res.stderr}\nLogs: {logs.stdout or logs.stderr}"
            )

        # Cleanup
        await self._kubectl(["delete", f"pod/{builder_name}", "--ignore-not-found=true"])
        await self._kubectl(["delete", "secret", secret_name, "--ignore-not-found=true"])

    async def _ensure_eks_context(self) -> None:
        # Ensure kubeconfig context is configured and aliased to a stable name
        alias_name = self._context or self._cluster_name
        args = [
            "eks",
            "update-kubeconfig",
            "--name",
            self._cluster_name,
            "--alias",
            alias_name,
        ]
        if self._aws_region:
            args.extend(["--region", self._aws_region])
        await self._aws(args)
        self._context = alias_name

    async def _ensure_eks_cluster(self) -> None:
        # Check if cluster exists
        res = await self._aws([
            "eks",
            "describe-cluster",
            "--name",
            self._cluster_name,
            "--query",
            "cluster.status",
            "--output",
            "text",
        ], check=False)
        if res.return_code == 0 and (res.stdout or "").strip() in {"ACTIVE", "CREATING", "UPDATING"}:
            return
        if not self._provision:
            raise RuntimeError(
                f"EKS cluster {self._cluster_name} not found and provision=false. Create it or set provision=true."
            )
        # Try eksctl for a simple cluster creation
        eksctl = await self._run_local(["bash", "-lc", "command -v eksctl"], check=False)
        if eksctl.return_code == 0:
            create_cmd = [
                "eksctl",
                "create",
                "cluster",
                "--name",
                self._cluster_name,
            ]
            if self._aws_region:
                create_cmd.extend(["--region", self._aws_region])
            # Minimal defaults: 2 nodes, on-demand
            create_cmd.extend(["--nodes", "2", "--node-type", "m6i.large"])
            await self._run_local(create_cmd)
        else:
            raise RuntimeError(
                "eksctl is required for automatic cluster provisioning. Install eksctl or pre-create the cluster."
            )

    async def _ensure_image_pull_secret(self) -> None:
        # If secret exists, skip
        check = await self._kubectl(["get", "secret", self._image_pull_secret], check=False)
        if check.return_code == 0:
            return
        account_id = await self._aws_get_account_id()
        registry = f"{account_id}.dkr.ecr.{self._aws_region}.amazonaws.com"
        # Create docker-registry secret using ECR password
        pass_res = await self._aws(["ecr", "get-login-password"], check=True)
        password = (pass_res.stdout or "").strip()
        # kubectl create secret docker-registry ...
        args = [
            "create",
            "secret",
            "docker-registry",
            self._image_pull_secret,
            "--docker-server",
            registry,
            "--docker-username",
            "AWS",
            "--docker-password",
            password,
        ]
        await self._kubectl(args)

    async def _ensure_s3_bucket(self, bucket: str) -> None:
        res = await self._aws(["s3api", "head-bucket", "--bucket", bucket], check=False)
        if res.return_code == 0:
            return
        if self._aws_region == "us-east-1":
            await self._aws(["s3api", "create-bucket", "--bucket", bucket])
        else:
            await self._aws([
                "s3api",
                "create-bucket",
                "--bucket",
                bucket,
                "--create-bucket-configuration",
                f"LocationConstraint={self._aws_region}",
            ])

    async def _zip_and_upload_context(self, bucket: str, repo_base: str, tag: str) -> str:
        s3_key = f"tasks/{repo_base}/{tag}/context.zip"
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "context.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(self.environment_dir.rglob("*")):
                    if p.is_file():
                        zf.write(p, arcname=p.relative_to(self.environment_dir).as_posix())
            await self._aws(["s3", "cp", str(zip_path), f"s3://{bucket}/{s3_key}"])
        return s3_key

    async def _ensure_codebuild_role(self) -> str:
        role_name = f"{CloudDefaults().stack}-codebuild-role"
        res = await self._aws(["iam", "get-role", "--role-name", role_name], check=False)
        if res.return_code == 0:
            data = json.loads(res.stdout or "{}")
            return data["Role"]["Arn"]
        assume = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "codebuild.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        await self._aws([
            "iam",
            "create-role",
            "--role-name",
            role_name,
            "--assume-role-policy-document",
            json.dumps(assume),
        ])
        for policy_arn in [
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser",
            "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
            "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
        ]:
            await self._aws([
                "iam",
                "attach-role-policy",
                "--role-name",
                role_name,
                "--policy-arn",
                policy_arn,
            ])
        role = await self._aws(["iam", "get-role", "--role-name", role_name])
        data = json.loads(role.stdout or "{}")
        return data["Role"]["Arn"]

    async def _ensure_codebuild_project(self) -> None:
        res = await self._aws([
            "codebuild",
            "batch-get-projects",
            "--names",
            self._codebuild_project,
            "--output",
            "json",
        ], check=False)
        if res.return_code == 0 and json.loads(res.stdout or "{}").get("projects"):
            return
        role_arn = await self._ensure_codebuild_role()
        # Build request via CLI JSON to avoid quoting issues
        project_def = {
            "name": self._codebuild_project,
            "serviceRole": role_arn,
            "source": {
                "type": "NO_SOURCE",
                "buildspec": "version: 0.2\nphases:\n  build:\n    commands:\n      - echo sandboxes\n",
            },
            "artifacts": {"type": "NO_ARTIFACTS"},
            "environment": {
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_MEDIUM",
                "privilegedMode": True,
            },
        }
        await self._aws([
            "codebuild",
            "create-project",
            "--cli-input-json",
            json.dumps(project_def),
        ])

    async def _start_codebuild_build(
        self, s3_bucket: str, s3_key: str, image_uri: str, platform: str
    ) -> tuple[str, str]:
        buildspec = (
            "version: 0.2\n"
            "phases:\n"
            "  pre_build:\n"
            "    commands:\n"
            "      - ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)\n"
            "      - ECR_REG=$ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com\n"
            "      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REG\n"
            "      - export DOCKER_BUILDKIT=1\n"
            "      - docker buildx create --use --bootstrap || true\n"
            "  build:\n"
            "    commands:\n"
            "      - CACHE_REF=$ECR_REG/$ECR_REPO:buildcache\n"
            "      - docker buildx build --platform ${PLATFORM:-linux/amd64} --cache-from=type=registry,ref=$CACHE_REF --cache-to=type=registry,ref=$CACHE_REF,mode=max -t $IMAGE_URI --push .\n"
        )
        payload = {
            "projectName": self._codebuild_project,
            "sourceTypeOverride": "S3",
            "sourceLocationOverride": f"{s3_bucket}/{s3_key}",
            "buildspecOverride": buildspec,
            "environmentVariablesOverride": [
                {"name": "IMAGE_URI", "value": image_uri, "type": "PLAINTEXT"},
                {"name": "PLATFORM", "value": platform, "type": "PLAINTEXT"},
                {"name": "ECR_REPO", "value": self._ecr_repo, "type": "PLAINTEXT"},
            ],
        }
        res = await self._aws([
            "codebuild",
            "start-build",
            "--cli-input-json",
            json.dumps(payload),
            "--output",
            "json",
        ])
        data = json.loads(res.stdout or "{}")
        build_id = data["build"]["id"]
        url = f"https://console.aws.amazon.com/codesuite/codebuild/projects/{self._codebuild_project}/build/{build_id}/?region={self._aws_region}"
        return build_id, url

    async def _poll_codebuild_build(self, build_id: str) -> None:
        while True:
            res = await self._aws(["codebuild", "batch-get-builds", "--ids", build_id, "--output", "json"])
            data = json.loads(res.stdout or "{}")
            builds = data.get("builds", [])
            if not builds:
                raise RuntimeError("CodeBuild build not found")
            status = builds[0].get("buildStatus")
            if status == "SUCCEEDED":
                return
            if status in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
                raise RuntimeError(f"CodeBuild build failed with status {status}")
            await asyncio.sleep(5)

    async def _kubectl(
        self,
        args: list[str],
        stdin: bytes | None = None,
        timeout_sec: int | None = None,
        check: bool = True,
    ) -> ExecResult:
        cmd = ["kubectl"]
        if self._kubeconfig:
            env = os.environ.copy()
            env["KUBECONFIG"] = str(self._kubeconfig)
        else:
            env = os.environ.copy()

        if self._context:
            cmd.extend(["--context", self._context])
        if self._namespace and args[:1] != ["config"]:
            # Namespace flag applies to most commands; avoid for `kubectl config`.
            cmd.extend(["-n", self._namespace])
        cmd.extend(args)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            if timeout_sec:
                await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
            else:
                await proc.wait()
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            raise

        stdout = await proc.stdout.read() if proc.stdout else None
        stderr = await proc.stderr.read() if proc.stderr else None
        result = ExecResult(
            stdout=stdout.decode() if stdout else None,
            stderr=stderr.decode() if stderr else None,
            return_code=proc.returncode or 0,
        )

        if check and result.return_code != 0:
            raise RuntimeError(
                f"kubectl command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    async def _kubectl_with_retries(
        self,
        args: list[str],
        stdin: bytes | None = None,
        timeout_sec: int | None = None,
        check: bool = True,
        retries: int = 5,
        base_delay: float = 0.5,
        max_delay: float = 5.0,
    ) -> ExecResult:
        """Run kubectl with simple exponential backoff retries (for cp-heavy paths).

        This reduces transient API server/refused-connection failures when many
        trials concurrently invoke `kubectl cp`.
        """
        last: ExecResult | None = None
        for attempt in range(retries):
            res = await self._kubectl(args, stdin=stdin, timeout_sec=timeout_sec, check=False)
            if res.return_code == 0:
                return res
            last = res
            # Backoff with jitter
            delay = min(max_delay, base_delay * (2 ** attempt))
            delay = delay * (0.5 + random.random())
            await asyncio.sleep(delay)
        # Exhausted retries
        if check:
            cmd = " ".join(["kubectl"] + (["--context", self._context] if self._context else []) + (["-n", self._namespace] if self._namespace else []) + args)
            raise RuntimeError(
                f"kubectl command failed after {retries} retries: {cmd}\nstdout: {last.stdout if last else ''}\nstderr: {last.stderr if last else ''}"
            )
        return last  # type: ignore[return-value]

    async def _run_local(
        self, cmd: list[str], check: bool = True, timeout_sec: int | None = None
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            if timeout_sec:
                await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
            else:
                await proc.wait()
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            raise

        stdout = await proc.stdout.read() if proc.stdout else None
        stderr = await proc.stderr.read() if proc.stderr else None
        result = ExecResult(
            stdout=stdout.decode() if stdout else None,
            stderr=stderr.decode() if stderr else None,
            return_code=proc.returncode or 0,
        )
        if check and result.return_code != 0:
            raise RuntimeError(
                f"Local command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def _parse_workdir(self) -> str | None:
        try:
            lines = self._environment_definition_path.read_text().splitlines()
        except Exception:
            return None
        for line in reversed(lines):
            s = line.strip()
            if s.upper().startswith("WORKDIR"):
                parts = s.split(maxsplit=1)
                if len(parts) == 2:
                    return parts[1]
        return None

    def _sanitize_name(self, name: str) -> str:
        # DNS-1123 label: lowercase alphanumerics, '-' allowed, must start/end alphanumeric, <=63
        name = name.lower()
        name = re.sub(r"[^a-z0-9-]", "-", name)
        name = re.sub(r"^-+", "", name)
        name = re.sub(r"-+$", "", name)
        if not name:
            name = "sb"
        return name[:63]
