import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CloudDefaults:
    region: str = os.environ.get("SB_AWS_REGION", "us-west-2")
    stack: str = os.environ.get("SB_STACK", "abundant-sb")
    eks_cluster: str = os.environ.get("SB_EKS_CLUSTER", "abundant-sb-eks")
    ecr_repo: str = os.environ.get("SB_ECR_REPO", "abundant-sb-sandboxes")
    s3_bucket: str = os.environ.get("SB_S3_BUCKET", "abundant-sb-artifacts")
    codebuild_project: str = os.environ.get("SB_CODEBUILD_PROJECT", "abundant-sb-build")
    k8s_namespace: str = os.environ.get("SB_K8S_NAMESPACE", "agents")
    image_platform: str = os.environ.get("SB_IMAGE_PLATFORM", "linux/amd64")
