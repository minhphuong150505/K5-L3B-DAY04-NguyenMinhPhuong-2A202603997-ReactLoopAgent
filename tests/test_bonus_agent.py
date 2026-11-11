"""Test offline cho phần bonus của agent: retry 429 có giới hạn, Retry-After, rút gọn context."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent
from agent import run_agent
from bonus.tools import BonusTools


def call(call_id, name, args):
    raw = json.dumps(args)
    msg = {'role': 'assistant', 'content': None,
           'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': raw}}]}
    return {'message': msg, 'calls': [{'id': call_id, 'name': name, 'arguments': raw}], 'finish': 'tool_calls', 'usage': {}}


def final(answer='Đã kiểm tra'):
    return {'message': {'role': 'assistant', 'content': answer}, 'calls': [], 'finish': 'stop', 'usage': {}}


class ScriptedProvider:
    def __init__(self, responses):
        self.responses, self.histories = iter(responses), []

    def complete(self, messages, schemas):
        self.histories.append(list(messages))
        return next(self.responses)


class CountingTools:
    """Tool giả: luôn trả 429 với Retry-After cho trước, đếm số lần bị gọi thật."""
    schemas = []
    system_prompt = 'Prompt bonus giả.'

    def __init__(self, retry_after):
        self.retry_after, self.calls = retry_after, 0

    def execute(self, name, args):
        self.calls += 1
        return {'ok': False, 'error': 'RATE_LIMITED', 'http_status': 429, 'retry_after': self.retry_after}


def tool_results(provider):
    return [json.loads(m['content']) for m in provider.histories[-1] if m['role'] == 'tool']


class BonusRetryTests(unittest.TestCase):
    def test_retry_stops_after_limit(self):
        tools = BonusTools(scenario='rate_limit_always')
        p = ScriptedProvider([call(str(i), 'get_vercel_status', {}) for i in range(5)] + [final()])
        result = run_agent('Kiểm tra Vercel', p, tools, max_steps=8)
        self.assertEqual(result['status'], 'completed')
        errors = [r['error'] for r in tool_results(p)]
        limit = agent.MAX_RATE_LIMIT_RETRIES
        self.assertEqual(errors[:limit], ['RATE_LIMITED'] * limit)
        self.assertEqual(set(errors[limit:]), {'RETRY_LIMIT_REACHED'})
        # Tool thật chỉ bị gọi đúng `limit` lần, các lần sau agent tự chặn.
        self.assertEqual(tools.calls[('get_vercel_status', 'vercel')], limit)

    def test_success_after_429_resets_counter(self):
        tools = BonusTools(scenario='rate_limit_once')
        p = ScriptedProvider([call('a', 'get_vercel_status', {}), call('b', 'get_vercel_status', {}), final()])
        run_agent('Kiểm tra', p, tools, max_steps=5)
        first, second = tool_results(p)
        self.assertEqual((first['error'], first['attempt']), ('RATE_LIMITED', 1))
        self.assertEqual(second['provider'], 'vercel')

    def test_waits_for_short_retry_after(self):
        tools = CountingTools(retry_after='2')
        p = ScriptedProvider([call('a', 'get_npm_status', {}), call('b', 'get_npm_status', {}), final()])
        with patch.object(agent.time, 'sleep') as sleep:
            run_agent('Kiểm tra', p, tools, max_steps=5)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(tools.calls, 2)

    def test_long_retry_after_is_not_waited(self):
        tools = CountingTools(retry_after='120')
        p = ScriptedProvider([call('a', 'get_npm_status', {}), call('b', 'get_npm_status', {}), final()])
        with patch.object(agent.time, 'sleep') as sleep:
            run_agent('Kiểm tra', p, tools, max_steps=5)
        sleep.assert_not_called()
        self.assertEqual(tools.calls, 1)
        self.assertEqual(tool_results(p)[-1]['error'], 'RETRY_LIMIT_REACHED')


class BonusContextTests(unittest.TestCase):
    def test_noisy_list_is_compacted_for_model_but_not_trace(self):
        tools = BonusTools(scenario='noisy')
        p = ScriptedProvider([call('a', 'get_vercel_status', {}), final()])
        result = run_agent('Kiểm tra', p, tools, max_steps=3)
        sent = tool_results(p)[0]
        self.assertEqual(len(sent['distractors']), agent.MAX_LIST_ITEMS)
        self.assertEqual(sent['distractors_omitted'], 160 - agent.MAX_LIST_ITEMS)
        traced = [e for e in result['trace'] if e['event'] == 'tool'][0]['result']
        self.assertEqual(len(traced['distractors']), 160)

    def test_main_tools_are_not_compacted(self):
        class MainTools:  # tools bài chính (không có system_prompt) với list dài
            def execute(self, name, args):
                return {'ok': True, 'distractors': list(range(40))}
        p = ScriptedProvider([call('a', 'get_status', {}), final()])
        run_agent('Kiểm tra', p, MainTools(), max_steps=3)
        self.assertEqual(len(tool_results(p)[0]['distractors']), 40)

    def test_bonus_prompt_gets_extra_rules_main_prompt_unchanged(self):
        p = ScriptedProvider([final()])
        run_agent('Kiểm tra', p, BonusTools())
        system = p.histories[0][0]['content']
        self.assertIn('DigitalOcean', system)
        self.assertIn('next_offset', system)
        self.assertIn('KHÔNG phải outage', system)

        class MainTools:  # không có system_prompt -> dùng prompt bài chính, không nối thêm
            def execute(self, name, args):
                return {}
        p = ScriptedProvider([final()])
        run_agent('Kiểm tra', p, MainTools())
        self.assertEqual(p.histories[0][0]['content'], agent.SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()
