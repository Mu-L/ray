"""Client tests that run their own init (as with init_and_serve) live here"""

import time
import random
import sys
import subprocess
from unittest.mock import patch

import pytest

import ray.util.client.server.server as ray_client_server
import ray.core.generated.ray_client_pb2 as ray_client_pb2

from ray.util.client import _ClientContext
from ray.cluster_utils import cluster_not_supported

import ray


@ray.remote
def hello_world():
    c1 = complex_task.remote(random.randint(1, 10))
    c2 = complex_task.remote(random.randint(1, 10))
    return sum(ray.get([c1, c2]))


@ray.remote
def complex_task(value):
    time.sleep(1)
    return value * 10


@ray.remote
class C:
    def __init__(self, x):
        self.val = x

    def double(self):
        self.val += self.val

    def get(self):
        return self.val


@pytest.mark.xfail(cluster_not_supported, reason="cluster not supported")
@pytest.fixture
def init_and_serve_lazy(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=1, num_gpus=0)
    cluster.wait_for_nodes(1)
    address = cluster.address

    def connect(job_config=None, **ray_init_kwargs):
        ray.init(address=address, job_config=job_config, **ray_init_kwargs)

    server_handle = ray_client_server.serve("localhost:50051", connect)
    yield server_handle
    ray_client_server.shutdown_with_server(server_handle.grpc_server)


def test_validate_port():
    """Check that ports outside of 1024-65535 are rejected."""
    for port in [1000, 1023, 65536, 700000]:
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            subprocess.check_output(
                [
                    "ray",
                    "start",
                    "--head",
                    "--num-cpus",
                    "8",
                    "--ray-client-server-port",
                    f"{port}",
                ]
            )
            assert "ValueError" in str(excinfo.traceback)
            assert "65535" in str(excinfo.traceback)


def test_basic_preregister(init_and_serve):
    """Tests conversion of Ray actors and remote functions to client actors
    and client remote functions.

    Checks that the conversion works when disconnecting and reconnecting client
    sessions.
    """
    from ray.util.client import ray

    for _ in range(2):
        ray.connect("localhost:50051")
        val = ray.get(hello_world.remote())
        print(val)
        assert val >= 20
        assert val <= 200
        c = C.remote(3)
        x = c.double.remote()
        y = c.double.remote()
        ray.wait([x, y])
        val = ray.get(c.get.remote())
        assert val == 12
        ray.disconnect()


def test_idempotent_disconnect(init_and_serve):
    from ray.util.client import ray

    ray.disconnect()
    ray.disconnect()
    ray.connect("localhost:50051")
    ray.disconnect()
    ray.disconnect()


def test_num_clients(init_and_serve_lazy):
    # Tests num clients reporting; useful if you want to build an app that
    # load balances clients between Ray client servers.

    def get_job_id(api):
        return api.get_runtime_context().worker.current_job_id

    api1 = _ClientContext()
    info1 = api1.connect("localhost:50051")
    job_id_1 = get_job_id(api1)
    assert info1["num_clients"] == 1, info1
    api2 = _ClientContext()
    info2 = api2.connect("localhost:50051")
    job_id_2 = get_job_id(api2)
    assert info2["num_clients"] == 2, info2

    assert job_id_1 == job_id_2

    # Disconnect the first two clients.
    api1.disconnect()
    api2.disconnect()
    time.sleep(1)

    api3 = _ClientContext()
    info3 = api3.connect("localhost:50051")
    job_id_3 = get_job_id(api3)
    assert info3["num_clients"] == 1, info3
    assert job_id_1 != job_id_3

    # Check info contains ray and python version.
    assert isinstance(info3["ray_version"], str), info3
    assert isinstance(info3["ray_commit"], str), info3
    assert isinstance(info3["python_version"], str), info3
    api3.disconnect()


def test_python_version(init_and_serve):
    server_handle = init_and_serve
    ray = _ClientContext()
    info1 = ray.connect("localhost:50051")
    assert info1["python_version"] == ".".join(
        [str(x) for x in list(sys.version_info)[:3]]
    )
    ray.disconnect()
    time.sleep(1)

    def mock_connection_response():
        return ray_client_pb2.ConnectionInfoResponse(
            num_clients=1,
            python_version="2.7.12",
            ray_version="",
            ray_commit="",
        )

    # inject mock connection function
    server_handle.data_servicer._build_connection_response = mock_connection_response

    ray = _ClientContext()
    with pytest.raises(RuntimeError):
        _ = ray.connect("localhost:50051")

    ray = _ClientContext()
    info3 = ray.connect("localhost:50051", ignore_version=True)
    assert info3["num_clients"] == 1, info3
    ray.disconnect()


@patch("ray.util.client.server.dataservicer.CLIENT_SERVER_MAX_THREADS", 6)
@patch("ray.util.client.server.logservicer.CLIENT_SERVER_MAX_THREADS", 6)
def test_max_clients(init_and_serve):
    # Tests max clients. Should support up to CLIENT_SERVER_MAX_THREADS / 2.
    def get_job_id(api):
        return api.get_runtime_context().worker.current_job_id

    for i in range(3):
        api1 = _ClientContext()
        info1 = api1.connect("localhost:50051")

        assert info1["num_clients"] == i + 1, info1

    with pytest.raises(ConnectionError):
        api = _ClientContext()
        _ = api.connect("localhost:50051")


if __name__ == "__main__":
    sys.exit(pytest.main(["-sv", __file__]))
