# This file is part of cloud-init. See LICENSE file for license information.
import datetime
import logging
import os
import pytest
import sys
from tarfile import TarFile
from contextlib import contextmanager
from pathlib import Path

from tests.integration_tests import integration_settings
from tests.integration_tests.clouds import (
    AzureCloud,
    Ec2Cloud,
    GceCloud,
    ImageSpecification,
    LxdContainerCloud,
    LxdVmCloud,
    OciCloud,
)
from tests.integration_tests.instances import IntegrationInstance


log = logging.getLogger('integration_testing')
log.addHandler(logging.StreamHandler(sys.stdout))
log.setLevel(logging.INFO)

platforms = {
    'ec2': Ec2Cloud,
    'gce': GceCloud,
    'azure': AzureCloud,
    'oci': OciCloud,
    'lxd_container': LxdContainerCloud,
    'lxd_vm': LxdVmCloud,
}
os_list = ["ubuntu"]

session_start_time = datetime.datetime.now().strftime('%y%m%d%H%M%S')


def pytest_runtest_setup(item):
    """Skip tests on unsupported clouds.

    A test can take any number of marks to specify the platforms it can
    run on. If a platform(s) is specified and we're not running on that
    platform, then skip the test. If platform specific marks are not
    specified, then we assume the test can be run anywhere.
    """
    all_platforms = platforms.keys()
    test_marks = [mark.name for mark in item.iter_markers()]
    supported_platforms = set(all_platforms).intersection(test_marks)
    current_platform = integration_settings.PLATFORM
    unsupported_message = 'Cannot run on platform {}'.format(current_platform)
    if 'no_container' in test_marks:
        if 'lxd_container' in test_marks:
            raise Exception(
                'lxd_container and no_container marks simultaneously set '
                'on test'
            )
        if current_platform == 'lxd_container':
            pytest.skip(unsupported_message)
    if supported_platforms and current_platform not in supported_platforms:
        pytest.skip(unsupported_message)

    image = ImageSpecification.from_os_image()
    current_os = image.os
    supported_os_set = set(os_list).intersection(test_marks)
    if current_os and supported_os_set and current_os not in supported_os_set:
        pytest.skip("Cannot run on OS {}".format(current_os))


# disable_subp_usage is defined at a higher level, but we don't
# want it applied here
@pytest.fixture()
def disable_subp_usage(request):
    pass


@pytest.yield_fixture(scope='session')
def session_cloud():
    if integration_settings.PLATFORM not in platforms.keys():
        raise ValueError(
            "{} is an invalid PLATFORM specified in settings. "
            "Must be one of {}".format(
                integration_settings.PLATFORM, list(platforms.keys())
            )
        )

    cloud = platforms[integration_settings.PLATFORM]()
    cloud.emit_settings_to_log()
    yield cloud
    try:
        cloud.delete_snapshot()
    finally:
        cloud.destroy()


@pytest.fixture(scope='session', autouse=True)
def setup_image(session_cloud):
    """Setup the target environment with the correct version of cloud-init.

    So we can launch instances / run tests with the correct image
    """
    client = None
    log.info('Setting up environment for %s', session_cloud.datasource)
    if integration_settings.CLOUD_INIT_SOURCE == 'NONE':
        pass  # that was easy
    elif integration_settings.CLOUD_INIT_SOURCE == 'IN_PLACE':
        if session_cloud.datasource not in ['lxd_container', 'lxd_vm']:
            raise ValueError(
                'IN_PLACE as CLOUD_INIT_SOURCE only works for LXD')
        # The mount needs to happen after the instance is created, so
        # no further action needed here
    elif integration_settings.CLOUD_INIT_SOURCE == 'PROPOSED':
        client = session_cloud.launch()
        client.install_proposed_image()
    elif integration_settings.CLOUD_INIT_SOURCE.startswith('ppa:'):
        client = session_cloud.launch()
        client.install_ppa(integration_settings.CLOUD_INIT_SOURCE)
    elif os.path.isfile(str(integration_settings.CLOUD_INIT_SOURCE)):
        client = session_cloud.launch()
        client.install_deb()
    else:
        raise ValueError(
            'Invalid value for CLOUD_INIT_SOURCE setting: {}'.format(
                integration_settings.CLOUD_INIT_SOURCE))
    if client:
        # Even if we're keeping instances, we don't want to keep this
        # one around as it was just for image creation
        client.destroy()
    log.info('Done with environment setup')


def _collect_logs(instance: IntegrationInstance, node_id: str,
                  test_failed: bool):
    """Collect logs from remote instance.

    Args:
        instance: The current IntegrationInstance to collect logs from
        node_id: The pytest representation of this test, E.g.:
            tests/integration_tests/test_example.py::TestExample.test_example
        test_failed: If test failed or not
    """
    if any([
        integration_settings.COLLECT_LOGS == 'NEVER',
        integration_settings.COLLECT_LOGS == 'ON_ERROR' and not test_failed
    ]):
        return
    instance.execute(
        'cloud-init collect-logs -u -t /var/tmp/cloud-init.tar.gz')
    node_id_path = Path(
        node_id
        .replace('.py', '')  # Having a directory with '.py' would be weird
        .replace('::', os.path.sep)  # Turn classes/tests into paths
        .replace('[', '-')  # For parametrized names
        .replace(']', '')  # For parameterized names
    )
    log_dir = Path(
        integration_settings.LOCAL_LOG_PATH
    ) / session_start_time / node_id_path
    if not log_dir.exists():
        log_dir.mkdir(parents=True)
    tarball_path = log_dir / 'cloud-init.tar.gz'
    instance.pull_file('/var/tmp/cloud-init.tar.gz', tarball_path)

    tarball = TarFile.open(str(tarball_path))
    tarball.extractall(path=str(log_dir))
    tarball_path.unlink()


@contextmanager
def _client(request, fixture_utils, session_cloud):
    """Fixture implementation for the client fixtures.

    Launch the dynamic IntegrationClient instance using any provided
    userdata, yield to the test, then cleanup
    """
    user_data = fixture_utils.closest_marker_first_arg_or(
        request, 'user_data', None)
    name = fixture_utils.closest_marker_first_arg_or(
        request, 'instance_name', None
    )
    launch_kwargs = {}
    if name is not None:
        launch_kwargs = {"name": name}
    with session_cloud.launch(
        user_data=user_data, launch_kwargs=launch_kwargs
    ) as instance:
        previous_failures = request.session.testsfailed
        yield instance
        test_failed = request.session.testsfailed - previous_failures > 0
        _collect_logs(instance, request.node.nodeid, test_failed)


@pytest.yield_fixture
def client(request, fixture_utils, session_cloud):
    """Provide a client that runs for every test."""
    with _client(request, fixture_utils, session_cloud) as client:
        yield client


@pytest.yield_fixture(scope='module')
def module_client(request, fixture_utils, session_cloud):
    """Provide a client that runs once per module."""
    with _client(request, fixture_utils, session_cloud) as client:
        yield client


@pytest.yield_fixture(scope='class')
def class_client(request, fixture_utils, session_cloud):
    """Provide a client that runs once per class."""
    with _client(request, fixture_utils, session_cloud) as client:
        yield client


def pytest_assertrepr_compare(op, left, right):
    """Custom integration test assertion explanations.

    See
    https://docs.pytest.org/en/stable/assert.html#defining-your-own-explanation-for-failed-assertions
    for pytest's documentation.
    """
    if op == "not in" and isinstance(left, str) and isinstance(right, str):
        # This stanza emits an improved assertion message if we're testing for
        # the presence of a string within a cloud-init log: it will report only
        # the specific lines containing the string (instead of the full log,
        # the default behaviour).
        potential_log_lines = right.splitlines()
        first_line = potential_log_lines[0]
        if "DEBUG" in first_line and "Cloud-init" in first_line:
            # We are looking at a cloud-init log, so just pick out the relevant
            # lines
            found_lines = [
                line for line in potential_log_lines if left in line
            ]
            return [
                '"{}" not in cloud-init.log string; unexpectedly found on'
                " these lines:".format(left)
            ] + found_lines
