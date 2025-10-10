import traceback
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sandboxes.models.agent.context import AgentContext
from sandboxes.models.task.id import GitTaskId, LocalTaskId
from sandboxes.models.trial.config import TrialConfig
from sandboxes.models.verifier.result import VerifierResult


class TimingInfo(BaseModel):
    """Timing information for a phase of trial execution."""

    started_at: datetime | None = None
    finished_at: datetime | None = None


class ExceptionInfo(BaseModel):
    """Information about an exception that occurred during trial execution."""

    exception_type: str
    exception_message: str
    exception_traceback: str
    occurred_at: datetime

    @classmethod
    def from_exception(cls, e: BaseException) -> "ExceptionInfo":
        return cls(
            exception_type=type(e).__name__,
            exception_message=str(e),
            exception_traceback=traceback.format_exc(),
            occurred_at=datetime.now(),
        )


class ModelInfo(BaseModel):
    """Information about a model that participated in a trial."""

    name: str
    provider: str


class AgentInfo(BaseModel):
    """Information about an agent that participated in a trial."""

    name: str
    version: str
    model_info: ModelInfo | None = None


class TrialResult(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    task_name: str
    trial_name: str
    trial_uri: str
    # Optional S3 mirror locations (populated for remote runs)
    s3_task_uri: str | None = None
    s3_trial_uri: str | None = None
    task_id: LocalTaskId | GitTaskId
    task_checksum: str
    config: TrialConfig
    agent_info: AgentInfo
    agent_result: AgentContext | None = None
    verifier_result: VerifierResult | None = None
    exception_info: ExceptionInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    environment_setup: TimingInfo | None = None
    agent_setup: TimingInfo | None = None
    agent_execution: TimingInfo | None = None
    verifier: TimingInfo | None = None
