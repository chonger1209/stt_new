from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from single_instance import SingleInstance


class InstanceTests(unittest.TestCase):
    def test_same_directory_excludes_another_process_then_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = SingleInstance(directory)
            self.assertTrue(lock.acquire())
            code = 'import sys; from single_instance import SingleInstance; x=SingleInstance(sys.argv[1]); print(x.acquire()); x.close()'
            def child():
                return subprocess.check_output([sys.executable, '-c', code, directory],
                    cwd=Path(__file__).parent, text=True).strip()
            try:
                self.assertEqual(child(), 'False')
                alias = SingleInstance(str(Path(directory) / '.'))
                self.assertFalse(alias.acquire())
                with tempfile.TemporaryDirectory() as other:
                    independent = SingleInstance(other)
                    self.assertTrue(independent.acquire())
                    independent.close()
            finally:
                lock.close()
            self.assertEqual(child(), 'True')

    def test_process_termination_releases_mutex_without_stale_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            code = 'import sys,time; from single_instance import SingleInstance; x=SingleInstance(sys.argv[1]); print(x.acquire(),flush=True); time.sleep(30)'
            child = subprocess.Popen([sys.executable, '-c', code, directory], stdout=subprocess.PIPE,
                                     cwd=Path(__file__).parent, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'True')
                other = SingleInstance(directory)
                self.assertFalse(other.acquire())
            finally:
                child.terminate()
                child.wait(3)
                child.stdout.close()
            self.assertTrue(other.acquire())
            other.close()


if __name__ == '__main__':
    unittest.main()
