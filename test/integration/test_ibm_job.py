# This code is part of Qiskit.
#
# (C) Copyright IBM 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""IBMJob Test."""
import copy
import time
from datetime import datetime, timedelta
from threading import Thread, Event
from unittest import SkipTest, mock
from unittest import skip

from dateutil import tz
from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
from qiskit.compiler import transpile
from qiskit.providers.jobstatus import JobStatus, JOB_FINAL_STATES
from qiskit.test.reference_circuits import ReferenceCircuits

from qiskit import IBMQ
from qiskit_ibm_provider import IBMBackend
from qiskit_ibm_provider.api.exceptions import RequestsApiError
from qiskit_ibm_provider.api.rest.job import Job as RestJob
from qiskit_ibm_provider.exceptions import IBMBackendApiError
from qiskit_ibm_provider.job.exceptions import IBMJobTimeoutError
from ..decorators import (
    IntegrationTestDependencies,
    integration_test_setup_with_backend,
)
from ..fake_account_client import BaseFakeAccountClient, CancelableFakeJob
from ..ibm_test_case import IBMTestCase
from ..utils import (
    most_busy_backend,
    cancel_job,
    submit_and_cancel,
)


class TestIBMJob(IBMTestCase):
    """Test ibm_job module."""

    @classmethod
    @integration_test_setup_with_backend(simulator=False, min_num_qubits=2)
    def setUpClass(
        cls, backend: IBMBackend, dependencies: IntegrationTestDependencies
    ) -> None:
        """Initial class level setup."""
        # pylint: disable=arguments-differ
        super().setUpClass()
        cls.provider = dependencies.provider
        cls.sim_backend = dependencies.provider.get_backend(
            "ibmq_qasm_simulator", instance=dependencies.instance
        )
        cls.real_device_backend = backend
        cls.dependencies = dependencies
        cls.bell = transpile(ReferenceCircuits.bell(), cls.sim_backend)
        cls.sim_job = cls.sim_backend.run(cls.bell)
        cls.last_month = datetime.now() - timedelta(days=30)
        cls.legacy_provider = IBMQ.enable_account(dependencies.token)

    def test_run_multiple_simulator(self):
        """Test running multiple jobs in a simulator."""
        num_qubits = 16
        quantum_register = QuantumRegister(num_qubits, "qr")
        classical_register = ClassicalRegister(num_qubits, "cr")
        quantum_circuit = QuantumCircuit(quantum_register, classical_register)
        for i in range(num_qubits - 1):
            quantum_circuit.cx(quantum_register[i], quantum_register[i + 1])
        quantum_circuit.measure(quantum_register, classical_register)
        num_jobs = 4
        job_array = [
            self.sim_backend.run(transpile([quantum_circuit] * 20), shots=2048)
            for _ in range(num_jobs)
        ]
        timeout = 30
        start_time = time.time()
        while True:
            check = sum(job.status() is JobStatus.RUNNING for job in job_array)
            if check >= 2:
                self.log.info("found %d simultaneous jobs", check)
                break
            if all((job.status() is JobStatus.DONE for job in job_array)):
                # done too soon? don't generate error
                self.log.warning(
                    "all jobs completed before simultaneous jobs could be detected"
                )
                break
            for job in job_array:
                self.log.info(
                    "%s %s %s %s",
                    job.status(),
                    job.status() is JobStatus.RUNNING,
                    check,
                    job.job_id(),
                )
            self.log.info("-  %s", str(time.time() - start_time))
            if (
                time.time() - start_time > timeout
                and self.sim_backend.status().pending_jobs <= 4
            ):
                raise TimeoutError(
                    "Failed to see multiple running jobs after "
                    "{0} seconds.".format(timeout)
                )
            time.sleep(0.2)

        result_array = [job.result() for job in job_array]
        self.log.info("got back all job results")
        # Ensure all jobs have finished.
        self.assertTrue(all((job.status() is JobStatus.DONE for job in job_array)))
        self.assertTrue(all((result.success for result in result_array)))

        # Ensure job ids are unique.
        job_ids = [job.job_id() for job in job_array]
        self.assertEqual(sorted(job_ids), sorted(list(set(job_ids))))

    def test_cancel(self):
        """Test job cancellation."""
        # Find the most busy backend
        backend = most_busy_backend(self.provider, instance=self.dependencies.instance)
        submit_and_cancel(backend)

    def test_retrieve_jobs(self):
        """Test retrieving jobs."""
        job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name,
            limit=5,
            skip=0,
            start_datetime=self.last_month,
        )
        self.assertLessEqual(len(job_list), 5)
        for job in job_list:
            self.assertTrue(isinstance(job.job_id(), str))

    def test_retrieve_legacy_jobs(self):
        """Test retrieving legacy jobs."""
        # run jobs from qiskit_ibmq_provider
        backend_name = "ibmq_qasm_simulator"
        backend = self.legacy_provider.get_backend(backend_name)
        job1 = backend.run(ReferenceCircuits.bell())
        job2 = backend.run(ReferenceCircuits.bell())
        self.assertTrue(job1.result())
        self.assertTrue(job2.result())

        job_list = self.provider.backend.jobs(
            backend_name=backend_name,
            limit=5,
            skip=0,
        )
        job_ids = [job.job_id() for job in job_list]
        self.assertTrue(job1.job_id() in job_ids)
        self.assertTrue(job2.job_id() in job_ids)

    def test_retrieve_single_legacy_job(self):
        """Test retrieving a single legacy job."""
        # run job from qiskit_ibmq_provider
        backend = self.legacy_provider.get_backend("ibmq_qasm_simulator")
        job = backend.run(ReferenceCircuits.bell())

        retrieved_job = self.provider.backend.retrieve_job(job.job_id())
        self.assertEqual(job.job_id(), retrieved_job.job_id())
        self.assertTrue(job.result())

    def test_retrieve_jobs_with_status(self):
        """Test retreiving jobs with status filter."""
        statuses = [["DONE"], JobStatus.DONE, [JobStatus.DONE]]
        job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name, limit=3, status="DONE"
        )
        for status in statuses:
            new_list = self.provider.backend.jobs(
                backend_name=self.sim_backend.name, limit=3, status=status
            )
            self.assertEqual(
                [job.job_id() for job in job_list], [job.job_id() for job in new_list]
            )

    def test_retrieve_completed_jobs(self):
        """Test retrieving jobs with the completed filter."""
        completed_job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name, limit=3, status="completed"
        )
        for job in completed_job_list:
            self.assertTrue(
                job.status() in [JobStatus.DONE, JobStatus.CANCELLED, JobStatus.ERROR]
            )

    def test_retrieve_pending_jobs(self):
        """Test retrieving jobs with the pending filter."""
        pending_job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name, limit=3, status="pending"
        )
        for job in pending_job_list:
            self.assertTrue(job.status() in [JobStatus.QUEUED, JobStatus.RUNNING])

    def test_retrieve_running_error_jobs(self):
        """Test client side filtering with running and error jobs."""
        statuses = ["RUNNING", JobStatus.ERROR]
        job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name, limit=3, status=statuses
        )
        for job in job_list:
            self.assertTrue(job.status() in [JobStatus.RUNNING, JobStatus.ERROR])

    def test_retrieve_queued_cancelled_jobs(self):
        """Test client side filtering with queued and cancelled jobs."""
        statuses = ["QUEUED", "CANCELLED"]
        job_list = self.provider.backend.jobs(limit=3, status=statuses)
        for job in job_list:
            self.assertTrue(job.status() in [JobStatus.QUEUED, JobStatus.CANCELLED])

    def test_retrieve_job(self):
        """Test retrieving a single job."""
        retrieved_job = self.provider.backend.retrieve_job(self.sim_job.job_id())
        self.assertEqual(self.sim_job.job_id(), retrieved_job.job_id())
        self.assertEqual(self.sim_job.circuits(), retrieved_job.circuits())
        self.assertEqual(
            self.sim_job.result().get_counts(), retrieved_job.result().get_counts()
        )

    def test_retrieve_job_uses_appropriate_backend(self):
        """Test that retrieved jobs come from their appropriate backend."""
        backend_1 = self.real_device_backend
        # Get a second backend.
        backend_2 = None
        provider = self.real_device_backend.provider
        for my_backend in provider.backends():
            if my_backend.status().operational and my_backend.name != backend_1.name:
                backend_2 = my_backend
                break
        if not backend_2:
            raise SkipTest("Skipping test that requires multiple backends")

        job_1 = backend_1.run(transpile(ReferenceCircuits.bell()))
        job_2 = backend_2.run(transpile(ReferenceCircuits.bell()))

        # test a retrieved job's backend is the same as the queried backend
        self.assertEqual(
            provider.backend.retrieve_job(job_1.job_id()).backend().name, backend_1.name
        )
        self.assertEqual(
            provider.backend.retrieve_job(job_2.job_id()).backend().name, backend_2.name
        )

        # Cleanup
        for job in [job_1, job_2]:
            cancel_job(job)

    def test_retrieve_job_error(self):
        """Test retrieving an invalid job."""
        self.assertRaises(
            IBMBackendApiError, self.provider.backend.retrieve_job, "BAD_JOB_ID"
        )

    def test_retrieve_jobs_status(self):
        """Test retrieving jobs filtered by status."""
        backend_jobs = self.provider.backend.jobs(
            backend_name=self.sim_backend.name,
            limit=5,
            skip=5,
            status="completed",
            start_datetime=self.last_month,
        )
        self.assertTrue(backend_jobs)

        for job in backend_jobs:
            self.assertTrue(
                job.status() in JOB_FINAL_STATES,
                "Job {} has status {} when it should be DONE, CANCELLED, or ERROR".format(
                    job.job_id(), job.status()
                ),
            )

    def test_retrieve_jobs_start_datetime(self):
        """Test retrieving jobs created after a specified datetime."""
        past_month = datetime.now() - timedelta(days=30)
        # Add local tz in order to compare to `creation_date` which is tz aware.
        past_month_tz_aware = past_month.replace(tzinfo=tz.tzlocal())

        job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name,
            limit=2,
            start_datetime=past_month,
        )
        self.assertTrue(job_list)
        for job in job_list:
            self.assertGreaterEqual(
                job.creation_date(),
                past_month_tz_aware,
                "job {} creation date {} not within range".format(
                    job.job_id(), job.creation_date()
                ),
            )

    def test_retrieve_jobs_end_datetime(self):
        """Test retrieving jobs created before a specified datetime."""
        past_month = datetime.now() - timedelta(days=30)
        # Add local tz in order to compare to `creation_date` which is tz aware.
        past_month_tz_aware = past_month.replace(tzinfo=tz.tzlocal())

        job_list = self.provider.backend.jobs(
            backend_name=self.sim_backend.name,
            limit=2,
            end_datetime=past_month,
        )
        self.assertTrue(job_list)
        for job in job_list:
            self.assertLessEqual(
                job.creation_date(),
                past_month_tz_aware,
                "job {} creation date {} not within range".format(
                    job.job_id(), job.creation_date()
                ),
            )

    def test_retrieve_jobs_between_datetimes(self):
        """Test retrieving jobs created between two specified datetimes."""
        date_today = datetime.now()
        past_month = date_today - timedelta(30)
        past_two_month = date_today - timedelta(60)

        # Add local tz in order to compare to `creation_date` which is tz aware.
        past_month_tz_aware = past_month.replace(tzinfo=tz.tzlocal())
        past_two_month_tz_aware = past_two_month.replace(tzinfo=tz.tzlocal())

        with self.subTest():
            job_list = self.provider.backend.jobs(
                backend_name=self.sim_backend.name,
                limit=2,
                start_datetime=past_two_month,
                end_datetime=past_month,
            )
            self.assertTrue(job_list)
            for job in job_list:
                self.assertTrue(
                    (
                        past_two_month_tz_aware
                        <= job.creation_date()
                        <= past_month_tz_aware
                    ),
                    "job {} creation date {} not within range".format(
                        job.job_id(), job.creation_date()
                    ),
                )

    def test_retrieve_jobs_order(self):
        """Test retrieving jobs with different orders."""
        job = self.sim_backend.run(self.bell)
        job.wait_for_final_state()
        newest_jobs = self.provider.backend.jobs(
            limit=10,
            status="completed",
            descending=True,
            start_datetime=self.last_month,
        )
        self.assertIn(job.job_id(), [rjob.job_id() for rjob in newest_jobs])

        oldest_jobs = self.provider.backend.jobs(
            limit=10,
            status="completed",
            descending=False,
            start_datetime=self.last_month,
        )
        self.assertNotIn(job.job_id(), [rjob.job_id() for rjob in oldest_jobs])

    def test_refresh_job_result(self):
        """Test re-retrieving job result via refresh."""
        result = self.sim_job.result()

        # Save original cached results.
        cached_result = copy.deepcopy(result.to_dict())
        self.assertTrue(cached_result)

        # Modify cached results.
        result.results[0].header.name = "modified_result"
        self.assertNotEqual(cached_result, result.to_dict())
        self.assertEqual(result.results[0].header.name, "modified_result")

        # Re-retrieve result via refresh.
        result = self.sim_job.result(refresh=True)
        self.assertDictEqual(cached_result, result.to_dict())
        self.assertNotEqual(result.results[0].header.name, "modified_result")

    @skip("TODO update test case")
    def test_wait_for_final_state(self):
        """Test waiting for job to reach final state."""

        def final_state_callback(c_job_id, c_status, c_job, **kwargs):
            """Job status query callback function."""
            self.assertEqual(c_job_id, job.job_id())
            self.assertNotIn(c_status, JOB_FINAL_STATES)
            self.assertEqual(c_job.job_id(), job.job_id())
            self.assertIn("queue_info", kwargs)

            queue_info = kwargs.pop("queue_info", None)
            callback_info["called"] = True

            if wait_time is None:
                # Look for status change.
                data = {"status": c_status, "queue_info": queue_info}
                self.assertNotEqual(data, callback_info["last data"])
                callback_info["last data"] = data
            else:
                # Check called within wait time.
                if (
                    callback_info["last call time"]
                    and job._status not in JOB_FINAL_STATES
                ):
                    self.assertAlmostEqual(
                        time.time() - callback_info["last call time"],
                        wait_time,
                        delta=0.2,
                    )
                callback_info["last call time"] = time.time()

        def job_canceller(job_, exit_event, wait):
            exit_event.wait(wait)
            cancel_job(job_)

        wait_args = [2, None]

        saved_api = self.sim_backend._api_client
        try:
            self.sim_backend._api_client = BaseFakeAccountClient(
                job_class=CancelableFakeJob
            )
            for wait_time in wait_args:
                with self.subTest(wait_time=wait_time):
                    # Put callback data in a dictionary to make it mutable.
                    callback_info = {
                        "called": False,
                        "last call time": 0.0,
                        "last data": {},
                    }
                    cancel_event = Event()
                    job = self.sim_backend.run(self.bell)
                    # Cancel the job after a while.
                    Thread(
                        target=job_canceller, args=(job, cancel_event, 7), daemon=True
                    ).start()
                    try:
                        job.wait_for_final_state(
                            timeout=10, wait=wait_time, callback=final_state_callback
                        )
                        self.assertTrue(job.in_final_state())
                        self.assertTrue(callback_info["called"])
                        cancel_event.set()
                    finally:
                        # Ensure all threads ended.
                        for thread in job._executor._threads:
                            thread.join(0.1)
        finally:
            self.sim_backend._api_client = saved_api

    def test_wait_for_final_state_timeout(self):
        """Test waiting for job to reach final state times out."""
        backend = most_busy_backend(self.provider, instance=self.dependencies.instance)
        job = backend.run(transpile(ReferenceCircuits.bell(), backend=backend))
        try:
            self.assertRaises(IBMJobTimeoutError, job.wait_for_final_state, timeout=0.1)
        finally:
            # Ensure all threads ended.
            for thread in job._executor._threads:
                thread.join(0.1)
            cancel_job(job)

    @skip("not supported by api")
    def test_job_submit_partial_fail(self):
        """Test job submit partial fail."""
        job_id = []

        def _side_effect(self, *args, **kwargs):
            # pylint: disable=unused-argument
            job_id.append(self.job_id)
            raise RequestsApiError("Kaboom")

        fail_points = ["put_object_storage", "callback_upload"]

        for fail_method in fail_points:
            with self.subTest(fail_method=fail_method):
                with mock.patch.object(
                    RestJob, fail_method, side_effect=_side_effect, autospec=True
                ):
                    with self.assertRaises(IBMBackendApiError):
                        self.sim_backend.run(self.bell)

                self.assertTrue(job_id, "Job ID not saved.")
                job = self.provider.backend.retrieve_job(job_id[0])
                self.assertEqual(
                    job.status(),
                    JobStatus.CANCELLED,
                    f"Job {job.job_id()} status is {job.status()} and not cancelled!",
                )

    def test_job_circuits(self):
        """Test job circuits."""
        self.assertEqual(str(self.bell), str(self.sim_job.circuits()[0]))

    def test_job_backend_options(self):
        """Test job backend options."""
        run_config = {"shots": 2048, "memory": True}
        job = self.sim_backend.run(self.bell, **run_config)
        self.assertLessEqual(run_config.items(), job.backend_options().items())

    def test_job_header(self):
        """Test job header."""
        custom_header = {"test": "test_job_header"}
        job = self.sim_backend.run(self.bell, header=custom_header)
        self.assertEqual(custom_header["test"], job.header()["test"])
        self.assertLessEqual(custom_header.items(), job.header().items())
