from pathlib import Path
from typing import Annotated

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from typer import Option, Typer

from sandboxes.cli.sb.jobs import start as job_start
from sandboxes.cli.sb.trials import start as trial_start
from sandboxes.models.environment_type import EnvironmentType
from sandboxes.models.agent.name import AgentName
from sandboxes.cloud.defaults import CloudDefaults
from sandboxes.cloud.aws_ensure import (
    AwsContext,
    ensure_all,
    ensure_s3_bucket,
    ensure_ecr_repo,
    ensure_codebuild_project,
    ensure_eks_context,
    ensure_eks_oidc,
    ensure_network_tags_for_karpenter,
    ensure_node_instance_profile,
    ensure_karpenter_install,
    delete_codebuild_project,
    delete_ecr_repo,
    purge_s3_bucket,
    delete_eks_cluster,
    uninstall_karpenter,
)

cloud_app = Typer(no_args_is_help=True)
console = Console()


@cloud_app.command()
def init(
    provider: Annotated[str, Option("--provider", help="Cloud provider", show_default=True)] = "aws",
):
    if provider != "aws":
        raise ValueError("Only 'aws' provider is supported right now")

    console.print("[bold]Ensuring cloud infrastructure (AWS)…[/bold] (S3/ECR/CodeBuild/EKS and Karpenter autoscaling)")
    cd = CloudDefaults()
    ctx = AwsContext(
        region=cd.region,
        cluster_name=cd.eks_cluster,
        ecr_repo=cd.ecr_repo,
        s3_bucket=cd.s3_bucket,
        codebuild_project=cd.codebuild_project,
    )
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            transient=True,
        ) as progress:
            progress.add_task("Ensuring S3 bucket…", total=None)
            ensure_s3_bucket(ctx)
            progress.add_task("Ensuring ECR repo…", total=None)
            ensure_ecr_repo(ctx)
            progress.add_task("Ensuring CodeBuild project…", total=None)
            ensure_codebuild_project(ctx)
            progress.add_task("Ensuring EKS kubecontext…", total=None)
            ensure_eks_context(ctx)
            progress.add_task("Associating EKS OIDC (IRSA)…", total=None)
            ensure_eks_oidc(ctx)
            progress.add_task("Tagging subnets/SGs for Karpenter…", total=None)
            ensure_network_tags_for_karpenter(ctx)
            progress.add_task("Ensuring node InstanceProfile…", total=None)
            ensure_node_instance_profile(ctx)
            progress.add_task("Installing/Upgrading Karpenter…", total=None)
            ensure_karpenter_install(ctx, instance_profile="")
        console.print("[green]Cloud infra ensured successfully.[/green]")
    except Exception as e:
        console.print(f"[red]Cloud init failed:[/red] {e}")
        raise


@cloud_app.command()
def down(
    provider: Annotated[str, Option("--provider", help="Cloud provider", show_default=True)] = "aws",
    exclude_s3: Annotated[bool, Option("--exclude-s3/--include-s3", help="Do not delete S3 bucket (default: delete)")] = False,
    exclude_ecr: Annotated[bool, Option("--exclude-ecr/--include-ecr", help="Do not delete ECR repo (default: delete)")] = False,
    exclude_eks: Annotated[bool, Option("--exclude-eks/--include-eks", help="Do not delete EKS cluster (default: delete)")] = False,
    exclude_codebuild: Annotated[bool, Option("--exclude-codebuild/--include-codebuild", help="Do not delete CodeBuild project (default: delete)")] = False,
):
    if provider != "aws":
        raise ValueError("Only 'aws' provider is supported right now")

    console.print("[bold]Tearing down cloud infrastructure (AWS)…[/bold]")
    cd = CloudDefaults()
    ctx = AwsContext(
        region=cd.region,
        cluster_name=cd.eks_cluster,
        ecr_repo=cd.ecr_repo,
        s3_bucket=cd.s3_bucket,
        codebuild_project=cd.codebuild_project,
    )
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            transient=True,
        ) as progress:
            progress.add_task("Ensuring EKS context…", total=None)
            ensure_eks_context(ctx)
            progress.add_task("Uninstalling Karpenter…", total=None)
            uninstall_karpenter(ctx)
            if not exclude_codebuild:
                progress.add_task("Deleting CodeBuild project…", total=None)
                delete_codebuild_project(ctx)
            if not exclude_ecr:
                progress.add_task("Deleting ECR repository…", total=None)
                delete_ecr_repo(ctx)
            if not exclude_s3:
                progress.add_task("Purging S3 bucket…", total=None)
                purge_s3_bucket(ctx)
            if not exclude_eks:
                progress.add_task("Deleting EKS cluster… (this may take minutes)", total=None)
                delete_eks_cluster(ctx)
        console.print("[green]Cloud teardown finished (cluster deletion may still be in progress).[/green]")
    except Exception as e:
        console.print(f"[red]Cloud teardown failed:[/red] {e}")
        raise


@cloud_app.command()
def run(
    task_path: Annotated[
        Path,
        Option("-t", "--task-path", help="Task directory or directory of tasks", rich_help_panel="Task"),
    ],
    agent_name: Annotated[
        str | None,
        Option("-a", "--agent", help="Agent name (default: oracle)", show_default=False),
    ] = None,
    model_name: Annotated[
        str | None,
        Option("-m", "--model", help="Model name for the agent", show_default=False),
    ] = None,
    no_provision: Annotated[
        bool, Option("--no-provision", help="Use existing infra; do not provision")
    ] = False,
    upload_results: Annotated[
        bool, Option("--upload-results/--no-upload-results", help="Upload trial_dir to S3 at end", show_default=False)
    ] = True,
    timeout_multiplier: Annotated[
        float | None,
        Option("--timeout-multiplier", help="Multiply task timeouts (env/agent/verifier)", show_default=False),
    ] = None,
):
    """Run task(s) remotely (CodeBuild+S3+ECR+EKS) with sensible defaults."""

    # Single trial vs. job over directory
    if (task_path / "task.toml").exists():
        agent_enum = AgentName(agent_name) if agent_name is not None else None
        trial_start(
            task_path=task_path,
            agent_name=agent_enum,
            model_name=model_name,
            environment_type=EnvironmentType.KUBERNETES,
            environment_force_build=False,
            environment_delete=True,
            environment_kwargs=[
                "cluster=remote",
                f"provision={'false' if no_provision else 'true'}",
                f"upload_results={'true' if upload_results else 'false'}",
            ],
            timeout_multiplier=timeout_multiplier,
        )
    else:
        agent_enum = AgentName(agent_name) if agent_name is not None else None
        # Submit all tasks; cluster/backends will queue/scale as needed.
        # Internally set orchestrator concurrency to the number of tasks so we don't
        # throttle from the client side.
        try:
            task_count = sum(1 for p in task_path.iterdir() if (p / "task.toml").exists())
        except Exception:
            task_count = None
        job_start(
            config_path=None,
            job_name=None,
            jobs_dir=None,
            n_attempts=None,
            timeout_multiplier=timeout_multiplier,
            quiet=False,
            orchestrator_type=None,
            n_concurrent_trials=task_count,
            dataset_path=task_path,
            dataset_name_version=None,
            registry_url=None,
            registry_path=None,
            dataset_task_names=None,
            dataset_exclude_task_names=None,
            orchestrator_kwargs=None,
            agent_name=agent_enum,
            agent_import_path=None,
            model_names=[model_name] if model_name else None,
            agent_kwargs=None,
            environment_type=EnvironmentType.KUBERNETES,
            environment_force_build=False,
            environment_delete=True,
            environment_kwargs=["cluster=remote", f"provision={'false' if no_provision else 'true'}", f"upload_results={'true' if upload_results else 'false'}"],
            task_path=None,
            task_git_url=None,
            task_git_commit_id=None,
            export_traces=False,
            export_sharegpt=False,
            export_episodes="all",
            export_push=False,
            export_repo=None,
        )
