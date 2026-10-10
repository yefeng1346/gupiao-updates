"""Regression for the actual HTTP boundary missed by download-only checks."""
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from app.db import Database
from app.update_service import UpdateError
from test_capital_flow_api import LocalAsgiClient

class InstallApiTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        db=Database(Path(self.folder.name)/'isolated.db')
        with patch('app.db.Database',return_value=db),patch('app.config.ensure_directories'):
            self.main=importlib.import_module('main')
        self.client=LocalAsgiClient(self.main.app)
        self.jobs=Mock()
        self.jobs.is_running.return_value=False
        self.preparation=Mock()
        self.thread=Mock()
        for name,value in (('_flow_jobs',self.jobs),('_flow_active_operations',0),('UPDATE_PREPARATION',self.preparation),('Thread',self.thread)):
            handle=patch.object(self.main,name,value);handle.start();self.addCleanup(handle.stop)

    def install(self):
        return self.client.get('/api/update/install',method='POST')

    def test_ready_installs_and_schedules_exit_only_after_success(self):
        self.preparation.install.return_value={'status':'installing','message':'正在安装'}
        response=self.install()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['status'],'installing')
        self.preparation.install.assert_called_once()
        self.thread.return_value.start.assert_called_once()

    def test_not_ready_is_409_not_500_and_never_exits(self):
        self.preparation.install.side_effect=UpdateError('安装包尚未准备好或已取消')
        response=self.install()
        self.assertEqual(response.status_code,409)
        self.assertIn('尚未准备好',response.json()['detail'])
        self.thread.assert_not_called()

    def test_active_history_job_blocks_install(self):
        self.jobs.is_running.return_value=True
        self.assertEqual(self.install().status_code,409)
        self.preparation.install.assert_not_called()
        self.thread.assert_not_called()

    def test_active_current_or_automatic_flow_blocks_install(self):
        with patch.object(self.main,'_flow_active_operations',1):
            self.assertEqual(self.install().status_code,409)
        self.preparation.install.assert_not_called()
        self.thread.assert_not_called()

    def test_file_failure_preserves_application(self):
        self.preparation.install.side_effect=OSError('模拟文件被占用')
        response=self.install()
        self.assertEqual(response.status_code,409)
        self.assertIn('文件被占用',response.json()['detail'])
        self.thread.assert_not_called()

if __name__=='__main__':unittest.main()
