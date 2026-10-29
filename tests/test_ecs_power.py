import unittest
from unittest.mock import patch
from scripts import ecs_power as power


class StartupTests(unittest.TestCase):
    @patch.object(power, 'credentials', return_value=('id', 'secret', 'token'))
    @patch.object(power.time, 'sleep')
    @patch.object(power.ctl, 'call')
    def test_stopped_instance_starts_once_and_waits(self, call, sleep, credentials):
        def status(value):
            return {'InstanceStatuses': {'InstanceStatus': [{'Status': value}]}}
        call.side_effect = [status('Stopped'), {}, status('Starting'), status('Running')]
        power.ensure_running()
        self.assertEqual([c.args[0] for c in call.call_args_list],
                         ['DescribeInstanceStatus', 'StartInstance',
                          'DescribeInstanceStatus', 'DescribeInstanceStatus'])

    @patch.object(power, 'credentials', return_value=('id', 'secret', 'token'))
    @patch.object(power.ctl, 'call')
    def test_running_instance_is_not_started_again(self, call, credentials):
        call.return_value = {'InstanceStatuses': {'InstanceStatus': [{'Status': 'Running'}]}}
        power.ensure_running()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], 'DescribeInstanceStatus')


if __name__ == '__main__':
    unittest.main()
