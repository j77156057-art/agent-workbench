import unittest,tempfile,os
import game_workbench as gw
class UnrealAssetIndexTests(unittest.TestCase):
 def test_classifies_assets(self):
  with tempfile.TemporaryDirectory() as d:
   open(os.path.join(d,'Game.uproject'),'w').write('{}')
   os.makedirs(os.path.join(d,'Content'),exist_ok=True)
   for n in ('BP_Player.uasset','Environment.uasset','Main.umap'):
    open(os.path.join(d,'Content',n),'wb').write(b'')
   x=gw.engine_inspect(d,'unreal')
   self.assertEqual(x['blueprints'][0]['path'],'Content/BP_Player.uasset')
   self.assertEqual(x['levels'][0]['path'],'Content/Main.umap')
if __name__=='__main__': unittest.main()
