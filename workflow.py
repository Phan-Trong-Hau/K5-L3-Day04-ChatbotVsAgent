"""Kiểm tra bằng chứng cho trợ lý thời tiết, độc lập với bộ câu hỏi chấm điểm.

LLM có thể đề nghị hành động; policy chỉ chấp nhận dữ liệu từ tool_result thật.
Các phép so sánh và phần số liệu được tính bằng Python để không phụ thuộc việc
model nhỏ ghi nhớ số, làm toán hoặc dịch tên trang phục.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

from tools import CITIES


def normalize(text: str) -> str:
    text = unicodedata.normalize('NFD', text.lower().replace('đ', 'd'))
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn')


def signature(name: str, arguments: dict) -> tuple[str, str]:
    return name, json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def observations(messages: list[dict]) -> dict:
    """Đọc kết quả do executor gửi; bỏ qua mọi tool_response nằm trong text model."""
    calls, results = {}, {}
    for message in messages:
        content = message['content']
        if isinstance(content, str):
            continue
        if message['role'] == 'assistant':
            for block in content:
                if block.type == 'tool_use':
                    calls[block.id] = signature(block.name, block.input)
        elif message['role'] == 'user':
            for block in content:
                if block.get('type') != 'tool_result' or block['tool_use_id'] not in calls:
                    continue
                raw = block['content']
                if raw.startswith('ERROR:'):
                    continue
                try:
                    value = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    results[calls[block['tool_use_id']]] = value
    return results


@dataclass
class RequestPlan:
    kind: str
    cities: tuple[str, ...] = ()
    relative_days: tuple[str, ...] = ()
    explicit_days: tuple[str, ...] = ()
    outfit: bool = False
    supplied: tuple[float, float] | None = None
    error: str = ''
    comparison: str = ''

    def next_actions(self, evidence: dict) -> list[tuple[str, dict]]:
        if self.error or self.kind == 'catalog':
            return []
        if self.supplied is not None:
            temp, rain = self.supplied
            actions = [('recommend_outfit', {'temp_high_c': temp, 'rain_probability': rain})]
        elif self.relative_days and signature('get_current_date', {}) not in evidence:
            actions = [('get_current_date', {})]
        elif self.kind == 'date':
            return []
        else:
            actions = [('get_weather', {'city': city, 'date': day})
                       for city in self.cities for day in self.days(evidence)]
            missing = [(name, args) for name, args in actions if signature(name, args) not in evidence]
            if missing:
                return missing
            if not self.outfit:
                return []
            actions = [('recommend_outfit', {
                'temp_high_c': evidence[signature(name, args)]['temp_high_c'],
                'rain_probability': evidence[signature(name, args)]['rain_probability'],
            }) for name, args in actions]
        return [(name, args) for name, args in actions if signature(name, args) not in evidence]

    def days(self, evidence: dict) -> tuple[str, ...]:
        current = evidence.get(signature('get_current_date', {}), {})
        return tuple(dict.fromkeys((*self.explicit_days, *(current[key] for key in self.relative_days))))

    def day_label(self, day: str, evidence: dict) -> str:
        current = evidence.get(signature('get_current_date', {}), {})
        for key, label in [('today', 'hôm nay'), ('tomorrow', 'ngày mai')]:
            if day == current.get(key):
                return f'{label} ({day})'
        return day

    def answer(self, evidence: dict) -> str:
        if self.error:
            return self.error
        if self.kind == 'catalog':
            return 'Tôi tra được thời tiết ở: ' + ', '.join(CITIES) + '.'
        if self.kind == 'date':
            current = evidence[signature('get_current_date', {})]
            lines = []
            for key in self.relative_days:
                day = date.fromisoformat(current[key])
                label = 'Hôm nay' if key == 'today' else 'Ngày mai'
                lines.append(f'{label} là ngày {day.day} tháng {day.month} năm {day.year}.')
            return ' '.join(lines)
        if self.supplied is not None:
            temp, rain = self.supplied
            items = evidence[signature('recommend_outfit', {'temp_high_c': temp, 'rain_probability': rain})]['items']
            return f'Với nhiệt độ {temp:g}°C và khả năng mưa {rain * 100:g}%, nên mang: ' + '; '.join(items) + '.'

        records, lines = [], []
        for city in self.cities:
            for day in self.days(evidence):
                weather = evidence[signature('get_weather', {'city': city, 'date': day})]
                temp, rain = weather['temp_high_c'], weather['rain_probability']
                label = f'{city}, {self.day_label(day, evidence)}'
                records.append((label, temp, rain))
                line = f'{label}: nhiệt độ cao nhất {temp:g}°C, khả năng mưa {rain * 100:g}%.'
                if self.outfit:
                    items = evidence[signature('recommend_outfit', {'temp_high_c': temp, 'rain_probability': rain})]['items']
                    line += ' Gợi ý: ' + '; '.join(items) + '.'
                    if rain <= 0.5:
                        line += ' Không cần mang ô theo ngưỡng mưa của tool.'
                lines.append(line)
        if len(records) > 1:
            if self.comparison == 'difference':
                difference = max(r[1] for r in records) - min(r[1] for r in records)
                lines.append(f'Nhiệt độ cao nhất chênh nhau {difference:g} độ C.')
            elif self.comparison:
                index = 1 if self.comparison == 'hottest' else 2
                choose = min if self.comparison == 'driest' else max
                target = choose(r[index] for r in records)
                winners = [r[0] for r in records if r[index] == target]
                if len(winners) == len(records):
                    lines.append('Các nơi/ngày có giá trị bằng nhau.')
                else:
                    title = {'hottest': 'Nóng nhất', 'driest': 'Ít khả năng mưa hơn', 'wettest': 'Khả năng mưa cao hơn'}[self.comparison]
                    lines.append(title + ': ' + '; '.join(winners) + '.')
        return '\n'.join(lines)


def plan_request(question: str) -> RequestPlan | None:
    text = normalize(question)
    if re.search(r'\b(viet|sang tac|lam)\b.*\b(tho|truyen|bai hat)\b', text):
        return None
    relative = tuple(key for pattern, key in [(r'\bhom nay\b', 'today'), (r'\b(?:ngay )?mai\b', 'tomorrow')]
                     if re.search(pattern, text))
    weather = bool(re.search(r'thoi tiet|nhiet do|du bao|\bmua\b|\bmac\b|trang phuc|\bnong\b|\blanh\b|\bmang\b', text))
    if ('thanh pho' in text and not relative and re.search(r'nao|ho tro', text)
            and not re.search(r'\d{4}-\d{2}-\d{2}|nong|nhiet do|mua|chenh', text)):
        return RequestPlan('catalog')
    if not weather:
        asks_date = bool(re.search(r'(?:ngay|thu) (?:la )?(?:may|bao nhieu)|(?:hom nay|ngay mai) la ngay', text))
        return RequestPlan('date', relative_days=relative) if relative and asks_date else None
    outfit = bool(re.search(r'\bmac\b|\bmang\b|trang phuc', text))
    temperature = re.search(r'(-?\d+(?:[.,]\d+)?)\s*(?:°\s*c?|do\b)', text)
    probability = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:%|phan tram)', text)
    if outfit and temperature and probability:
        temp = float(temperature[1].replace(',', '.'))
        rain = float(probability[1].replace(',', '.')) / 100
        if not 0 <= rain <= 1:
            return RequestPlan('weather', error='Khả năng mưa không hợp lệ; cần từ 0% đến 100%.')
        return RequestPlan('weather', outfit=True, supplied=(temp, rain))
    aliases = {
        'Hà Nội': ('ha noi', 'hanoi'),
        'TP.HCM': ('tp.hcm', 'tp. hcm', 'tp hcm', 'tphcm', 'ho chi minh', 'sai gon'),
        'Đà Nẵng': ('da nang', 'danang'),
        'Phú Quốc': ('phu quoc', 'phuquoc'),
    }
    cities = tuple(city for city, names in aliases.items() if any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text) for name in names))
    if re.search(r'(?:4|bon|tat ca|cac) thanh pho', text) and 'ho tro' in text:
        cities = tuple(CITIES)
    if not cities:
        return RequestPlan('weather', error='Không có dữ liệu cho địa điểm này. Chỉ hỗ trợ: ' + ', '.join(CITIES) + '.')
    explicit = tuple(dict.fromkeys(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text)))
    for day in explicit:
        try:
            date.fromisoformat(day)
        except ValueError:
            return RequestPlan('weather', error=f'Ngày {day} không hợp lệ; cần ngày có thật theo định dạng YYYY-MM-DD.')
    if not relative and not explicit:
        return RequestPlan('weather', error='Bạn muốn tra thời tiết vào ngày nào? Hãy cho biết ngày cụ thể, hôm nay hoặc ngày mai.')
    comparison = ''
    if 'chenh' in text:
        comparison = 'difference'
    elif 'mua' in text and re.search(r'hon|nhat|so sanh', text):
        comparison = 'driest' if re.search(r'\bit\b|thap|kho', text) else 'wettest'
    elif re.search(r'nong nhat|nhiet do.*cao nhat', text):
        comparison = 'hottest'
    return RequestPlan('weather', cities, relative, explicit, outfit, comparison=comparison)


def enforce_plan(plan: RequestPlan | None, messages: list[dict], response, available_tools: set[str] | None = None):
    """Sửa đề nghị thiếu/sai của model; số liệu final chỉ lấy từ bằng chứng thật."""
    if plan is None:
        return response
    evidence = observations(messages)
    pending = plan.next_actions(evidence)
    if pending:
        unavailable = {name for name, _ in pending} - available_tools if available_tools is not None else set()
        if unavailable:
            response.content = [SimpleNamespace(type='text', text='Không thể hoàn thành: tool chưa được khai báo: ' + ', '.join(sorted(unavailable)) + '.')]
            response.stop_reason = 'end_turn'
            return response
        expected = {signature(name, args) for name, args in pending}
        proposed = {signature(b.name, b.input) for b in response.content if b.type == 'tool_use'}
        if proposed != expected:
            text = 'Policy: cần lấy đủ bằng chứng qua ' + ', '.join(name for name, _ in pending) + '.'
            response.content = [SimpleNamespace(type='text', text=text)] + [
                SimpleNamespace(type='tool_use', id=f'policy_{len(messages)}_{i}', name=name, input=args)
                for i, (name, args) in enumerate(pending)
            ]
        response.stop_reason = 'tool_use'
    else:
        response.content = [SimpleNamespace(type='text', text=plan.answer(evidence))]
        response.stop_reason = 'end_turn'
    return response
