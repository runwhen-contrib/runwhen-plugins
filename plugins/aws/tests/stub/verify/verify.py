#!/usr/bin/env python3
"""Check the canned CLI output of the stub scenarios against the real, pinned aws CLI (plan C7).

Runs INSIDE the aws@1 runtime image (see verify.sh). For each call of each scenario it starts a
fake HTTP endpoint that answers the way the AWS API does (the service's own wire protocol), runs the
real `aws` against it with `--endpoint-url`, and compares what the CLI printed with the canned
file. A canned file is a CLI output after the skill's `--query`, so the fake serves the API response
that projection would come from (`RAW` below turns the canned file back into one, adding the fields
the projection must drop, such as a function's environment variables). A match proves the shape,
the padding, the `null`s, the number formatting and the exit code are the CLI's, not typed from
memory. Failure scenarios are served as the API's error bodies and compared with the CLI's stderr
and exit code.

    verify.py [--write] [scenario ...]      --write replaces a canned file with the CLI's output

Standard library only. Calls it cannot serve (paged lists whose token is the CLI's own) are skipped
and said so.
"""

import json
import os
import re
import glob
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STUB = Path(__file__).resolve().parent.parent
SCENARIOS = STUB / "scenarios"
FAKE_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_PAGER": "",
}
ERROR = re.compile(
    r"An error occurred \((?P<code>[A-Za-z0-9_.]+)\) when calling the (?P<op>\w+) operation"
    r"(?: \(reached max retries: (?P<retries>\d+)\))?: (?P<message>.*)"
)


def epoch(value):
    """CloudWatch's JSON protocol carries a timestamp as epoch seconds."""
    if isinstance(value, dict):
        return {
            k: datetime.fromisoformat(v).timestamp() if k == "Timestamp" else epoch(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [epoch(v) for v in value]
    return value


# --- canned CLI output -> the API response it was projected from -------------------------------------


def drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def raw_list_functions(c):
    fns = []
    for f in c["Functions"]:
        fns.append(
            {
                **drop_none(f),
                "Role": "arn:aws:iam::123456789012:role/svc",
                "Handler": "app.handler",
                "CodeSize": 4096,
                "Environment": {"Variables": {"DB_PASSWORD": "must-not-reach-the-output"}},
            }
        )
    return {"Functions": fns}


def raw_get_function_configuration(c):
    raw = drop_none(
        {
            k: c.get(k)
            for k in (
                "FunctionName", "FunctionArn", "Runtime", "PackageType", "Handler", "MemorySize",
                "Timeout", "Architectures", "State", "StateReasonCode", "StateReason",
                "LastUpdateStatus", "LastUpdateStatusReasonCode", "LastUpdateStatusReason",
                "LastModified", "CodeSize", "Version",
            )
        }
    )  # fmt: skip
    if c.get("EphemeralStorageMb") is not None:
        raw["EphemeralStorage"] = {"Size": c["EphemeralStorageMb"]}
    raw["Layers"] = [
        {"Arn": f"arn:aws:lambda:eu-west-1:123456789012:layer:l{i}:1"}
        for i in range(c.get("LayerCount") or 0)
    ]
    if c.get("SubnetCount"):
        raw["VpcConfig"] = {
            "SubnetIds": [f"subnet-{i:08x}" for i in range(c["SubnetCount"])],
            "SecurityGroupIds": [f"sg-{i:08x}" for i in range(c.get("SecurityGroupCount") or 0)],
        }
    if c.get("DeadLetterTargetArn"):
        raw["DeadLetterConfig"] = {"TargetArn": c["DeadLetterTargetArn"]}
    if c.get("TracingMode"):
        raw["TracingConfig"] = {"Mode": c["TracingMode"]}
    logging = drop_none({"LogGroup": c.get("LogGroup"), "LogFormat": c.get("LogFormat")})
    if logging:
        raw["LoggingConfig"] = logging
    env = {"Variables": {"DB_PASSWORD": "must-not-reach-the-output"}}
    if c.get("EnvironmentErrorCode"):
        env["Error"] = drop_none(
            {"ErrorCode": c["EnvironmentErrorCode"], "Message": c.get("EnvironmentErrorMessage")}
        )
    raw["Environment"] = env
    raw["Role"] = "arn:aws:iam::123456789012:role/svc"
    return raw


def raw_provisioned(c):
    names = {
        "FunctionArn": "FunctionArn",
        "Requested": "RequestedProvisionedConcurrentExecutions",
        "Available": "AvailableProvisionedConcurrentExecutions",
        "Allocated": "AllocatedProvisionedConcurrentExecutions",
        "Status": "Status",
        "StatusReason": "StatusReason",
    }
    rows = [
        {names[k]: v for k, v in row.items() if v is not None}
        for row in c["ProvisionedConcurrencyConfigs"]
    ]
    return {"ProvisionedConcurrencyConfigs": rows}


def raw_logs(c):
    events = [
        {**e, "ingestionTime": e["timestamp"] + 5, "eventId": f"{i:056d}"}
        for i, e in enumerate(c["events"])
    ]
    raw = {"events": events, "searchedLogStreams": []}
    if c.get("NextToken"):
        raw["nextToken"] = "more"
    return raw


def raw_same(c):
    return c


# --- EC2: the API response a projection came from, encoded from the CLI's own service model ---------------
# EC2 answers in XML. The model in the CLI's botocore (service-2.json) names every member, its
# locationName and its shape, so one generic encoder turns the API-shaped dict a builder makes into
# the XML the real CLI parses, instead of hand-writing tags for each field.

_MODEL = None


def ec2_model():
    global _MODEL
    if _MODEL is None:
        path = glob.glob(
            "/usr/local/aws-cli/v2/*/dist/awscli/botocore/data/ec2/2016-11-15/service-2.json"
        )[0]
        _MODEL = json.loads(Path(path).read_text())
    return _MODEL


def xml_text(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_value(shape_name, value, name):
    """The element `name` holding `value` of the model shape `shape_name`."""
    shapes = ec2_model()["shapes"]
    shape = shapes[shape_name]
    kind = shape["type"]
    if kind == "structure":
        inner = "".join(
            xml_value(shape["members"][k]["shape"], v, shape["members"][k].get("locationName", k))
            if not shapes[shape["members"][k]["shape"]].get("flattened")
            else xml_flat(
                shape["members"][k]["shape"], v, shape["members"][k].get("locationName", k)
            )
            for k, v in value.items()
        )
        return f"<{name}>{inner}</{name}>"
    if kind == "list":
        item = shape["member"].get("locationName", "item")
        return (
            f"<{name}>"
            + "".join(xml_value(shape["member"]["shape"], v, item) for v in value)
            + f"</{name}>"
        )
    if kind == "timestamp":
        moment = datetime.fromisoformat(value)
        return (
            f"<{name}>{moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</{name}>"
        )
    if kind == "boolean":
        return f"<{name}>{'true' if value else 'false'}</{name}>"
    return f"<{name}>{xml_text(value)}</{name}>"


def xml_flat(shape_name, value, name):
    shape = ec2_model()["shapes"][shape_name]
    return "".join(xml_value(shape["member"]["shape"], v, name) for v in value)


def ec2_response(op, raw):
    """<OpResponse> for an API-shaped dict, members named and ordered by the operation's output
    shape."""
    model = ec2_model()
    output = model["operations"][op]["output"]["shape"]
    shape = model["shapes"][output]
    body = "".join(
        xml_value(shape["members"][k]["shape"], v, shape["members"][k].get("locationName", k))
        for k, v in raw.items()
    )
    return (
        f'<{op}Response xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
        f"<requestId>req-1</requestId>{body}</{op}Response>"
    )


def raw_describe_instances(c):
    """describe-instances from either projection: one instance (instance-config: OwnerId and
    Instance) or a list (discover: Instances). Adds what each projection must drop: the key pair
    name, public DNS and, for the list, tags other than Name."""
    if "Instance" in c:
        reservations = [(c["OwnerId"], [full_instance(c["Instance"])])]
    else:
        reservations = [("123456789012", [list_instance(i)]) for i in c["Instances"]]
    return {
        "Reservations": [
            {"ReservationId": f"r-{n:017x}", "OwnerId": owner, "Instances": instances}
            for n, (owner, instances) in enumerate(reservations, 1)
        ]
    }


def list_instance(row):
    tags = [{"Key": "team", "Value": "payments"}]
    if row.get("Name"):
        tags.insert(0, {"Key": "Name", "Value": row["Name"]})
    return drop_none(
        {
            "InstanceId": row["InstanceId"],
            "InstanceType": row["InstanceType"],
            "State": {"Code": STATE_CODES[row["State"]], "Name": row["State"]},
            "LaunchTime": row["LaunchTime"],
            "Placement": {"AvailabilityZone": row["AvailabilityZone"], "Tenancy": "default"},
            "KeyName": "ops-keypair",
            "PublicDnsName": "ec2-203-0-113-10.eu-west-1.compute.amazonaws.com",
            "Tags": tags,
        }
    )


STATE_CODES = {
    "pending": 0,
    "running": 16,
    "shutting-down": 32,
    "terminated": 48,
    "stopping": 64,
    "stopped": 80,
}


def full_instance(c):
    """The API's Instance for the instance-config projection (the inverse of INSTANCE_QUERY)."""
    raw = {
        "InstanceId": c["InstanceId"],
        "InstanceType": c["InstanceType"],
        "State": {"Code": c["StateCode"], "Name": c["State"]},
        "StateTransitionReason": c["StateTransitionReason"],
        "LaunchTime": c["LaunchTime"],
        "Placement": drop_none({"AvailabilityZone": c["AvailabilityZone"], "Tenancy": c["Tenancy"]}),
        "Architecture": c["Architecture"],
        "PlatformDetails": c["PlatformDetails"],
        "VirtualizationType": c["VirtualizationType"],
        "InstanceLifecycle": c["InstanceLifecycle"],
        "EbsOptimized": c["EbsOptimized"],
        "Monitoring": {"State": c["Monitoring"]},
        "RootDeviceType": c["RootDeviceType"],
        "ImageId": c["ImageId"],
        "VpcId": c["VpcId"],
        "SubnetId": c["SubnetId"],
        "PrivateIpAddress": c["PrivateIpAddress"],
        "PublicIpAddress": c["PublicIpAddress"],
        "KeyName": "ops-keypair",
        "PublicDnsName": "ec2-203-0-113-10.eu-west-1.compute.amazonaws.com",
        "MetadataOptions": drop_none({"HttpTokens": c["HttpTokens"], "HttpEndpoint": c["HttpEndpoint"]}),
        "SecurityGroups": [
            {"GroupId": g, "GroupName": f"sg-name-{n}"} for n, g in enumerate(c["SecurityGroupIds"] or [])
        ],
        "NetworkInterfaces": [
            {"NetworkInterfaceId": f"eni-{n:017x}", "Status": "in-use"}
            for n in range(c["NetworkInterfaceCount"])
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": b["DeviceName"],
                "Ebs": drop_none({
                    "VolumeId": b["VolumeId"], "Status": b["Status"],
                    "DeleteOnTermination": b["DeleteOnTermination"], "AttachTime": b["AttachTime"],
                }),
            }
            for b in (c["BlockDevices"] or [])
        ],
        "Tags": [drop_none(t) for t in (c["Tags"] or [])],
    }  # fmt: skip
    if c.get("IamInstanceProfileArn"):
        raw["IamInstanceProfile"] = {"Arn": c["IamInstanceProfileArn"], "Id": "AIPAEXAMPLEPROFILE"}
    if c.get("StateReasonCode") or c.get("StateReasonMessage"):
        raw["StateReason"] = drop_none(
            {"Code": c["StateReasonCode"], "Message": c["StateReasonMessage"]}
        )
    if not raw["Tags"]:
        del raw["Tags"]
    return drop_none(raw)


def deep_drop_none(value):
    """The API leaves out what a --query projection prints as null."""
    if isinstance(value, dict):
        return {k: deep_drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [deep_drop_none(v) for v in value]
    return value


def raw_describe_instance_status(c):
    out = []
    for row in c["InstanceStatuses"]:
        entry = {
            "InstanceId": row["InstanceId"],
            "AvailabilityZone": row["AvailabilityZone"],
            "InstanceState": {"Code": row["StateCode"], "Name": row["State"]},
            "SystemStatus": row["SystemStatus"],
            "InstanceStatus": row["InstanceStatus"],
            "AttachedEbsStatus": row.get("AttachedEbsStatus"),
            "Events": row.get("Events"),
        }
        out.append(deep_drop_none(entry))
    return {"InstanceStatuses": out}


# (service, operation) -> (protocol, builder). The builder gets the canned JSON.
RAW = {
    ("lambda", "list-functions"): ("rest-json", raw_list_functions),
    ("lambda", "get-function-configuration"): ("rest-json", raw_get_function_configuration),
    ("lambda", "get-function-concurrency"): ("rest-json", raw_same),
    ("lambda", "list-provisioned-concurrency-configs"): ("rest-json", raw_provisioned),
    ("logs", "filter-log-events"): ("json", raw_logs),
    ("cloudwatch", "get-metric-statistics"): ("json10", raw_same),
    ("sts", "get-caller-identity"): ("query", raw_same),
    ("ec2", "describe-regions"): ("ec2", raw_same),
    ("ec2", "describe-instances"): ("ec2model", raw_describe_instances),
    ("ec2", "describe-instance-status"): ("ec2model", raw_describe_instance_status),
}
STATUS = {
    "AccessDeniedException": 403, "AccessDenied": 403, "UnauthorizedOperation": 403,
    "ExpiredToken": 403, "ExpiredTokenException": 403, "ResourceNotFoundException": 404,
    "TooManyRequestsException": 429, "Throttling": 400, "ThrottlingException": 400,
    "InvalidInstanceID.NotFound": 400, "RequestLimitExceeded": 503,
}  # fmt: skip


def xml_ok(service, op, c):
    if service == "sts":
        body = "".join(f"<{k}>{c[k]}</{k}>" for k in ("Arn", "UserId", "Account"))
        return (
            f'<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
            f"<GetCallerIdentityResult>{body}</GetCallerIdentityResult>"
            f"<ResponseMetadata><RequestId>req-1</RequestId></ResponseMetadata>"
            f"</GetCallerIdentityResponse>"
        )
    items = "".join(
        f"<item><regionName>{r['RegionName']}</regionName>"
        f"<regionEndpoint>{r.get('Endpoint', 'ec2.' + r['RegionName'] + '.amazonaws.com')}</regionEndpoint>"
        f"<optInStatus>{r['OptInStatus']}</optInStatus></item>"
        for r in c["Regions"]
    )
    return (
        '<DescribeRegionsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
        f"<requestId>req-1</requestId><regionInfo>{items}</regionInfo></DescribeRegionsResponse>"
    )


def xml_error(service, code, message):
    if service == "ec2":
        return (
            f"<Response><Errors><Error><Code>{code}</Code><Message>{message}</Message></Error>"
            "</Errors><RequestID>req-1</RequestID></Response>"
        )
    return (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error><Type>Sender</Type>'
        f"<Code>{code}</Code><Message>{message}</Message></Error><RequestId>req-1</RequestId></ErrorResponse>"
    )


class Fake(BaseHTTPRequestHandler):
    answer = None  # (status, headers, body bytes), set per call
    hits = 0

    def log_message(self, *args):
        pass

    def respond(self):
        type(self).hits += 1
        if os.environ.get("VERIFY_DEBUG"):
            print("   request:", self.command, self.path, dict(self.headers), file=sys.stderr)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        status, headers, body = type(self).answer
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = respond


def answer_for(service, op, protocol, canned, error):
    if error:
        code, message = error["code"], error["message"]
        status = STATUS.get(code, 400)
        if protocol == "rest-json":
            return (
                status,
                {"x-amzn-ErrorType": code, "Content-Type": "application/json"},
                json.dumps({"Message": message}).encode(),
            )
        if protocol == "json":
            return (
                status,
                {"Content-Type": "application/x-amz-json-1.1"},
                json.dumps({"__type": code, "message": message}).encode(),
            )
        if protocol == "json10":  # CloudWatch: JSON 1.0 in query-compatible mode
            return (
                status,
                {
                    "Content-Type": "application/x-amz-json-1.0",
                    "x-amzn-query-error": f"{code};Sender",
                },
                json.dumps({"__type": code, "message": message}).encode(),
            )
        return status, {"Content-Type": "text/xml"}, xml_error(service, code, message).encode()
    raw = RAW[service, op][1](canned)
    if protocol == "rest-json":
        return 200, {"Content-Type": "application/json"}, json.dumps(raw).encode()
    if protocol == "json":
        return 200, {"Content-Type": "application/x-amz-json-1.1"}, json.dumps(raw).encode()
    if protocol == "json10":
        return 200, {"Content-Type": "application/x-amz-json-1.0"}, json.dumps(epoch(raw)).encode()
    if protocol == "ec2model":
        op_name = "".join(p.capitalize() for p in op.split("-"))
        return 200, {"Content-Type": "text/xml"}, ec2_response(op_name, raw).encode()
    return 200, {"Content-Type": "text/xml"}, xml_ok(service, op, canned).encode()


def normalise(text):
    """The CLI's own page token is opaque: compare only that there is one."""
    return re.sub(r'("NextToken": )"[^"]+"', r'\1"<token>"', text)


def main(argv):
    write = "--write" in argv
    names = [a for a in argv if not a.startswith("--")] or sorted(
        p.name for p in SCENARIOS.iterdir() if (p / "calls.yaml").exists()
    )
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    home = Path(tempfile.mkdtemp(prefix="verify-home-"))
    checked = skipped = bad = 0
    for name in names:
        directory = SCENARIOS / name
        for call in json.loads((directory / "calls.yaml").read_text())["calls"]:
            service, op = call["argv"][0], call["argv"][1]
            label = f"{name}: aws {service} {op} {call['argv'][call['argv'].index('--region') + 1]}"
            if (service, op) not in RAW:
                skipped += 1
                print(f"skip  {label} (no wire encoding here)")
                continue
            protocol = RAW[service, op][0]
            error = canned = None
            path = directory / call["stdout"] if call.get("stdout") else None
            if call["rc"] != 0:
                m = ERROR.search(call["stderr"])
                if not m:
                    skipped += 1
                    print(f"skip  {label} (rc {call['rc']}, not a service error)")
                    continue
                error = m.groupdict()
            else:
                text = path.read_text() if path else ""
                canned = json.loads(text) if text.strip() else {}
                if name == "paged" and op in ("list-functions", "describe-instances"):
                    skipped += 1
                    print(f"skip  {label} (the CLI's own page token)")
                    continue
            Fake.answer = answer_for(service, op, protocol, canned, error)
            # a run's clock is not part of a scenario: CloudWatch takes ISO 8601, Logs epoch millis
            when = "1730682000000" if service == "logs" else "2026-11-04T01:00:00Z"
            args = [when if a == "<time>" else a for a in call["argv"]]
            done = subprocess.run(
                ["aws", *args, "--output", "json", "--no-cli-pager", "--endpoint-url", url],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, **FAKE_ENV, "HOME": str(home)},
            )  # fmt: skip
            checked += 1
            if error:
                ok = done.returncode == call["rc"] and done.stderr == call["stderr"]
                got = f"rc {done.returncode} stderr {done.stderr!r}"
                want = f"rc {call['rc']} stderr {call['stderr']!r}"
            else:
                ok = done.returncode == 0 and normalise(done.stdout) == normalise(
                    path.read_text() if path else ""
                )
                got, want = (
                    done.stdout[:400] + done.stderr[:300],
                    (path.read_text() if path else "")[:400],
                )
            if ok:
                print(f"ok    {label}")
            elif write and not error and done.returncode == 0:
                path.write_text(done.stdout)
                print(f"WROTE {label}")
            else:
                bad += 1
                print(f"DIFF  {label}\n   cli:    {got}\n   canned: {want}")
    print(f"checked {checked}, skipped {skipped}, different {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
