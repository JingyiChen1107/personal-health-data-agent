from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from health_agent.agent import HealthAgent
from health_agent.config import PROJECT_ROOT, load_config
from health_agent.results import ToolContext
from health_agent.seed import initialize_database
from health_agent.tools import HealthTools
from tests.scripted_provider import ScriptedProvider, call, final


class ChatAppTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'test.sqlite3'
        initialize_database(self.db)
        context = ToolContext('P001', 'app-test', load_config().reference_date)
        self.tools = HealthTools(self.db, context=context)

    def app(self):
        return AppTest.from_file(str(PROJECT_ROOT / 'streamlit_app.py'), default_timeout=15)

    def test_no_key_page_renders_and_chat_is_disabled(self):
        with patch('health_agent.app_support.configuration_status', return_value={'configured': False, 'model': None, 'message': '请配置本地模型。'}), \
                patch('health_agent.config.DEFAULT_DATABASE', self.db):
            app = self.app().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(app.chat_input[0].disabled)
            self.assertTrue(any('配置本地模型' in info.value for info in app.info))

    def test_chat_submits_once_and_displays_real_tool_sources(self):
        provider = ScriptedProvider([call('query_health_data', {'indicator': 'glucose'}), final('最近血糖6.9 mmol/L。[G032]', ['G032'])])
        agent = HealthAgent(self.tools, provider)
        with patch('health_agent.app_support.configuration_status', return_value={'configured': True, 'model': 'test-double-only'}), \
                patch('health_agent.app_support.create_session_agent', return_value=agent), \
                patch('health_agent.config.DEFAULT_DATABASE', self.db):
            app = self.app().run()
            app.chat_input[0].set_value('查最近血糖').run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.chat_message), 2)
            self.assertTrue(any('6.9' in text.value for text in app.markdown))
            self.assertTrue(any(item.label == '数据来源' for item in app.expander))
            app.run()
            self.assertEqual(len(provider.requests), 2)
            self.assertEqual(len(app.chat_message), 2)

    def test_new_session_clears_displayed_history(self):
        provider = ScriptedProvider([final('你好。')])
        agent = HealthAgent(self.tools, provider)
        with patch('health_agent.app_support.configuration_status', return_value={'configured': True, 'model': 'test-double-only'}), \
                patch('health_agent.app_support.create_session_agent', return_value=agent), \
                patch('health_agent.config.DEFAULT_DATABASE', self.db):
            app = self.app().run()
            first_id = app.session_state['session_id']
            app.chat_input[0].set_value('你好').run()
            self.assertEqual(len(app.chat_message), 2)
            app.sidebar.button[0].click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.chat_message), 0)
            self.assertNotEqual(first_id, app.session_state['session_id'])

    def test_unexpected_error_does_not_render_exception_or_secret(self):
        with patch('health_agent.app_support.configuration_status', return_value={'configured': True, 'model': 'test-double-only'}), \
                patch('health_agent.app_support.create_session_agent', side_effect=RuntimeError('SUPER_SECRET')), \
                patch('health_agent.config.DEFAULT_DATABASE', self.db):
            app = self.app().run()
            app.chat_input[0].set_value('查血糖').run()
            self.assertEqual(len(app.exception), 0)
            texts = ' '.join(item.value for item in app.markdown)
            self.assertNotIn('SUPER_SECRET', texts)
            self.assertIn('执行失败', texts)


if __name__ == '__main__':
    unittest.main()
