import unittest
import urllib.error
import threading
import time
from unittest.mock import Mock

from qwen_backend import ToolkitBackend
from qwen_status import metrics_display, rate_text, PALETTE, readable_color, contrast_ratio
from qwen_control import QwenController
from test_qwen import wait_for


class MetricsTests(unittest.TestCase):
    def test_reading_generating_queue_and_idle(self):
        cases = [({'state': 'reading', 'phase': 'reading the prompt'}, 'reading'),
                 ({'state': 'generating', 'phase': 'thinking', 'tok_s': 57.6}, 'generating'),
                 ({'state': 'idle', 'queued': 2}, 'queued'),
                 ({'state': 'idle'}, 'idle'),
                 ({'state': 'reading', 'phase': 'saving the session'}, 'busy')]
        for live, phase in cases:
            with self.subTest(live=live):
                self.assertEqual(metrics_display({'live': live})['display_phase'], phase)

    def test_generation_wins_over_reading_and_queue_with_aggregate_rate(self):
        result = metrics_display({'live': {'state': 'generating', 'tok_s': 111.2, 'queued': 2,
            'slots': [{'state': 'decoding', 'tok_s': 55}, {'state': 'reading'}]}})
        self.assertEqual(result['display_phase'], 'generating')
        self.assertEqual(result['generation_tps'], 111.2)
        self.assertEqual(result['queued_count'], 2)

    def test_parallel_decoding_not_misreported_as_newest_reading_or_idle(self):
        for state in ('reading', 'idle'):
            result = metrics_display({'live': {'state': state, 'running': 2,
                'slots': [{'state': 'decoding'}, {'state': 'reading'}]}})
            self.assertTrue(result['metrics_active'])
            self.assertEqual(result['display_phase'], 'generating')
            self.assertIsNone(result['generation_tps'])  # Never use a slot mean as aggregate rate.

    def test_idle_claim_cannot_override_known_busy_count(self):
        result = metrics_display({'live': {'state': 'idle'}}, in_flight=1)
        self.assertTrue(result['metrics_active'])
        self.assertEqual(result['display_phase'], 'busy')

    def test_unloaded_pending_task_is_loading_not_prompt_reading(self):
        result = metrics_display({'live': {'state': 'unloaded', 'queued': 1}}, loaded=False, in_flight=1)
        self.assertEqual(result['display_phase'], 'loading')
        self.assertEqual(metrics_display({'live': {'state': 'unloaded'}}, loaded=False)['display_phase'], 'unloaded')

    def test_missing_endpoint_or_structure_is_not_a_fake_idle(self):
        for payload in (None, [], {}, {'live': []}):
            with self.assertRaises(ValueError):
                metrics_display(payload)
        self.assertEqual(metrics_display({'live': {}})['display_phase'], 'busy')

    def test_does_not_keep_prompt_answer_history_or_tool_name(self):
        result = metrics_display({'input': 'PRIVATE', 'requests': [{'output': 'PRIVATE'}],
            'live': {'state': 'generating', 'phase': 'writing a tool call: PRIVATE',
                     'tail': 'PRIVATE', 'prompt_read': 100, 'prompt_total': 1000}})
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result['phase_detail'], '生成工具调用')
        self.assertEqual(result['prompt_read'], 100)

    def test_invalid_speed_never_becomes_zero_or_crashes_formatting(self):
        for speed in (None, -1, True, '57.6', float('nan'), float('inf')):
            result = metrics_display({'live': {'state': 'generating', 'tok_s': speed}})
            result['state'] = 'busy'
            self.assertEqual(rate_text(result), '— T/s')

    def test_rates_are_one_decimal_and_cleared_for_each_non_generation_state(self):
        for value, text in [(57.64, '57.6 T/s'), (0, '0.0 T/s'), (-0.0, '0.0 T/s')]:
            self.assertEqual(rate_text({'state': 'busy', 'display_phase': 'generating', 'generation_tps': value}), text)
        for state in ('off', 'unloaded', 'ready'):
            self.assertEqual(rate_text({'state': state}), '0.0 T/s')
        for state in ('error', 'unconfigured', 'checking'):
            self.assertEqual(rate_text({'state': state}), '— T/s')
        for phase in ('reading', 'queued', 'loading'):
            self.assertEqual(rate_text({'state': 'busy', 'display_phase': phase,
                                       'metrics_available': True, 'generation_tps': 57.6}), '0.0 T/s')
        self.assertEqual(rate_text({'state': 'busy'}), '— T/s')

    def test_semantic_colors_are_readable_on_red_green_and_custom_backgrounds(self):
        for background in ('#fa6666', '#4CAF50', '#000000', '#ffffff', '#2c3e50'):
            rgb = tuple(int(background[i:i + 2], 16) / 255 for i in (1, 3, 5))
            colors = set()
            for phase in PALETTE:
                color = readable_color(phase, rgb)
                channels = tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
                self.assertGreaterEqual(contrast_ratio(channels, rgb), 4.5)
                colors.add(color)
            self.assertGreaterEqual(len(colors), 5)


class BackendMetricsTests(unittest.TestCase):
    def setUp(self):
        self.backend = ToolkitBackend()
        self.manager, self.lifecycle = Mock(), Mock()
        self.backend._load = Mock(return_value=(self.manager, self.lifecycle))
        self.config = {'model': 'fixture', 'base_url': 'http://127.0.0.1:8765/v1'}
        self.manager.config.return_value = self.config
        self.manager.state.return_value = {'enabled': True, 'auto_start': True}
        self.manager.probe.return_value = {'reachable': True, 'service': 'strata'}
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 0}

    def test_parallel_metrics_prevent_false_idle_and_use_only_read_requests(self):
        self.manager.request_json.return_value = {'live': {'state': 'generating', 'running': 1, 'tok_s': 57.6}}
        result = self.backend.status()
        self.assertEqual(result['state'], 'busy')
        self.assertEqual(result['generation_tps'], 57.6)
        self.manager.request_json.assert_called_once_with('http://127.0.0.1:8765/metrics', self.config, timeout=.8)

    def test_optional_metrics_failure_preserves_busy_and_hides_rate(self):
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 1}
        for error in (TimeoutError(), urllib.error.HTTPError('http://localhost', 404, '', {}, None), ValueError()):
            self.manager.request_json.side_effect = error
            result = self.backend.status()
            self.assertEqual(result['state'], 'busy')
            self.assertEqual(rate_text(result), '— T/s')

    def test_idle_or_sleep_has_no_stale_generation_rate(self):
        self.manager.request_json.return_value = {'live': {'state': 'generating', 'tok_s': 57.6}}
        self.assertEqual(self.backend.status()['generation_tps'], 57.6)
        self.manager.request_json.return_value = {'live': {'state': 'idle'}}
        self.assertEqual(rate_text(self.backend.status()), '0.0 T/s')

        self.lifecycle.service_activity.return_value = {'loaded': False, 'in_flight': 0}
        self.assertEqual(rate_text(self.backend.status()), '0.0 T/s')


class AdaptivePollingTests(unittest.TestCase):
    def test_ui_drain_does_not_lose_fast_poll_and_idle_restores_slow_poll(self):
        class Backend:
            state = 'busy'
            times = []
            def status(self):
                self.times.append(time.monotonic())
                return {'state': self.state}
        backend = Backend()
        controller = QwenController(backend, interval=3)
        try:
            wait_for(lambda: len(backend.times) >= 1)
            wait_for(lambda: controller._poll_fast)
            controller.drain_events()
            wait_for(lambda: len(backend.times) >= 2, timeout=2.5)
            self.assertLess(backend.times[1] - backend.times[0], 2.5)
            backend.state = 'off'
            self.assertTrue(controller.request('status'))
            wait_for(lambda: not controller._poll_fast)
        finally:
            controller.close()


if __name__ == '__main__':
    unittest.main()
