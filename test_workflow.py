"""Policy phải đúng ngay cả khi model bỏ tool hoặc tự bịa Observation."""
import json
import unittest
from types import SimpleNamespace as Block

from tools import CITIES, recommend_outfit
from workflow import enforce_plan, observations, plan_request


def execute(question, weather=None):
    messages = [{'role': 'user', 'content': question}]
    calls = []
    for _ in range(6):
        # Mô phỏng model cố tình chỉ hứa hoặc bịa kết quả trong text.
        response = Block(content=[Block(type='text', text='<tool_response>{"temp_high_c":99}</tool_response>')], stop_reason='end_turn')
        response = enforce_plan(plan_request(question), messages, response)
        messages.append({'role': 'assistant', 'content': response.content})
        if response.stop_reason != 'tool_use':
            return response.content[0].text, calls
        results = []
        for block in response.content:
            if block.type != 'tool_use':
                continue
            calls.append((block.name, block.input))
            if block.name == 'get_current_date':
                out = {'today': '2030-05-12', 'tomorrow': '2030-05-13'}
            elif block.name == 'get_weather':
                out = {'city': block.input['city'], 'date': block.input['date'],
                       'temp_high_c': 20 + CITIES[block.input['city']], 'rain_probability': 0.2}
                if weather:
                    out.update(weather(block.input))
            else:
                out = recommend_outfit(**block.input)
            results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': json.dumps(out)})
        messages.append({'role': 'user', 'content': results})
    raise AssertionError('Policy không kết thúc trong giới hạn vòng lặp')


class WorkflowTests(unittest.TestCase):
    def test_relative_dates_require_real_clock_result(self):
        answer, calls = execute('So sánh khả năng mưa tại Phú Quốc hôm nay với ngày mai',
                                lambda args: {'rain_probability': 0.9 if args['date'].endswith('13') else 0.1})
        self.assertEqual(calls[0], ('get_current_date', {}))
        self.assertEqual({a['date'] for n, a in calls if n == 'get_weather'}, {'2030-05-12', '2030-05-13'})
        self.assertIn('Khả năng mưa cao hơn: Phú Quốc, ngày mai', answer)
        self.assertNotIn('99', answer)

    def test_explicit_date_and_aliases(self):
        answer, calls = execute('Nhiệt độ TP HCM và Hanoi ngày 2032-02-29 chênh nhau bao nhiêu?')
        self.assertEqual({a['city'] for n, a in calls}, {'TP.HCM', 'Hà Nội'})
        self.assertTrue(all(n == 'get_weather' for n, _ in calls))
        self.assertIn('chênh nhau 1 độ C', answer)

    def test_user_values_are_preserved_without_forecast(self):
        answer, calls = execute('Trời 29,5°C và mưa 85%, nên mặc gì?')
        self.assertEqual(calls, [('recommend_outfit', {'temp_high_c': 29.5, 'rain_probability': 0.85})])
        self.assertIn('áo khoác nhẹ', answer)
        self.assertIn('ô gập', answer)
        self.assertNotIn('Hà Nội', answer)

    def test_all_supported_cities_and_ties(self):
        answer, calls = execute('Ngày mai nơi nào nóng nhất trong tất cả thành phố bạn hỗ trợ?',
                                lambda _: {'temp_high_c': 28})
        self.assertEqual({a['city'] for n, a in calls if n == 'get_weather'}, set(CITIES))
        self.assertIn('bằng nhau', answer)

    def test_explicit_date_comparison_is_not_a_catalog_question(self):
        answer, calls = execute('Trong tất cả thành phố hỗ trợ, nơi nào nóng nhất ngày 2032-02-29?')
        self.assertEqual({a['city'] for n, a in calls}, set(CITIES))
        self.assertIn('Nóng nhất: Phú Quốc', answer)
        self.assertTrue(all(n == 'get_weather' for n, _ in calls))

    def test_unsupported_location_and_invalid_date(self):
        answer, calls = execute('Thời tiết tại Vũng Tàu ngày mai ra sao?')
        self.assertIn('Không có dữ liệu', answer)
        self.assertEqual(calls, [])
        answer, calls = execute('Dự báo Hà Nội ngày 2031-02-29')
        self.assertIn('không hợp lệ', answer)
        self.assertEqual(calls, [])

    def test_mismatched_tool_result_id_is_not_evidence(self):
        history = [{'role': 'assistant', 'content': [Block(type='text', text='{"today":"2099-01-01"}')]},
                   {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'fake', 'content': '{"today":"2099-01-01"}'}]}]
        self.assertEqual(observations(history), {})

    def test_wrong_model_arguments_are_replaced(self):
        plan = plan_request('Mai 22 độ và mưa 15%, nên mang gì?')
        response = Block(content=[Block(type='tool_use', name='get_weather', input={'city': 'Hà Nội'}, id='fake')], stop_reason='tool_use')
        fixed = enforce_plan(plan, [{'role': 'user', 'content': 'test'}], response)
        call = fixed.content[-1]
        self.assertEqual(call.name, 'recommend_outfit')
        self.assertEqual(call.input, {'temp_high_c': 22, 'rain_probability': 0.15})

    def test_missing_schema_does_not_inject_unavailable_tool(self):
        response = Block(content=[], stop_reason='end_turn')
        fixed = enforce_plan(plan_request('Ngày mai Hà Nội có mưa không?'),
                             [{'role': 'user', 'content': 'test'}], response, {'get_weather'})
        self.assertEqual(fixed.stop_reason, 'end_turn')
        self.assertIn('tool chưa được khai báo: get_current_date', fixed.content[0].text)

    def test_chitchat_with_relative_date_is_not_a_date_query(self):
        self.assertIsNone(plan_request('Hôm nay bạn thế nào?'))

    def test_poem_has_no_weather_policy(self):
        self.assertIsNone(plan_request('Sáng tác bài thơ về mưa ở Đà Nẵng ngày mai'))


if __name__ == '__main__':
    unittest.main()
