"""tools/jobs.py in isolation: progress maths, cancellation, failure handling, ordering, restart recovery."""
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools import jobs as jobs_module
from tools.jobs import JobCancelled, JobFailed, JobManager, JobOutcome


class JobTestBase(unittest.TestCase):
    inline = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-jobs-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "jobs.sqlite3"
        self.logs = []

        def connect():
            conn = sqlite3.connect(self.db, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            return conn

        self.manager = JobManager(connect, lambda: self.inline, log=self.logs.append)
        conn = connect()
        self.manager.init_schema(conn)
        conn.close()
        self.connect = connect


class InlineJobTests(JobTestBase):
    def test_success_records_outcome(self):
        job_id = self.manager.submit("t", "Title", lambda p: JobOutcome("all good", "success", "addon_detail", {"addon_id": 7}, True))
        job = self.manager.get(job_id)
        self.assertEqual((job["state"], job["message"], job["level"], job["percent"]), ("done", "all good", "success", 100.0))
        self.assertEqual((job["next_endpoint"], job["next_values"], job["next_auto"]), ("addon_detail", {"addon_id": 7}, 1))
        self.assertIsNotNone(job["started_at"])
        self.assertGreaterEqual(job["finished_at"], job["started_at"])

    def test_known_failures_become_failed_jobs_with_the_message(self):
        for exc in (ValueError("bad zip"), OSError("disk full"), sqlite3.IntegrityError("duplicate addon id")):
            with self.subTest(exc=repr(exc)):
                def boom(p, e=exc):
                    raise e
                job = self.manager.get(self.manager.submit("t", "T", boom))
                self.assertEqual((job["state"], job["message"], job["level"]), ("failed", str(exc), "error"))

    def test_job_failed_carries_the_next_page(self):
        def boom(p):
            raise JobFailed("nope", "import_plan", {"token": "abc"})
        job = self.manager.get(self.manager.submit("t", "T", boom))
        self.assertEqual((job["state"], job["message"], job["next_endpoint"], job["next_values"]), ("failed", "nope", "import_plan", {"token": "abc"}))

    def test_unexpected_errors_never_escape_and_are_logged(self):
        def boom(p):
            raise KeyError("surprise")
        job_id = self.manager.submit("t", "T", boom)                     # must not raise
        job = self.manager.get(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Unexpected error: KeyError", job["message"])
        self.assertTrue(any("Traceback" in line and "surprise" in line for line in self.logs))

    def test_cancel_inside_the_job(self):
        def cancel_self(p):
            p("Working", 1, 10)
            self.manager.request_cancel(p.job_id)
            p("Working", 2, 10)                                           # next progress call is the cancellation point
            self.fail("not reached")
        job = self.manager.get(self.manager.submit("t", "T", cancel_self))
        self.assertEqual((job["state"], job["level"]), ("cancelled", "info"))
        self.assertIn("Nothing was changed", job["message"])

    def test_progress_percent_follows_stage_weights_and_never_decreases(self):
        seen = []

        def work(p):
            p.plan([("a", 1), ("b", 3)])
            for stage, done, total in (("a", 0, 1), ("a", 1, 1), ("b", 1, 2), ("b", 2, 2)):
                p(stage, done, total)
                seen.append((stage, done, self.manager.get(p.job_id)["percent"]))
            p("a", 1, 2)                                                   # going back to an earlier stage must not move the bar back
            seen.append(("back", 0, self.manager.get(p.job_id)["percent"]))
            return JobOutcome("ok")
        self.manager.submit("t", "T", work)
        self.assertEqual(seen, [("a", 0, 0.0), ("a", 1, 25.0), ("b", 1, 62.5), ("b", 2, 100.0), ("back", 0, 100.0)])

    def test_unplanned_stage_keeps_the_bar_where_it_was(self):
        out = []

        def work(p):
            p.plan([("a", 1)])
            p("a", 1, 1)
            p("surprise", 5, 10)
            out.append(self.manager.get(p.job_id)["percent"])
            return JobOutcome("ok")
        self.manager.submit("t", "T", work)
        self.assertEqual(out, [100.0])

    def test_progress_writes_are_throttled_but_stage_ends_are_always_written(self):
        counts = []
        real = self.manager._update

        def counting(job_id, **fields):
            if "done" in fields:
                counts.append(fields["done"])
            real(job_id, **fields)
        self.manager._update = counting

        def work(p):
            p.plan([("s", 1)])
            for i in range(0, 1001):
                p("s", i, 1000)
            return JobOutcome("ok")
        self.manager.submit("t", "T", work)
        self.assertLess(len(counts), 50)
        self.assertEqual(counts[-1], 1000)

    def test_finished_jobs_are_pruned_but_active_ones_kept(self):
        old = jobs_module.KEEP_FINISHED_JOBS
        jobs_module.KEEP_FINISHED_JOBS = 3
        self.addCleanup(setattr, jobs_module, "KEEP_FINISHED_JOBS", old)
        ids = [self.manager.submit("t", f"T{i}", lambda p: JobOutcome("ok")) for i in range(6)]
        self.manager.submit("t", "last", lambda p: JobOutcome("ok"))
        conn = self.connect()
        remaining = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
        conn.close()
        self.assertLessEqual(len(remaining), 4)
        self.assertNotIn(ids[0], remaining)

    def test_restart_recovery(self):
        now = time.time()
        conn = self.connect()
        for jid, state in (("q", "queued"), ("r", "running"), ("d", "done"), ("f", "failed")):
            conn.execute("INSERT INTO jobs (id, kind, title, state, message, created_at, updated_at) VALUES (?,?,?,?,?,?,?)", (jid, "t", jid, state, "orig", now, now))
        conn.close()
        self.assertEqual(self.manager.recover_interrupted(), 2)
        states = {j: self.manager.get(j) for j in "qrdf"}
        self.assertEqual([states[j]["state"] for j in "qrdf"], ["failed", "failed", "done", "failed"])
        self.assertIn("Interrupted by a restart", states["q"]["message"])
        self.assertEqual(states["d"]["message"], "orig")
        self.assertEqual(self.manager.active(), [])

    def test_unknown_job_and_cancel_of_finished_job(self):
        self.assertIsNone(self.manager.get("nope"))
        job_id = self.manager.submit("t", "T", lambda p: JobOutcome("ok"))
        self.assertFalse(self.manager.request_cancel(job_id))
        self.assertFalse(self.manager.request_cancel("nope"))


class ThreadedJobTests(JobTestBase):
    inline = False

    def test_submit_returns_immediately_while_the_job_keeps_running(self):
        started, release = threading.Event(), threading.Event()

        def slow(p):
            p.plan([("work", 1)])
            p("work", 0, 100)
            started.set()
            release.wait(10)
            p("work", 100, 100)
            return JobOutcome("finished")
        t0 = time.monotonic()
        job_id = self.manager.submit("t", "slow", slow)
        self.assertLess(time.monotonic() - t0, 0.5)                       # the caller (an HTTP request) is not held
        self.assertTrue(started.wait(5))
        job = self.manager.get(job_id)
        self.assertEqual((job["state"], job["stage"]), ("running", "work"))
        self.assertEqual([j["id"] for j in self.manager.active()], [job_id])
        release.set()
        self.assertTrue(self.manager.wait_idle(5))
        self.assertEqual(self.manager.get(job_id)["state"], "done")

    def test_jobs_run_one_at_a_time_in_submission_order(self):
        order, gate = [], threading.Event()

        def make(name, wait=False):
            def run(p):
                order.append(f"start {name}")
                if wait:
                    gate.wait(5)
                order.append(f"end {name}")
                return JobOutcome("ok")
            return run
        a = self.manager.submit("t", "A", make("A", wait=True))
        b = self.manager.submit("t", "B", make("B"))
        time.sleep(0.2)
        self.assertEqual(order, ["start A"])                              # B waits for A
        self.assertEqual(self.manager.get(b)["state"], "queued")
        gate.set()
        self.assertTrue(self.manager.wait_idle(5))
        self.assertEqual(order, ["start A", "end A", "start B", "end B"])

    def test_cancel_a_running_job_from_another_thread(self):
        entered, cleaned = threading.Event(), []

        def long(p):
            try:
                for i in range(1000):
                    p("loop", i, 1000)
                    entered.set()
                    time.sleep(0.01)
            finally:
                cleaned.append(True)
            return JobOutcome("should not finish")
        job_id = self.manager.submit("t", "long", long)
        self.assertTrue(entered.wait(5))
        self.assertTrue(self.manager.request_cancel(job_id))
        self.assertTrue(self.manager.wait_idle(5))
        self.assertEqual(self.manager.get(job_id)["state"], "cancelled")
        self.assertEqual(cleaned, [True])                                 # the job's own cleanup ran

    def test_cancelling_a_queued_job_means_it_never_runs(self):
        gate, ran = threading.Event(), []
        first = self.manager.submit("t", "first", lambda p: (gate.wait(5), JobOutcome("ok"))[1])
        second = self.manager.submit("t", "second", lambda p: (ran.append(1), JobOutcome("ok"))[1])
        self.assertTrue(self.manager.request_cancel(second))
        gate.set()
        self.assertTrue(self.manager.wait_idle(5))
        self.assertEqual((self.manager.get(first)["state"], self.manager.get(second)["state"], ran), ("done", "cancelled", []))

    def test_a_crashing_job_does_not_kill_the_worker(self):
        def boom(p):
            raise RuntimeError("worker must survive")
        bad = self.manager.submit("t", "bad", boom)
        good = self.manager.submit("t", "good", lambda p: JobOutcome("still alive"))
        self.assertTrue(self.manager.wait_idle(5))
        self.assertEqual((self.manager.get(bad)["state"], self.manager.get(good)["state"]), ("failed", "done"))


if __name__ == "__main__":
    unittest.main()
