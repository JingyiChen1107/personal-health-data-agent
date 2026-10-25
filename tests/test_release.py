from pathlib import Path
from tempfile import TemporaryDirectory
import json,unittest,zipfile,stat
from scripts.build_release import source_files,write_entry
class ReleaseBoundaryTests(unittest.TestCase):
    def test_only_frozen_files_no_env_runtime_database_or_upload(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ['.env','.env.example','.runtime/private','.venv/private','data/conversations.sqlite3','sample_data/seed.txt','sample_data/user_upload.txt','streamlit_app.py']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('TEST_ONLY')
            (root/'sample_data/seed.json').write_text(json.dumps({'reports':[{'file_path':'sample_data/seed.txt'}]}))
            files={str(p.relative_to(root)) for p in source_files(root)}
            self.assertIn('.env.example',files);self.assertIn('sample_data/seed.txt',files)
            self.assertFalse(files & {'.env','.runtime/private','.venv/private','data/conversations.sqlite3','sample_data/user_upload.txt'})
    def test_archive_command_keeps_executable_flag(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'test.zip'
            with zipfile.ZipFile(path,'w') as z:write_entry(z,'演示前检查.command',b'#!/bin/zsh\n')
            with zipfile.ZipFile(path) as z:self.assertTrue((z.getinfo('演示前检查.command').external_attr>>16)&stat.S_IXUSR)
