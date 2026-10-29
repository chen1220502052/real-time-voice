"""Credential and permission validation for the ECS power scheduler."""
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from scripts import aliyun_instance_ctl as ctl


class PowerSchedulerTests(unittest.TestCase):
    @patch.dict(ctl.os.environ, {}, clear=True)
    @patch.object(ctl.os.path, 'exists', return_value=False)
    @patch.object(ctl, 'metadata')
    def test_uses_role_credentials_without_persisting_secrets(self, metadata, exists):
        metadata.side_effect = ['role', json.dumps({
            'Code': 'Success', 'AccessKeyId': 'id',
            'AccessKeySecret': 'secret', 'SecurityToken': 'token'})]
        self.assertEqual(ctl.load_credentials(), ('id', 'secret', 'token'))

    @patch.object(ctl.urllib.request, 'urlopen')
    def test_dry_run_signs_token_and_accepts_permission_success(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            'https://example.test', 400, 'dry run', {},
            io.BytesIO(b'{"Code":"DryRunOperation"}'))
        result = ctl.call('StopInstance', 'id', 'secret', 'region', 'example.test',
                          {'InstanceId': 'instance', 'StoppedMode': 'StopCharging'},
                          dry_run=True, security_token='token+/=')
        self.assertEqual(result['Code'], 'DryRunOperation')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            urlopen.call_args.args[0].full_url).query)
        self.assertEqual(query['SecurityToken'], ['token+/='])
        self.assertEqual(query['DryRun'], ['true'])
        self.assertEqual(query['StoppedMode'], ['StopCharging'])

    @patch.object(ctl.urllib.request, 'urlopen')
    def test_denied_permission_remains_failure(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            'https://example.test', 403, 'denied', {},
            io.BytesIO(b'{"Code":"Forbidden.RAM","Message":"denied"}'))
        with self.assertRaisesRegex(SystemExit, 'Forbidden.RAM'):
            ctl.call('StopInstance', 'id', 'secret', 'region', 'example.test', {},
                     dry_run=True)


if __name__ == '__main__':
    unittest.main()
