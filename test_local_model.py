"""Kiểm tra giao tiếp Ollama mà không cần server/model."""
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import agent


class LocalModelTests(unittest.TestCase):
    def test_multiple_calls_and_tool_history(self):
        reply = {
            'message': {'content': 'Cần tra thời tiết.', 'tool_calls': [
                {'function': {'name': 'get_weather', 'arguments': {'city': 'Hà Nội', 'date': '2026-11-11'}}},
                {'function': {'name': 'get_weather', 'arguments': {'city': 'Đà Nẵng', 'date': '2026-11-11'}}},
            ]},
            'prompt_eval_count': 123, 'eval_count': 45,
        }
        with patch.object(agent, 'urlopen', return_value=io.BytesIO(json.dumps(reply).encode())) as send:
            response = agent.call_model([{'role': 'user', 'content': 'So sánh hai nơi'}])
            payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload['model'], agent.MODEL)
        self.assertFalse(payload['stream'])
        self.assertEqual(len(payload['tools']), 3)
        self.assertEqual(response.stop_reason, 'tool_use')
        self.assertEqual(response.usage.input_tokens, 123)
        self.assertEqual(response.usage.output_tokens, 45)
        calls = [b for b in response.content if b.type == 'tool_use']
        self.assertEqual(len({b.id for b in calls}), 2)
        history = [
            {'role': 'user', 'content': 'So sánh hai nơi'},
            {'role': 'assistant', 'content': response.content},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': '{"temp_high_c": 30}'},
                {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': 'ERROR: thử lại'},
            ]},
        ]
        with patch.object(agent, 'urlopen', return_value=io.BytesIO(b'{"message":{"content":"Xong"}}')) as send:
            final = agent.call_model(history)
            payload = json.loads(send.call_args.args[0].data)
        self.assertEqual([m['role'] for m in payload['messages']], ['system', 'user', 'assistant', 'tool', 'tool'])
        self.assertEqual([m['tool_name'] for m in payload['messages'][-2:]], ['get_weather', 'get_weather'])
        self.assertEqual(payload['messages'][-1]['content'], 'ERROR: thử lại')
        self.assertEqual(final.stop_reason, 'end_turn')
        self.assertEqual(final.content[0].text, 'Xong')

    def test_connection_and_http_errors(self):
        with patch.object(agent, 'urlopen', side_effect=URLError('connection refused')):
            with self.assertRaisesRegex(RuntimeError, 'Không kết nối được Ollama'):
                agent.call_model([{'role': 'user', 'content': 'Chào'}])
        error = HTTPError(agent.OLLAMA_HOST, 404, 'Not found', {}, io.BytesIO(b'model not found'))
        with patch.object(agent, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'model not found'):
                agent.call_model([{'role': 'user', 'content': 'Chào'}])


if __name__ == '__main__':
    unittest.main()
