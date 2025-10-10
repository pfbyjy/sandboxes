import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class AwsContext:
    region: str
    cluster_name: str
    ecr_repo: str
    s3_bucket: str
    codebuild_project: str


def _run(cmd: list[str], env: Optional[dict] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)


def ensure_s3_bucket(ctx: AwsContext) -> None:
    res = _run(["aws", "s3api", "head-bucket", "--bucket", ctx.s3_bucket], check=False)
    if res.returncode == 0:
        return
    if ctx.region == "us-east-1":
        _run(["aws", "s3api", "create-bucket", "--bucket", ctx.s3_bucket, "--region", ctx.region])
    else:
        _run([
            "aws",
            "s3api",
            "create-bucket",
            "--bucket",
            ctx.s3_bucket,
            "--create-bucket-configuration",
            json.dumps({"LocationConstraint": ctx.region}),
            "--region",
            ctx.region,
        ])


def ensure_ecr_repo(ctx: AwsContext) -> None:
    res = _run([
        "aws",
        "ecr",
        "describe-repositories",
        "--repository-names",
        ctx.ecr_repo,
        "--region",
        ctx.region,
    ], check=False)
    if res.returncode == 0:
        return
    _run(["aws", "ecr", "create-repository", "--repository-name", ctx.ecr_repo, "--region", ctx.region])


def ensure_codebuild_project(ctx: AwsContext) -> None:
    res = _run([
        "aws",
        "codebuild",
        "batch-get-projects",
        "--names",
        ctx.codebuild_project,
        "--region",
        ctx.region,
        "--output",
        "json",
    ], check=False)
    if res.returncode == 0 and json.loads(res.stdout or "{}").get("projects"):
        return

    # Ensure role
    role_name = f"{ctx.cluster_name}-codebuild-role"
    get_role = _run(["aws", "iam", "get-role", "--role-name", role_name], check=False)
    if get_role.returncode != 0:
        assume = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}
            ],
        }
        _run(["aws", "iam", "create-role", "--role-name", role_name, "--assume-role-policy-document", json.dumps(assume)])
        for policy_arn in [
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser",
            "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
            "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
        ]:
            _run(["aws", "iam", "attach-role-policy", "--role-name", role_name, "--policy-arn", policy_arn])
    role = _run(["aws", "iam", "get-role", "--role-name", role_name])
    role_arn = json.loads(role.stdout)["Role"]["Arn"]

    project_def = {
        "name": ctx.codebuild_project,
        "serviceRole": role_arn,
        "source": {"type": "NO_SOURCE", "buildspec": "version: 0.2\nphases:\n  build:\n    commands:\n      - echo sandboxes\n"},
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": {
            "type": "LINUX_CONTAINER",
            "image": "aws/codebuild/standard:7.0",
            "computeType": "BUILD_GENERAL1_MEDIUM",
            "privilegedMode": True,
        },
    }
    _run([
        "aws",
        "codebuild",
        "create-project",
        "--cli-input-json",
        json.dumps(project_def),
        "--region",
        ctx.region,
    ])


def ensure_eks_context(ctx: AwsContext) -> None:
    _run([
        "aws",
        "eks",
        "update-kubeconfig",
        "--name",
        ctx.cluster_name,
        "--alias",
        ctx.cluster_name,
        "--region",
        ctx.region,
    ])


def ensure_eks_oidc(ctx: AwsContext) -> None:
    # Idempotent OIDC association
    _run([
        "eksctl",
        "utils",
        "associate-iam-oidc-provider",
        "--cluster",
        ctx.cluster_name,
        "--region",
        ctx.region,
        "--approve",
    ], check=False)


def _eks_cluster_info(ctx: AwsContext) -> dict:
    res = _run(["aws", "eks", "describe-cluster", "--name", ctx.cluster_name, "--region", ctx.region, "--output", "json"])
    return json.loads(res.stdout)["cluster"]


def ensure_network_tags_for_karpenter(ctx: AwsContext) -> None:
    cluster = _eks_cluster_info(ctx)
    subnets = cluster.get("resourcesVpcConfig", {}).get("subnetIds", [])
    sgs = cluster.get("resourcesVpcConfig", {}).get("securityGroupIds", [])
    for sn in subnets:
        _run([
            "aws", "ec2", "create-tags", "--resources", sn, "--tags",
            f"Key=karpenter.sh/discovery,Value={ctx.cluster_name}",
            "--region", ctx.region,
        ], check=False)
    for sg in sgs:
        _run([
            "aws", "ec2", "create-tags", "--resources", sg, "--tags",
            f"Key=karpenter.sh/discovery,Value={ctx.cluster_name}",
            "--region", ctx.region,
        ], check=False)


def ensure_node_instance_profile(ctx: AwsContext) -> str:
    profile_name = f"{ctx.cluster_name}-node"
    role_name = f"{ctx.cluster_name}-node-role"
    get_prof = _run(["aws", "iam", "get-instance-profile", "--instance-profile-name", profile_name], check=False)
    if get_prof.returncode != 0:
        _run(["aws", "iam", "create-instance-profile", "--instance-profile-name", profile_name])
    get_role = _run(["aws", "iam", "get-role", "--role-name", role_name], check=False)
    if get_role.returncode != 0:
        assume = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        }
        _run(["aws", "iam", "create-role", "--role-name", role_name, "--assume-role-policy-document", json.dumps(assume)])
        for policy_arn in [
            "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        ]:
            _run(["aws", "iam", "attach-role-policy", "--role-name", role_name, "--policy-arn", policy_arn])
    # Ensure role attached to instance profile
    _run(["aws", "iam", "add-role-to-instance-profile", "--instance-profile-name", profile_name, "--role-name", role_name], check=False)
    return profile_name


def ensure_karpenter_install(ctx: AwsContext, instance_profile: str) -> None:
    # Ensure ServiceAccount via eksctl with permissive policy (quickstart); production should scope this down.
    _run([
        "eksctl",
        "create",
        "iamserviceaccount",
        "--cluster",
        ctx.cluster_name,
        "--namespace",
        "karpenter",
        "--name",
        "karpenter",
        "--attach-policy-arn",
        "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
        "--attach-policy-arn",
        "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess",
        "--override-existing-serviceaccounts",
        "--approve",
        "--region",
        ctx.region,
    ], check=False)

    # Helm install/upgrade Karpenter
    _run(["helm", "repo", "add", "karpenter", "https://charts.karpenter.sh/"], check=False)
    _run(["helm", "repo", "update"], check=False)

    # Discover cluster endpoint
    cluster = _eks_cluster_info(ctx)
    endpoint = cluster.get("endpoint")

    # Install chart; use existing SA created by eksctl
    _run([
        "helm", "upgrade", "--install", "karpenter", "karpenter/karpenter",
        "--namespace", "karpenter", "--create-namespace",
        "--set", f"settings.clusterName={ctx.cluster_name}",
        "--set", f"settings.clusterEndpoint={endpoint}",
        "--set", "serviceAccount.create=false",
    ])

    # Apply NodeClass + Provisioner
    nodeclass = f"""
apiVersion: karpenter.k8s.aws/v1beta1
kind: EC2NodeClass
metadata:
  name: abundant-sb
spec:
  amiFamily: AL2
  role: {ctx.cluster_name}-node-role
  subnetSelector:
    karpenter.sh/discovery: {ctx.cluster_name}
  securityGroupSelector:
    karpenter.sh/discovery: {ctx.cluster_name}
"""
    provisioner = f"""
apiVersion: karpenter.sh/v1beta1
kind: Provisioner
metadata:
  name: default
spec:
  nodeClassRef:
    name: abundant-sb
  limits:
    resources:
      cpu: "64"
      memory: 256Gi
  consolidation:
    enabled: true
  ttlSecondsAfterEmpty: 60
  requirements:
    - key: karpenter.k8s.aws/instance-family
      operator: In
      values: ["m6i", "c6i"]
    - key: karpenter.sh/capacity-type
      operator: In
      values: ["spot", "on-demand"]
"""
    _run(["kubectl", "apply", "-f", "-"], env=os.environ, check=True).stdout
    # Pipe manifests separately
    subprocess.run(["kubectl", "apply", "-f", "-"], input=nodeclass, text=True, check=False)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=provisioner, text=True, check=False)


def ensure_autoscaling(ctx: AwsContext) -> None:
    ensure_eks_context(ctx)
    ensure_eks_oidc(ctx)
    ensure_network_tags_for_karpenter(ctx)
    prof = ensure_node_instance_profile(ctx)
    ensure_karpenter_install(ctx, prof)


def ensure_all(ctx: AwsContext) -> None:
    ensure_s3_bucket(ctx)
    ensure_ecr_repo(ctx)
    ensure_codebuild_project(ctx)
    ensure_autoscaling(ctx)


# ---------------- Teardown ---------------- #

def _helm_uninstall_karpenter() -> None:
    _run(["helm", "uninstall", "karpenter", "-n", "karpenter"], check=False)


def _delete_karpenter_crs() -> None:
    # Best-effort delete of default CRs we created
    _run(["kubectl", "delete", "provisioner", "default"], check=False)
    _run(["kubectl", "delete", "ec2nodeclass", "abundant-sb"], check=False)


def _delete_karpenter_sa(ctx: AwsContext) -> None:
    _run([
        "eksctl",
        "delete",
        "iamserviceaccount",
        "--cluster",
        ctx.cluster_name,
        "--namespace",
        "karpenter",
        "--name",
        "karpenter",
        "--region",
        ctx.region,
        "--approve",
    ], check=False)


def _delete_instance_profile(ctx: AwsContext) -> None:
    profile_name = f"{ctx.cluster_name}-node"
    role_name = f"{ctx.cluster_name}-node-role"
    # Detach role from instance profile
    _run(["aws", "iam", "remove-role-from-instance-profile", "--instance-profile-name", profile_name, "--role-name", role_name], check=False)
    # Detach policies
    for policy_arn in [
        "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    ]:
        _run(["aws", "iam", "detach-role-policy", "--role-name", role_name, "--policy-arn", policy_arn], check=False)
    # Delete role and instance profile
    _run(["aws", "iam", "delete-role", "--role-name", role_name], check=False)
    _run(["aws", "iam", "delete-instance-profile", "--instance-profile-name", profile_name], check=False)


def delete_codebuild_project(ctx: AwsContext) -> None:
    _run(["aws", "codebuild", "delete-project", "--name", ctx.codebuild_project, "--region", ctx.region], check=False)


def delete_ecr_repo(ctx: AwsContext) -> None:
    _run(["aws", "ecr", "delete-repository", "--repository-name", ctx.ecr_repo, "--force", "--region", ctx.region], check=False)


def purge_s3_bucket(ctx: AwsContext) -> None:
    # Remove bucket and contents
    _run(["aws", "s3", "rb", f"s3://{ctx.s3_bucket}", "--force"], check=False)


def delete_eks_cluster(ctx: AwsContext) -> None:
    _run(["eksctl", "delete", "cluster", "--name", ctx.cluster_name, "--region", ctx.region], check=False)


def teardown_all(ctx: AwsContext, purge_s3: bool = False, purge_ecr: bool = False) -> None:
    # Best-effort uninstall order
    ensure_eks_context(ctx)
    _delete_karpenter_crs()
    _helm_uninstall_karpenter()
    _delete_karpenter_sa(ctx)
    _delete_instance_profile(ctx)
    delete_codebuild_project(ctx)
    if purge_ecr:
        delete_ecr_repo(ctx)
    if purge_s3:
        purge_s3_bucket(ctx)
    delete_eks_cluster(ctx)


def uninstall_karpenter(ctx: AwsContext) -> None:
    """Public wrapper to uninstall Karpenter resources in order."""
    ensure_eks_context(ctx)
    _delete_karpenter_crs()
    _helm_uninstall_karpenter()
    _delete_karpenter_sa(ctx)
    _delete_instance_profile(ctx)
