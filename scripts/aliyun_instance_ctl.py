#!/usr/bin/env python3
"""Start/stop the Aliyun ECS demo server on a schedule (节省停机模式).

Production scheduling uses Alibaba Cloud OOS (21:00 stop / 09:00 start,
Asia/Shanghai). Legacy server cron and macOS launchd tasks were removed.
This script remains available for manual control and signed RPC calls.

Credentials: env ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET, else the
first existing file of ~/.config/aliyun-ecs.env (mac) or /etc/aliyun-ecs.env
(server), each line KEY=VALUE. On ECS, falls back to the attached RAM role using IMDSv2 temporary credentials.
The identity needs ecs:StopInstance / ecs:StartInstance as required, plus
ecs:DescribeInstanceStatus for status checks. Scope write access to this instance.

Stdlib only; RPC request signing follows the POP signature 1.0 spec.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

DEFAULT_INSTANCE = "i-j6ci77fwjtiopeqqxctu"
DEFAULT_REGION = "cn-hongkong"
VPC_ENDPOINT = "ecs-vpc.{region}.aliyuncs.com"
PUBLIC_ENDPOINT = "ecs.{region}.aliyuncs.com"
CRED_PATHS = ("~/.config/aliyun-ecs.env", "/etc/aliyun-ecs.env")


def load_credentials():
    key = os.environ.get("ALIYUN_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "").strip()
    if key and secret:
        return key, secret, os.environ.get("ALIYUN_SECURITY_TOKEN", "").strip()
    for path in CRED_PATHS:
        full = os.path.expanduser(path)
        if not os.path.exists(full):
            continue
        values = {}
        for line in open(full, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, val = line.partition("=")
                values[name.strip()] = val.strip().strip('"').strip("'")
        if values.get("ALIYUN_ACCESS_KEY_ID") and values.get("ALIYUN_ACCESS_KEY_SECRET"):
            return (values["ALIYUN_ACCESS_KEY_ID"], values["ALIYUN_ACCESS_KEY_SECRET"],
                    values.get("ALIYUN_SECURITY_TOKEN", ""))
    role = metadata("ram/security-credentials/")
    if role:
        raw = metadata("ram/security-credentials/" + urllib.parse.quote(role, safe=""))
        try:
            credentials = json.loads(raw or "{}")
            if credentials.get("Code") == "Success":
                return (credentials["AccessKeyId"], credentials["AccessKeySecret"],
                        credentials["SecurityToken"])
        except (ValueError, KeyError):
            pass
        sys.exit("could not obtain temporary ECS RAM role credentials")
    sys.exit("missing credentials: attach an ECS RAM role or configure ALIYUN_ACCESS_KEY_ID/SECRET")


def metadata(name):
    """Read ECS metadata with an IMDSv2 token; never log credential responses."""
    base = "http://100.100.100.200/latest/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(
            base + "api/token", method="PUT",
            headers={"X-aliyun-ecs-metadata-token-ttl-seconds": "60"})
        with opener.open(request, timeout=1) as response:
            token = response.read().decode().strip()
        request = urllib.request.Request(
            base + "meta-data/" + name,
            headers={"X-aliyun-ecs-metadata-token": token})
        with opener.open(request, timeout=1) as response:
            return response.read().decode().strip() or None
    except (OSError, ValueError):
        return None


def call(action, key, secret, region, endpoint, params, dry_run=False, security_token="", api_version="2014-05-26"):
    query = {
        "Format": "JSON", "Version": api_version, "AccessKeyId": key,
        "SignatureMethod": "HMAC-SHA1", "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0", "SignatureNonce": uuid.uuid4().hex,
        "Action": action, "RegionId": region,
        **params,
    }
    if security_token:
        query["SecurityToken"] = security_token
    if dry_run:
        query["DryRun"] = "true"
    encoded = sorted((urllib.parse.quote(k, safe="~"), urllib.parse.quote(str(v), safe="~"))
                     for k, v in query.items())
    canonical = "&".join(f"{k}={v}" for k, v in encoded)
    string_to_sign = "GET&%2F&" + urllib.parse.quote(canonical, safe="~")
    signature = base64.b64encode(
        hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
    url = f"https://{endpoint}/?{canonical}&Signature={urllib.parse.quote(signature, safe='~')}"
    request = urllib.request.Request(url, headers={"User-Agent": "ecs-power-scheduler/1"})
    try:
        context = ssl.create_default_context()
        # macOS Python may retain a removed Homebrew CA path. Use the trusted
        # system CA bundle when no default CA file is available.
        if sys.platform == 'darwin' and ssl.get_default_verify_paths().cafile is None:
            if os.path.isfile('/etc/ssl/cert.pem'):
                context.load_verify_locations(cafile='/etc/ssl/cert.pem')
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        try:
            detail = json.loads(body)
            if dry_run and detail.get("Code") == "DryRunOperation":
                return detail
            raise SystemExit(f"{action} failed: {detail.get('Code')} {detail.get('Message')}")
        except json.JSONDecodeError:
            raise SystemExit(f"{action} HTTP {error.code}: {body[:300]}")
    except urllib.error.URLError as error:
        raise SystemExit(f"{action} unreachable ({endpoint}): {error.reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument("--instance", default=os.environ.get("ECS_INSTANCE_ID") or metadata("instance-id") or DEFAULT_INSTANCE)
    parser.add_argument("--region", default=os.environ.get("ECS_REGION") or metadata("region-id") or DEFAULT_REGION)
    parser.add_argument("--endpoint", default=os.environ.get("ECS_ENDPOINT"),
                        help="override, e.g. ecs-vpc.cn-hongkong.aliyuncs.com from inside the VPC")
    parser.add_argument("--wait", action="store_true", help="block until the target state is reached")
    parser.add_argument("--dry-run", action="store_true", help="validate permission without changing state")
    args = parser.parse_args()

    key, secret, security_token = load_credentials()

    def request(action, params, dry_run=False):
        return call(action, key, secret, args.region, endpoint, params,
                    dry_run=dry_run, security_token=security_token)
    on_instance = metadata("instance-id") is not None
    endpoint = args.endpoint or (VPC_ENDPOINT.format(region=args.region) if on_instance
                                 else PUBLIC_ENDPOINT.format(region=args.region))
    instance = {"InstanceId": args.instance}

    if args.command == "status":
        result = request("DescribeInstanceStatus",
                      {"InstanceId.1": args.instance})
        statuses = result.get("InstanceStatuses", {}).get("InstanceStatus", [])
        print(statuses[0]["Status"] if statuses else "NotFound")
        return

    if args.command == "start":
        request("StartInstance", instance, dry_run=args.dry_run)
        print(f"start issued for {args.instance} ({'dry-run' if args.dry_run else 'real'})")
        target = "Running"
    else:
        # StopCharging = 节省停机模式: vCPU/内存停止计费, 云盘保留.
        request("StopInstance",
             {**instance, "StoppedMode": "StopCharging", "ForceStop": "false"}, dry_run=args.dry_run)
        print(f"stop (StopCharging) issued for {args.instance} ({'dry-run' if args.dry_run else 'real'})")
        target = "Stopped"

    if args.wait and not args.dry_run:
        deadline = time.time() + 300
        while time.time() < deadline:
            result = request("DescribeInstanceStatus",
                          {"InstanceId.1": args.instance})
            statuses = result.get("InstanceStatuses", {}).get("InstanceStatus", [])
            state = statuses[0]["Status"] if statuses else "NotFound"
            if state == target:
                print(f"reached {target}")
                return
            time.sleep(5)
        raise SystemExit(f"timed out waiting for {target}")


if __name__ == "__main__":
    main()
