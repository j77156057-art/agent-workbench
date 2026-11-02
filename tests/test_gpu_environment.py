import unittest
from gpu_coordinator import process_environment
class EnvTests(unittest.TestCase):
 def test_env(self): self.assertEqual(process_environment(3)['CUDA_VISIBLE_DEVICES'],'3')
if __name__=='__main__': unittest.main()
