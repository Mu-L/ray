from collections import OrderedDict
from unittest.mock import MagicMock

import pytest

import ray
from ray.actor import ActorHandle
from ray.train.v2._internal.callbacks.state_manager import StateManagerCallback
from ray.train.v2._internal.exceptions import WorkerGroupStartupTimeoutError
from ray.train.v2._internal.execution.context import DistributedContext
from ray.train.v2._internal.execution.controller.state import (
    ErroredState,
    FinishedState,
    InitializingState,
    ReschedulingState,
    ResizingState,
    RestartingState,
    RunningState,
    SchedulingState,
)
from ray.train.v2._internal.execution.scaling_policy import ResizeDecision
from ray.train.v2._internal.execution.worker_group import (
    ActorMetadata,
    Worker,
    WorkerGroup,
    WorkerGroupContext,
)
from ray.train.v2._internal.state.schema import (
    ActorStatus,
    RunAttemptStatus,
    RunStatus,
    TrainResources,
    TrainRun,
    TrainRunAttempt,
)
from ray.train.v2._internal.state.state_actor import (
    TrainStateActor,
    get_state_actor,
)
from ray.train.v2._internal.state.state_manager import TrainStateManager
from ray.train.v2._internal.state.util import _DEAD_CONTROLLER_ABORT_STATUS_DETAIL
from ray.train.v2.api.exceptions import ControllerError, WorkerGroupError
from ray.train.v2.tests.util import (
    create_dummy_run_context,
    create_mock_train_run,
    create_mock_train_run_attempt,
)
from ray.util.state.common import ActorState


def create_mock_actor_state(state: ActorStatus):
    return ActorState(
        state=state,
        actor_id="mock_actor_id",
        class_name="mock_class_name",
        job_id="mock_job_id",
        name="mock_name",
        node_id="mock_node_id",
        pid=1234,
        ray_namespace="mock_ray_namespace",
    )


@pytest.fixture(scope="function")
def ray_start_regular():
    ray.init()
    yield
    ray.shutdown()


@pytest.fixture
def mock_worker_group_context():
    context = MagicMock(spec=WorkerGroupContext)
    context.run_attempt_id = "attempt_1"
    context.num_workers = 2
    context.resources_per_worker = {"CPU": 1}
    return context


def get_mock_actor(actor_id: str):
    actor = MagicMock(spec=ActorHandle)
    actor._actor_id.hex.return_value = actor_id
    return actor


@pytest.fixture
def mock_worker():
    actor = get_mock_actor("actor_1")

    metadata = MagicMock(spec=ActorMetadata)
    metadata.node_id = "node_1"
    metadata.node_ip = "127.0.0.1"
    metadata.pid = 1000
    metadata.gpu_ids = []

    distributed_context = MagicMock(spec=DistributedContext)
    distributed_context.world_rank = 0
    distributed_context.local_rank = 0
    distributed_context.node_rank = 0

    return Worker(
        actor=actor,
        metadata=metadata,
        resources={"CPU": 1},
        distributed_context=distributed_context,
        log_file_path="/tmp/ray/session_xxx/logs/train/ray-train-app-worker.log",
    )


@pytest.fixture
def mock_worker_group(mock_worker_group_context, mock_worker):
    group = MagicMock(spec=WorkerGroup)
    group.get_worker_group_context.return_value = mock_worker_group_context
    group.get_worker_group_state.return_value = MagicMock(workers=[mock_worker])
    group.get_latest_poll_status.return_value = None
    return group


@pytest.fixture
def callback(monkeypatch):
    # Mock the runtime context to return a fixed actor ID
    mock_runtime_context = MagicMock()
    mock_runtime_context.get_job_id.return_value = "test_job_id"
    mock_runtime_context.get_actor_id.return_value = "test_controller_id"
    monkeypatch.setattr(
        ray.runtime_context, "get_runtime_context", lambda: mock_runtime_context
    )

    # Mock the log path function
    expected_controller_log_path = (
        "/tmp/ray/session_xxx/logs/train/ray-train-app-controller.log"
    )
    monkeypatch.setattr(
        ray.train.v2._internal.callbacks.state_manager,
        "get_train_application_controller_log_path",
        lambda: expected_controller_log_path,
    )

    callback = StateManagerCallback()
    callback.after_controller_start(train_run_context=create_dummy_run_context())
    return callback


def test_train_state_actor_create_and_get_run(ray_start_regular):
    """Test basic CRUD operations for train runs in the state actor."""
    actor = ray.remote(TrainStateActor).remote()

    # Test creation with minimal fields
    run = TrainRun(
        id="test_run",
        name="test",
        job_id="job_1",
        status=RunStatus.INITIALIZING,
        status_detail=None,
        controller_actor_id="controller_1",
        start_time_ns=1000,
        controller_log_file_path="/tmp/ray/session_xxx/logs/train/ray-train-app-controller.log",
    )

    ray.get(actor.create_or_update_train_run.remote(run))
    runs = ray.get(actor.get_train_runs.remote())

    assert len(runs) == 1
    assert "test_run" in runs
    stored_run = runs["test_run"]
    assert stored_run == run  # Check full equality

    # Test update preserves unmodified fields
    updated_run = run.copy(
        update={"status": RunStatus.RUNNING, "status_detail": "Now running"}
    )
    ray.get(actor.create_or_update_train_run.remote(updated_run))

    runs = ray.get(actor.get_train_runs.remote())
    stored_run = runs["test_run"]
    assert stored_run == updated_run
    assert stored_run.start_time_ns == run.start_time_ns  # Original field preserved


def test_train_state_actor_create_and_get_run_attempt(ray_start_regular):
    actor = ray.remote(TrainStateActor).remote()

    resources = [TrainResources(resources={"CPU": 1})]
    run_attempt = TrainRunAttempt(
        run_id="test_run",
        attempt_id="attempt_1",
        status=RunAttemptStatus.PENDING,
        status_detail=None,
        start_time_ns=1000,
        resources=resources,
        workers=[],
    )

    # Test creation
    ray.get(actor.create_or_update_train_run_attempt.remote(run_attempt))
    attempts = ray.get(actor.get_train_run_attempts.remote())
    assert "test_run" in attempts
    assert "attempt_1" in attempts["test_run"]

    attempt = attempts["test_run"]["attempt_1"]
    assert attempt.status == RunAttemptStatus.PENDING
    assert attempt.start_time_ns == 1000
    assert attempt.resources == resources
    assert len(attempt.workers) == 0

    # Test update
    updated_attempt = run_attempt.copy(update={"status": RunAttemptStatus.RUNNING})
    ray.get(actor.create_or_update_train_run_attempt.remote(updated_attempt))
    attempts = ray.get(actor.get_train_run_attempts.remote())
    assert attempts["test_run"]["attempt_1"].status == RunAttemptStatus.RUNNING


def test_train_state_actor_abort_dead_controller_live_runs(monkeypatch):
    # Monkeypatch get_actor to return correct actor state per controller actor ID.
    def get_actor(actor_id: str, timeout: float):
        if actor_id == "nonexistent_controller_no_attempts_id":
            return None
        if actor_id in [
            "dead_controller_one_attempt_id",
            "dead_controller_two_attempts_id",
            "finished_controller_id",
        ]:
            return create_mock_actor_state(state="DEAD")
        if actor_id == "live_controller_one_attempt_id":
            return create_mock_actor_state(state="ALIVE")
        raise ValueError(f"Unknown actor {actor_id}.")

    monkeypatch.setattr("ray.train.v2._internal.state.util.get_actor", get_actor)
    monkeypatch.setattr("uuid.uuid4", lambda: MagicMock(hex="mock_uuid"))
    monkeypatch.setattr("time.time_ns", lambda: 1000)

    # Create TrainStateActor with interesting runs and run attempts.
    # NOTE: TrainStateActor will poll for real but its updates are idempotent.
    actor = TrainStateActor(enable_state_actor_reconciliation=True)
    finished_controller_run = create_mock_train_run(
        status=RunStatus.FINISHED,
        controller_actor_id="finished_controller_id",
        id="finished_controller_run_id",
    )
    live_controller_one_attempt_run = create_mock_train_run(
        status=RunStatus.RUNNING,
        controller_actor_id="live_controller_one_attempt_id",
        id="live_controller_one_attempt_run_id",
    )
    actor._runs = OrderedDict(
        {
            "nonexistent_controller_no_attempts_run_id": create_mock_train_run(
                status=RunStatus.INITIALIZING,
                controller_actor_id="nonexistent_controller_no_attempts_id",
                id="nonexistent_controller_no_attempts_run_id",
            ),
            "dead_controller_one_attempt_run_id": create_mock_train_run(
                status=RunStatus.INITIALIZING,
                controller_actor_id="dead_controller_one_attempt_id",
                id="dead_controller_one_attempt_run_id",
            ),
            "dead_controller_two_attempts_run_id": create_mock_train_run(
                status=RunStatus.SCHEDULING,
                controller_actor_id="dead_controller_two_attempts_id",
                id="dead_controller_two_attempts_run_id",
            ),
            "finished_controller_run_id": finished_controller_run,
            "live_controller_one_attempt_run_id": live_controller_one_attempt_run,
        }
    )
    live_controller_one_attempt_run_attempt = create_mock_train_run_attempt(
        status=RunAttemptStatus.RUNNING,
        run_id="live_controller_one_attempt_run_id",
        attempt_id="attempt_1",
    )
    dead_controller_two_attempts_first_attempt = (
        create_mock_train_run_attempt(
            attempt_id="attempt_1",
            status=RunAttemptStatus.ERRORED,
            run_id="dead_controller_two_attempts_run_id",
        ),
    )
    actor._run_attempts = {
        "nonexistent_controller_no_attempts_run_id": {},
        "dead_controller_one_attempt_run_id": {
            "attempt_1": create_mock_train_run_attempt(
                attempt_id="attempt_1",
                status=RunAttemptStatus.PENDING,
                run_id="dead_controller_one_attempt_run_id",
            ),
        },
        "dead_controller_two_attempts_run_id": OrderedDict(
            {
                "attempt_1": dead_controller_two_attempts_first_attempt,
                "attempt_2": create_mock_train_run_attempt(
                    status=RunAttemptStatus.RUNNING,
                    attempt_id="attempt_2",
                    run_id="dead_controller_two_attempts_run_id",
                ),
            }
        ),
        "finished_controller_run_id": {},
        "live_controller_one_attempt_run_id": {
            "attempt_1": live_controller_one_attempt_run_attempt,
        },
    }

    # Assert correct runs and run attempts get aborted.
    assert (
        actor._abort_live_runs_with_dead_controllers(
            "dead_controller_two_attempts_run_id"
        )
        == "dead_controller_two_attempts_run_id"
    )
    assert actor._runs == OrderedDict(
        {
            "nonexistent_controller_no_attempts_run_id": create_mock_train_run(
                status=RunStatus.ABORTED,
                controller_actor_id="nonexistent_controller_no_attempts_id",
                end_time_ns=1000,
                id="nonexistent_controller_no_attempts_run_id",
                status_detail=_DEAD_CONTROLLER_ABORT_STATUS_DETAIL,
            ),
            "dead_controller_one_attempt_run_id": create_mock_train_run(
                status=RunStatus.ABORTED,
                controller_actor_id="dead_controller_one_attempt_id",
                end_time_ns=1000,
                id="dead_controller_one_attempt_run_id",
                status_detail=_DEAD_CONTROLLER_ABORT_STATUS_DETAIL,
            ),
            "dead_controller_two_attempts_run_id": create_mock_train_run(
                status=RunStatus.ABORTED,
                controller_actor_id="dead_controller_two_attempts_id",
                end_time_ns=1000,
                id="dead_controller_two_attempts_run_id",
                status_detail=_DEAD_CONTROLLER_ABORT_STATUS_DETAIL,
            ),
            "finished_controller_run_id": finished_controller_run,
            "live_controller_one_attempt_run_id": live_controller_one_attempt_run,
        }
    )
    assert actor._run_attempts == {
        "nonexistent_controller_no_attempts_run_id": {},
        "dead_controller_one_attempt_run_id": {
            "attempt_1": create_mock_train_run_attempt(
                status=RunAttemptStatus.ABORTED,
                run_id="dead_controller_one_attempt_run_id",
                attempt_id="attempt_1",
                end_time_ns=1000,
                worker_status=ActorStatus.DEAD,
                status_detail=_DEAD_CONTROLLER_ABORT_STATUS_DETAIL,
            )
        },
        "dead_controller_two_attempts_run_id": OrderedDict(
            {
                "attempt_1": dead_controller_two_attempts_first_attempt,
                "attempt_2": create_mock_train_run_attempt(
                    status=RunAttemptStatus.ABORTED,
                    run_id="dead_controller_two_attempts_run_id",
                    attempt_id="attempt_2",
                    end_time_ns=1000,
                    worker_status=ActorStatus.DEAD,
                    status_detail=_DEAD_CONTROLLER_ABORT_STATUS_DETAIL,
                ),
            }
        ),
        "finished_controller_run_id": {},
        "live_controller_one_attempt_run_id": {
            "attempt_1": live_controller_one_attempt_run_attempt,
        },
    }


def test_train_state_manager_run_lifecycle(ray_start_regular):
    """Test the complete lifecycle of a training run through the state manager."""
    manager = TrainStateManager()

    # Test run creation with validation
    run_id = "test_run"
    manager.create_train_run(
        id=run_id,
        name="test",
        job_id="job_1",
        controller_actor_id="controller_1",
        controller_log_file_path="/tmp/ray/session_xxx/logs/train/ray-train-app-controller.log",
    )

    def get_run():
        state_actor = get_state_actor()
        runs = ray.get(state_actor.get_train_runs.remote())
        return runs[run_id]

    # Verify initial state
    run = get_run()
    assert run.status == RunStatus.INITIALIZING
    assert run.start_time_ns is not None
    assert run.end_time_ns is None

    # Test state transitions with timestamps
    state_transitions = [
        (manager.update_train_run_scheduling, RunStatus.SCHEDULING),
        (manager.update_train_run_running, RunStatus.RUNNING),
        (manager.update_train_run_finished, RunStatus.FINISHED),
    ]

    for update_fn, expected_status in state_transitions:
        update_fn(run_id)
        run = get_run()
        assert run.status == expected_status

        if expected_status == RunStatus.FINISHED:
            assert run.end_time_ns is not None
        else:
            assert run.end_time_ns is None


def test_train_state_manager_run_attempt_lifecycle(ray_start_regular):
    manager = TrainStateManager()

    # Create initial run
    manager.create_train_run(
        id="test_run",
        name="test",
        job_id="job_1",
        controller_actor_id="controller_1",
        controller_log_file_path="/tmp/ray/session_xxx/logs/train/ray-train-app-controller.log",
    )

    # Test attempt creation
    manager.create_train_run_attempt(
        run_id="test_run",
        attempt_id="attempt_1",
        num_workers=2,
        resources_per_worker={"CPU": 1},
    )

    state_actor = get_state_actor()
    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    assert "test_run" in attempts
    assert "attempt_1" in attempts["test_run"]
    attempt = attempts["test_run"]["attempt_1"]
    assert attempt.status == RunAttemptStatus.PENDING
    assert len(attempt.resources) == 2
    assert all(r.resources == {"CPU": 1} for r in attempt.resources)

    # Test running state with workers
    workers = [
        Worker(
            actor=get_mock_actor(f"actor_{i}"),
            metadata=MagicMock(
                node_id="node_1", node_ip="127.0.0.1", pid=1000 + i, gpu_ids=[]
            ),
            resources={"CPU": 1},
            distributed_context=MagicMock(world_rank=i, local_rank=i, node_rank=0),
            log_file_path="/tmp/ray/session_xxx/logs/train/ray-train-app-worker.log",
        )
        for i in range(2)
    ]

    manager.update_train_run_attempt_running(
        run_id="test_run",
        attempt_id="attempt_1",
        workers=workers,
    )

    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    attempt = attempts["test_run"]["attempt_1"]
    assert attempt.status == RunAttemptStatus.RUNNING
    assert len(attempt.workers) == 2
    assert all(w.status == ActorStatus.ALIVE for w in attempt.workers)

    # Test finished state
    manager.update_train_run_attempt_finished(
        run_id="test_run",
        attempt_id="attempt_1",
    )

    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    attempt = attempts["test_run"]["attempt_1"]
    assert attempt.status == RunAttemptStatus.FINISHED
    assert attempt.end_time_ns is not None
    assert len(attempt.workers) == 2
    assert all(w.status == ActorStatus.DEAD for w in attempt.workers)


def test_callback_controller_state_transitions(ray_start_regular, callback):
    states = [
        InitializingState(),
        SchedulingState(
            scaling_decision=ResizeDecision(num_workers=2, resources_per_worker={})
        ),
        RunningState(),
        RestartingState(
            training_failed_error=WorkerGroupError(error_message="", worker_failures={})
        ),
        SchedulingState(
            scaling_decision=ResizeDecision(num_workers=2, resources_per_worker={})
        ),
        RunningState(),
        ResizingState(
            scaling_decision=ResizeDecision(num_workers=4, resources_per_worker={})
        ),
        SchedulingState(
            scaling_decision=ResizeDecision(num_workers=4, resources_per_worker={})
        ),
        ReschedulingState(
            training_failed_error=ControllerError(WorkerGroupStartupTimeoutError(0))
        ),
        SchedulingState(
            scaling_decision=ResizeDecision(num_workers=2, resources_per_worker={})
        ),
        RunningState(),
        FinishedState(),
    ]
    expected_statuses = [
        RunStatus.INITIALIZING,
        RunStatus.SCHEDULING,
        RunStatus.RUNNING,
        RunStatus.RESTARTING,
        RunStatus.SCHEDULING,
        RunStatus.RUNNING,
        RunStatus.RESIZING,
        RunStatus.SCHEDULING,
        RunStatus.SCHEDULING,  # Rescheduling
        RunStatus.SCHEDULING,
        RunStatus.RUNNING,
        RunStatus.FINISHED,
    ]

    state_actor = get_state_actor()

    for i in range(len(states) - 1):
        callback.after_controller_state_update(states[i], states[i + 1])
        runs = ray.get(state_actor.get_train_runs.remote())
        run = runs[callback._run_id]
        assert run.status == expected_statuses[i + 1]


def test_callback_error_state_transition(ray_start_regular, callback):
    error_msg = "Test error"
    error_state = ErroredState(
        training_failed_error=ControllerError(Exception(error_msg))
    )
    callback.after_controller_state_update(RunningState(), error_state)

    state_actor = get_state_actor()
    runs = ray.get(state_actor.get_train_runs.remote())
    run = list(runs.values())[0]
    print(runs)
    assert run.status == RunStatus.ERRORED
    assert error_msg in run.status_detail
    assert run.end_time_ns is not None


def test_callback_aborted_with_worker_group_context(
    ray_start_regular, callback, mock_worker_group_context
):
    callback.before_worker_group_start(mock_worker_group_context)
    callback.before_worker_group_abort(mock_worker_group_context)
    state_actor = get_state_actor()
    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    attempt = list(attempts.values())[0]["attempt_1"]
    assert attempt.status == RunAttemptStatus.ABORTED


def test_callback_worker_group_lifecycle(
    ray_start_regular, callback, mock_worker_group, mock_worker_group_context
):
    """Test the complete lifecycle of a worker group through state callbacks."""
    state_actor = get_state_actor()

    def get_attempt():
        attempts = ray.get(state_actor.get_train_run_attempts.remote())
        return list(attempts.values())[0]["attempt_1"]

    # Test initialization
    callback.before_worker_group_start(mock_worker_group_context)
    attempt = get_attempt()
    assert attempt.status == RunAttemptStatus.PENDING
    assert len(attempt.resources) == mock_worker_group_context.num_workers
    assert all(
        r.resources == mock_worker_group_context.resources_per_worker
        for r in attempt.resources
    )

    # Test startup
    callback.after_worker_group_start(mock_worker_group)
    attempt = get_attempt()
    assert attempt.status == RunAttemptStatus.RUNNING
    assert len(attempt.workers) == len(
        mock_worker_group.get_worker_group_state().workers
    )
    for worker in attempt.workers:
        assert worker.status == ActorStatus.ALIVE
        assert (
            worker.resources.resources == mock_worker_group_context.resources_per_worker
        )

    # Test shutdown
    callback.before_worker_group_shutdown(mock_worker_group)
    attempt = get_attempt()
    assert attempt.status == RunAttemptStatus.FINISHED
    assert attempt.end_time_ns is not None


def test_callback_worker_group_error(
    ray_start_regular, callback, mock_worker_group, mock_worker_group_context
):
    state_actor = get_state_actor()

    callback.before_worker_group_start(mock_worker_group_context)
    callback.after_worker_group_start(mock_worker_group)

    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    attempt = list(attempts.values())[0]["attempt_1"]
    assert attempt.status == RunAttemptStatus.RUNNING
    assert len(attempt.workers) == 1
    assert attempt.workers[0].status == ActorStatus.ALIVE

    # Simulate error in worker group
    error_msg = "Test error"
    error_status = MagicMock()
    error_status.errors = [error_msg]
    error_status.get_error_string.return_value = error_msg
    mock_worker_group.get_latest_poll_status.return_value = error_status

    callback.before_worker_group_shutdown(mock_worker_group)

    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    attempt = list(attempts.values())[0]["attempt_1"]
    assert attempt.status == RunAttemptStatus.ERRORED
    assert attempt.status_detail == error_msg
    assert attempt.end_time_ns is not None
    assert len(attempt.workers) == 1
    assert attempt.workers[0].status == ActorStatus.DEAD


def test_callback_log_file_paths(
    ray_start_regular,
    monkeypatch,
    mock_worker_group_context,
    mock_worker,
):
    """Test that StateManagerCallback correctly captures and propagates log file paths."""

    # Mock the runtime context
    mock_runtime_context = MagicMock()
    mock_runtime_context.get_job_id.return_value = "test_job_id"
    mock_runtime_context.get_actor_id.return_value = "test_controller_id"
    monkeypatch.setattr(
        ray.runtime_context, "get_runtime_context", lambda: mock_runtime_context
    )

    # Mock the log path function
    expected_controller_log_path = (
        "/tmp/ray/session_xxx/logs/train/ray-train-app-controller.log"
    )
    monkeypatch.setattr(
        ray.train.v2._internal.callbacks.state_manager,
        "get_train_application_controller_log_path",
        lambda: expected_controller_log_path,
    )

    # Create the callback
    callback = StateManagerCallback()

    # Initialize the callback
    callback.after_controller_start(train_run_context=create_dummy_run_context())

    # Verify the log path was set in the state actor
    state_actor = get_state_actor()
    runs = ray.get(state_actor.get_train_runs.remote())
    run = runs[callback._run_id]
    assert run.controller_log_file_path == expected_controller_log_path

    # Now test worker log paths
    # Create a mock worker with a log file path
    mock_worker = mock_worker
    mock_worker.log_file_path = (
        "/tmp/ray/session_xxx/logs/train/ray-train-app-worker.log"
    )

    # Create a mock worker group
    mock_worker_group = MagicMock(spec=WorkerGroup)
    mock_worker_group.get_worker_group_context.return_value = mock_worker_group_context
    mock_worker_group.get_worker_group_state.return_value = MagicMock(
        workers=[mock_worker]
    )
    # mock_worker_group.get_latest_poll_status.return_value = None

    # Start the worker group
    callback.before_worker_group_start(mock_worker_group_context)
    callback.after_worker_group_start(mock_worker_group)

    # Verify the worker log path was set in the state actor
    attempts = ray.get(state_actor.get_train_run_attempts.remote())
    attempt = list(attempts.values())[0][mock_worker_group_context.run_attempt_id]
    assert len(attempt.workers) == 1
    assert attempt.workers[0].log_file_path == mock_worker.log_file_path


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-x", __file__]))
