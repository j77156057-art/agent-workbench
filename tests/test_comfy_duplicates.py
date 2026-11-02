import unittest,tempfile,os
import game_workbench as gw
class DuplicateTests(unittest.TestCase):
 def test_hash_groups(self):
  with tempfile.TemporaryDirectory() as d:
   os.makedirs(os.path.join(d,'assets','generated'))
   for n in ('a.png','b.png'):
    with open(os.path.join(d,'assets','generated',n),'wb') as handle: handle.write(b'same')
   x=gw.comfy_resource_duplicates(d)
   self.assertEqual(x['duplicate_files'],1); self.assertEqual(len(x['groups']),1)
if __name__=='__main__': unittest.main()
