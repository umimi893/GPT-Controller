import unittest
from agent_runtime.runtime import Runtime


class FakeBus:
    def __init__(self):
        self.head = "aaa"
        self.sync_count = 0

    def remote_head(self):
        return self.head

    def sync(self):
        self.sync_count += 1

    def recover_ambiguous(self):
        return 0

    def pending(self):
        return []


class RuntimePollingTests(unittest.TestCase):
    def make_runtime(self):
        runtime = Runtime.__new__(Runtime)
        runtime.config = None
        runtime.bus = FakeBus()
        runtime.executor = None
        runtime._recovered = True
        runtime._remote_head = None
        return runtime

    def test_idle_same_remote_head_skips_full_sync(self):
        runtime = self.make_runtime()
        self.assertFalse(runtime.run_once())
        self.assertEqual(runtime.bus.sync_count, 1)
        self.assertFalse(runtime.run_once())
        self.assertEqual(runtime.bus.sync_count, 1)

    def test_changed_remote_head_triggers_sync(self):
        runtime = self.make_runtime()
        runtime.run_once()
        runtime.bus.head = "bbb"
        runtime.run_once()
        self.assertEqual(runtime.bus.sync_count, 2)


if __name__ == "__main__":
    unittest.main()
