#!/usr/bin/env python3
"""Small Alibaba Cloud DNS RPC client; never prints credentials or signed URLs."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from deploy_ssh import read_config


def rpc(action: str, parameters: dict) -> dict:
    root = Path(__file__).resolve().parent.parent
    credentials = read_config(root / ".env.local")
    deploy = read_config(root / ".env.deploy")
    key_id = deploy.get("DEPLOY_ALIYUN_AK_ID") or credentials.get("ALIYUN_AK_ID", "")
    secret = deploy.get("DEPLOY_ALIYUN_AK_SECRET") or credentials.get("ALIYUN_AK_SECRET", "")
    if not key_id or not secret:
        raise ValueError("Existing RAM credentials are missing")
    params = {
        "Action": action, "Version": "2015-01-09", "Format": "JSON",
        "AccessKeyId": key_id, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureNonce": str(uuid.uuid4()), **parameters,
    }
    quote = lambda s: urllib.parse.quote(str(s), safe="~")
    canonical = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + quote(canonical)
    signature = base64.b64encode(hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
    request = urllib.request.Request("https://alidns.aliyuncs.com/?" + canonical + "&Signature=" + quote(signature))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get("Code", "HTTPError")
        except Exception:
            code = "HTTPError"
        raise RuntimeError(f"AliDNS rejected request: {code}") from None
    if "Code" in payload:
        raise RuntimeError("AliDNS rejected request: " + str(payload["Code"]))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["records", "add"])
    parser.add_argument("--rr")
    parser.add_argument("--type")
    parser.add_argument("--value")
    args = parser.parse_args()
    config = read_config(Path(__file__).resolve().parent.parent / ".env.deploy")
    domain = config["DEPLOY_DOMAIN"]
    if args.action == "records":
        page = 1
        records = []
        while True:
            result = rpc("DescribeDomainRecords", {"DomainName": domain, "PageSize": 100, "PageNumber": page})
            records.extend(result.get("DomainRecords", {}).get("Record", []))
            if len(records) >= result.get("TotalCount", 0):
                break
            page += 1
        print(json.dumps({"domain": domain, "records": records}, ensure_ascii=False, indent=2))
    else:
        if not all([args.rr, args.type, args.value]):
            parser.error("add requires --rr, --type and --value")
        result = rpc("AddDomainRecord", {"DomainName": domain, "RR": args.rr, "Type": args.type, "Value": args.value, "TTL": 600})
        print(json.dumps({"domain": domain, "rr": args.rr, "type": args.type, "value": args.value, "recordId": result.get("RecordId")}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"AliDNS request failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1)
