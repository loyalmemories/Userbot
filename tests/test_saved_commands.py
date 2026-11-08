"""Exercise save handlers without Telegram credentials or a live database."""

import asyncio
import builtins
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = (
    ('notes', 'save', 'add_note', 'notes_sql', 'add_note'),
    ('snips', 'snip', 'on_snip_save', 'snips_sql', 'add_snip'),
    ('filter', 'filter', 'add_new_filter', 'filter_sql', 'add_filter'),
)


def load_handler(module, handler_name):
    userbot = ModuleType('userbot')
    userbot.CMD_HELP = {}
    userbot.BOTLOG = False
    userbot.BOTLOG_CHATID = 42
    events = ModuleType('userbot.events')

    def register(**options):
        def decorate(handler):
            handler.pattern = options.get('pattern')
            return handler
        return decorate

    events.register = register
    spec = importlib.util.spec_from_file_location(
        'saved_command_under_test', ROOT / 'userbot/modules' / (module + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'userbot': userbot, 'userbot.events': events}):
        spec.loader.exec_module(loaded)
    return getattr(loaded, handler_name)


class Event:
    def __init__(self, handler, text, reply):
        self.text = text
        self.pattern_match = re.match(handler.pattern, text)
        self.chat_id = 123
        self.reply_to_msg_id = 7 if reply else None
        self.reply = reply
        self.edits = []
        self.forwarded = []
        self.client = self

    async def edit(self, text):
        self.edits.append(text)

    async def get_reply_message(self):
        return self.reply

    async def send_message(self, *args, **kwargs):
        pass

    async def forward_messages(self, **kwargs):
        self.forwarded.append(kwargs)
        return SimpleNamespace(id=99)


class SavedCommandsTest(unittest.TestCase):
    def run_command(self, config, suffix, reply=None, sql_error=False,
                    botlog=42, added=True):
        module, command, handler_name, sql_module, sql_function = config
        handler = load_handler(module, handler_name)
        handler.__globals__['BOTLOG_CHATID'] = botlog
        event = Event(handler, '.' + command + suffix, reply)
        self.assertIsNotNone(event.pattern_match, event.text)
        sql = ModuleType('userbot.modules.sql_helper.' + sql_module)
        save = Mock(return_value=added)
        setattr(sql, sql_function, save)
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if sql_error and name == sql.__name__:
                raise AttributeError('SQL is unavailable')
            return original_import(name, *args, **kwargs)

        with patch.dict(sys.modules, {sql.__name__: sql}), patch(
                'builtins.__import__', side_effect=import_module):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(handler(event))
            finally:
                loop.close()
        return event, save

    def test_missing_content_does_not_write(self):
        for config in COMMANDS:
            for suffix in (' key', ' key   '):
                with self.subTest(command=config[1], suffix=suffix):
                    event, save = self.run_command(config, suffix)
                    save.assert_not_called()
                    self.assertIn('Usage:', event.edits[-1])
                    self.assertIn('.' + config[1], event.edits[-1])

    def test_missing_name_shows_usage(self):
        for config in COMMANDS:
            for suffix in ('', ' ', '   content'):
                with self.subTest(command=config[1], suffix=suffix):
                    event, save = self.run_command(config, suffix)
                    save.assert_not_called()
                    self.assertIn('Usage:', event.edits[-1])

    def test_inline_content_and_command_like_names(self):
        for config in COMMANDS:
            for name in ('key', config[1], config[1][1:]):
                with self.subTest(command=config[1], name=name):
                    event, save = self.run_command(config, ' ' + name + ' hello')
                    self.assertEqual(save.call_args[0][-3:], (name, ' hello', None))
                    self.assertIn('successfully', event.edits[-1])

    def test_reply_text_with_trailing_spaces(self):
        for config in COMMANDS:
            with self.subTest(command=config[1]):
                reply = SimpleNamespace(text='saved reply', media=None)
                event, save = self.run_command(config, ' key  ', reply)
                self.assertEqual(save.call_args[0][-3:], ('key', 'saved reply', None))

    def test_inline_text_takes_precedence_over_reply(self):
        for config in COMMANDS:
            with self.subTest(command=config[1]):
                reply = SimpleNamespace(text='other text', media='photo')
                event, save = self.run_command(config, ' key hello', reply)
                self.assertEqual(save.call_args[0][-3:], ('key', ' hello', None))
                self.assertFalse(event.forwarded)

    def test_empty_reply_does_not_write(self):
        for config in COMMANDS:
            for text in (None, '', '   '):
                with self.subTest(command=config[1], text=text):
                    reply = SimpleNamespace(text=text, media=None)
                    event, save = self.run_command(config, ' key', reply)
                    save.assert_not_called()
                    self.assertIn('Usage:', event.edits[-1])

    def test_media_reply_with_trailing_spaces(self):
        for config in COMMANDS:
            with self.subTest(command=config[1]):
                reply = SimpleNamespace(text=None, media='photo')
                event, save = self.run_command(config, ' key  ', reply)
                self.assertEqual(save.call_args[0][-3:], ('key', '', 99))
                self.assertEqual(len(event.forwarded), 1)

    def test_media_without_log_chat_does_not_write(self):
        for config in COMMANDS:
            with self.subTest(command=config[1]):
                reply = SimpleNamespace(text=None, media='photo')
                event, save = self.run_command(config, ' key', reply, botlog=None)
                save.assert_not_called()
                self.assertIn('BOTLOG_CHATID', event.edits[-1])

    def test_non_sql_mode_shows_feedback(self):
        for config in COMMANDS:
            with self.subTest(command=config[1]):
                event, save = self.run_command(config, ' key hello', sql_error=True)
                save.assert_not_called()
                self.assertIn('Non-SQL', event.edits[-1])

    def test_existing_content_can_still_be_updated(self):
        for config in COMMANDS:
            with self.subTest(command=config[1]):
                event, save = self.run_command(config, ' key replacement', added=False)
                self.assertEqual(save.call_args[0][-3:], ('key', ' replacement', None))
                self.assertIn('updated', event.edits[-1])


if __name__ == '__main__':
    unittest.main()
