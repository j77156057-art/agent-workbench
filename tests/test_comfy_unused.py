import unittest,tempfile,os
import game_workbench as gw
class UnusedTests(unittest.TestCase):
 def test_unused(self):
  with tempfile.TemporaryDirectory() as d:
   p=os.path.join(d,'assets','generated'); os.makedirs(p)
   with open(os.path.join(p,'unused.png'),'wb') as handle: handle.write(b'x')
   with open(os.path.join(p,'used.png'),'wb') as handle: handle.write(b'x')
   with open(os.path.join(d,'scene.tscn'),'w') as handle: handle.write('used.png')
   x=gw.comfy_unused_resources(d); self.assertEqual(x['unused'],['assets/generated/unused.png'])
if __name__=='__main__': unittest.main()
