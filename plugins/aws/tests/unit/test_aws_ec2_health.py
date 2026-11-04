"""aws-ec2-health: instance-config, instance-status and instance-metrics through their scripts, with
aws answered by a fake and by the CLI stub. The classic bundle's false passes are the point: a
failed CloudWatch call, an instance that does not exist, a stopped instance with no datapoints and a
threshold of 0.8 percent on a 0 to 100 scale must not read as a healthy or a busy fleet; and tags
that look like secrets, user data and key pairs must never be output."""

import json
import time
from datetime import UTC, datetime

import jsonschema
import pytest
import yaml
from aws_ec2_health import instance as inst
from aws_ec2_health import metrics as mx
from aws_ec2_health import status as st
from aws_ec2_health.target import ec2_target
from aws_ec2_health.text import MAX_TEXT_CHARS, clip, looks_secret
from conftest import INSTANCE, PLUGIN, iam_action

SKILL = PLUGIN / "skills" / "aws-ec2-health"
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
ACCT = "123456789012"
REGION = "eu-west-1"
ID = "i-0a1b2c3d4e5f60718"


def err(code, op, message="boom", retries=None):
    tail = f" (reached max retries: {retries})" if retries is not None else ""
    return f"\naws: [ERROR]: An error occurred ({code}) when calling the {op} operation{tail}: {message}\n"


DENIED = err(
    "UnauthorizedOperation",
    "DescribeInstances",
    "You are not authorized to perform this operation.",
)
NOT_FOUND = err(
    "InvalidInstanceID.NotFound", "DescribeInstances", f"The instance ID '{ID}' does not exist"
)
THROTTLED = err(
    "RequestLimitExceeded", "DescribeInstanceStatus", "Request limit exceeded.", retries=2
)
CW_THROTTLED = err("Throttling", "GetMetricStatistics", "Rate exceeded", retries=2)
CW_DENIED = err(
    "AccessDenied",
    "GetMetricStatistics",
    "User: x is not authorized to perform: cloudwatch:GetMetricStatistics",
)


def config(**kw):
    """The CLI's output of describe-instances after --query (what read_instance sees)."""
    base = {
        "InstanceId": ID, "InstanceType": "t3.medium", "State": "running", "StateCode": 16,
        "StateReasonCode": None, "StateReasonMessage": None, "StateTransitionReason": "",
        "LaunchTime": "2026-10-20T08:15:42+00:00", "AvailabilityZone": "eu-west-1a",
        "Tenancy": "default", "Architecture": "x86_64", "PlatformDetails": "Linux/UNIX",
        "VirtualizationType": "hvm", "InstanceLifecycle": None, "EbsOptimized": True,
        "Monitoring": "disabled", "RootDeviceType": "ebs", "ImageId": "ami-0abcdef1234567890",
        "IamInstanceProfileArn": f"arn:aws:iam::{ACCT}:instance-profile/web",
        "VpcId": "vpc-0123456789abcdef0", "SubnetId": "subnet-0123456789abcdef0",
        "PrivateIpAddress": "10.0.12.34", "PublicIpAddress": "203.0.113.25",
        "SecurityGroupIds": ["sg-0123456789abcdef0"], "NetworkInterfaceCount": 1,
        "HttpTokens": "required", "HttpEndpoint": "enabled",
        "BlockDevices": [
            {"DeviceName": "/dev/xvda", "VolumeId": "vol-0a1b2c3d4e5f60718", "Status": "attached",
             "DeleteOnTermination": True, "AttachTime": "2026-10-20T08:15:46+00:00"}
        ],
        "Tags": [{"Key": "Name", "Value": "orders-web-1"}, {"Key": "env", "Value": "prod"}],
    }  # fmt: skip
    return {"OwnerId": ACCT, "Instance": {**base, **kw}}


def describe(fake, **kw):
    return fake.on("ec2", "describe-instances", body=config(**kw))


OK = {"Details": [{"Name": "reachability", "Status": "passed"}], "Status": "ok"}
IMPAIRED = {
    "Details": [
        {"Name": "reachability", "Status": "failed", "ImpairedSince": "2026-11-04T00:41:00+00:00"}
    ],
    "Status": "impaired",
}
NA = {
    "Details": [{"Name": "reachability", "Status": "insufficient-data"}],
    "Status": "not-applicable",
}


def status_row(**kw):
    row = {
        "InstanceId": ID, "AvailabilityZone": "eu-west-1a", "State": "running", "StateCode": 16,
        "SystemStatus": OK, "InstanceStatus": OK, "AttachedEbsStatus": OK, "Events": [],
    }  # fmt: skip
    return {"InstanceStatuses": [{**row, **kw}], "NextToken": None}


def status_answer(fake, **kw):
    return fake.on("ec2", "describe-instance-status", body=status_row(**kw))


def points(metric, rows):
    return {
        "Label": metric,
        "Datapoints": [{"Timestamp": "2026-11-04T11:05:00+00:00", **row} for row in rows],
    }


def metrics_for(fake, burstable=True, **overrides):
    """Answers the get-metric-statistics calls; overrides: Metric=(rc, stdout, stderr) or a body."""
    bodies = {
        "CPUUtilization": points("CPUUtilization", [
            {"SampleCount": 5.0, "Average": 20.0, "Maximum": 40.0},
            {"SampleCount": 15.0, "Average": 60.0, "Maximum": 75.5},
        ]),
        "NetworkIn": points("NetworkIn", [{"Sum": 1000.0}, {"Sum": 2000.0}]),
        "NetworkOut": points("NetworkOut", [{"Sum": 500.0}]),
        "EBSReadBytes": points("EBSReadBytes", [{"Sum": 4096.0}]),
        "EBSWriteBytes": points("EBSWriteBytes", [{"Sum": 8192.0}]),
        "EBSReadOps": points("EBSReadOps", [{"Sum": 4.0}]),
        "EBSWriteOps": points("EBSWriteOps", [{"Sum": 8.0}]),
        "CPUCreditBalance": points("CPUCreditBalance", [
            {"Average": 120.0, "Minimum": 100.0},
            {"Average": 110.0, "Minimum": 90.5},
        ]),
        "CPUCreditUsage": points("CPUCreditUsage", [{"Sum": 3.5}, {"Sum": 1.25}]),
    }  # fmt: skip
    for metric, body in bodies.items():
        got = overrides.get(metric, body)
        rc, out, stderr = got if isinstance(got, tuple) else (0, json.dumps(got), "")
        fake.on(
            "cloudwatch", "get-metric-statistics", "--namespace", "AWS/EC2", "--metric-name",
            metric, rc=rc, stdout=out, stderr=stderr,
        )  # fmt: skip


def run(run_script, script, **kw):
    return run_script(
        "aws-ec2-health", script, resource=kw.pop("resource", INSTANCE), now=NOW, **kw
    )


def codes(envelope):
    return [e["code"] for e in envelope["errors"]]


def scoped(envelope):
    return [(e["code"], e.get("scope")) for e in envelope["errors"]]


# --- instance-config ---------------------------------------------------------------------------------


def test_instance_config_reports_what_ec2_says_as_plain_facts(run_script, fake_aws):
    describe(fake_aws)
    env = run(run_script, "instance_config").envelope
    assert env["status"] == "ok" and env["errors"] == []
    i = env["outputs"]["instance"]
    assert (i["id"], i["region"], i["state"], i["instanceType"], i["burstable"]) == (
        ID, REGION, {"name": "running", "code": 16}, "t3.medium", True,
    )  # fmt: skip
    assert i["arn"] == f"arn:aws:ec2:{REGION}:{ACCT}:instance/{ID}"
    assert i["name"] == "orders-web-1" and i["lifecycle"] == "on-demand"
    assert i["imdsTokensRequired"] is True and i["monitoring"] == "disabled"
    assert i["blockDevices"][0]["volumeId"] == "vol-0a1b2c3d4e5f60718"
    assert (i["blockDeviceCount"], i["tagCount"], i["tagsRedacted"]) == (1, 2, 0)
    assert i["securityGroupIds"] == ["sg-0123456789abcdef0"] and i["networkInterfaceCount"] == 1


def test_instance_config_reports_a_stopped_instance_and_why(run_script, fake_aws):
    describe(
        fake_aws, State="stopped", StateCode=80, StateReasonCode="Client.UserInitiatedShutdown",
        StateReasonMessage="Client.UserInitiatedShutdown: User initiated shutdown",
        StateTransitionReason="User initiated (2026-11-03 22:10:15 GMT)", PublicIpAddress=None,
    )  # fmt: skip
    i = run(run_script, "instance_config").envelope["outputs"]["instance"]
    assert i["state"] == {"name": "stopped", "code": 80}
    assert i["stateReasonCode"] == "Client.UserInitiatedShutdown"
    assert i["stateReason"].endswith("User initiated shutdown") and i["publicIpAddress"] is None


def test_the_describe_call_never_asks_for_user_data_the_key_pair_or_console_output(
    run_script, fake_aws
):
    describe(fake_aws)
    run(run_script, "instance_config")
    query = inst.INSTANCE_QUERY
    for banned in (
        "UserData",
        "KeyName",
        "KeyMaterial",
        "PublicDnsName",
        "ConsoleOutput",
        "Output",
    ):
        assert banned not in query
    assert [w[:2] for w in fake_aws.words()] == [["ec2", "describe-instances"]]
    # nothing but the one describe call: no describe-instance-attribute (user data), no
    # get-console-output
    assert not [
        w for w in fake_aws.words() if w[1] in ("describe-instance-attribute", "get-console-output")
    ]


def test_a_secret_looking_tag_is_listed_by_key_without_its_value(run_script, fake_aws):
    tags = [
        {"Key": "Name", "Value": "orders-batch-2"},
        {"Key": "db_password", "Value": "hunter2"},
        {"Key": "owner", "Value": "AKIAIOSFODNN7EXAMPLE"},
        {"Key": "deploy", "Value": "https://svc:s3cr3t@git.example.com/repo.git"},
        {"Key": "build", "Value": "6f0b3c2a9d1e4b7a8c5d2e1f0a9b8c7d6e5f4a3b"},
        {"Key": "note", "Value": "token=abc123"},
        {"Key": "env", "Value": "prod"},
    ]
    describe(fake_aws, Tags=tags)
    env = run(run_script, "instance_config").envelope
    i = env["outputs"]["instance"]
    by_key = {t["key"]: t for t in i["tags"]}
    for key in ("db_password", "owner", "deploy", "build", "note"):
        assert by_key[key] == {"key": key, "value": None, "redacted": True}, key
    assert by_key["env"] == {"key": "env", "value": "prod", "redacted": False}
    assert (i["tagCount"], i["tagsRedacted"], i["name"]) == (7, 5, "orders-batch-2")
    shown = json.dumps(env)
    for value in ("hunter2", "AKIAIOSFODNN7EXAMPLE", "s3cr3t", "6f0b3c2a9d1e4b7a", "abc123"):
        assert value not in shown


def test_a_name_tag_that_looks_like_a_secret_is_not_the_name(run_script, fake_aws):
    describe(fake_aws, Tags=[{"Key": "Name", "Value": "AKIAIOSFODNN7EXAMPLE"}])
    i = run(run_script, "instance_config").envelope["outputs"]["instance"]
    assert i["name"] is None and i["tags"][0]["redacted"] is True


@pytest.mark.parametrize(
    "key, value, secret",
    [
        ("Name", "orders-web-1", False),
        ("Name", "eks-prod-nodegroup-main-Node-1A2B3C4D5E6F7G8H9I0J-instance", False),
        (
            "aws:cloudformation:stack-id",
            f"arn:aws:cloudformation:{REGION}:{ACCT}:stack/orders/6f0b3c2a-1d4e-4b7a-8c5d-2e1f0a9b8c7d",
            False,
        ),
        ("aws:ec2launchtemplate:id", "lt-0123456789abcdef0", False),
        ("cost-center", "1234", False),
        ("API_KEY", "x", True),
        ("Password", "x", True),
        ("sas-token", "x", True),
        ("secretName", "x", True),
        ("x", "-----BEGIN RSA PRIVATE KEY-----", True),
        ("x", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc", True),
        ("x", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", True),
        ("x", "Bearer abcdefgh12345678", True),
        ("x", "password: hunter2", True),
    ],
)
def test_the_tag_secret_check(key, value, secret):
    assert looks_secret(key, value) is secret


def test_tags_and_lists_are_bounded(run_script, fake_aws):
    tags = [{"Key": f"k{n}", "Value": "v"} for n in range(80)]
    devices = [
        {"DeviceName": f"/dev/sd{n}", "VolumeId": f"vol-{n:017x}", "Status": "attached",
         "DeleteOnTermination": False, "AttachTime": None}
        for n in range(60)
    ]  # fmt: skip
    describe(
        fake_aws, Tags=tags, BlockDevices=devices, SecurityGroupIds=[f"sg-{n}" for n in range(70)]
    )
    i = run(run_script, "instance_config").envelope["outputs"]["instance"]
    assert len(i["tags"]) == 50 and i["tagCount"] == 80
    assert len(i["blockDevices"]) == 50 and i["blockDeviceCount"] == 60
    assert len(i["securityGroupIds"]) == 50


def test_long_reason_text_is_cut_to_1024_characters(run_script, fake_aws):
    describe(fake_aws, StateReasonMessage="x " * 2000)
    i = run(run_script, "instance_config").envelope["outputs"]["instance"]
    assert len(i["stateReason"]) <= MAX_TEXT_CHARS and i["textTruncated"] is True
    assert clip("short") == ("short", False)


@pytest.mark.parametrize("itype, burstable", [
    ("t2.micro", True), ("t3.medium", True), ("t3a.large", True), ("t4g.small", True),
    ("m5.large", False), ("c6i.xlarge", False), ("t1.micro", False), ("tx.large", False),
])  # fmt: skip
def test_burstable_is_the_t_family(itype, burstable):
    assert inst.is_burstable(itype) is burstable


def test_an_instance_of_another_account_is_not_found(run_script, fake_aws):
    fake_aws.on("ec2", "describe-instances", body={**config(), "OwnerId": "210987654321"})
    env = run(run_script, "instance_config").envelope
    assert env["status"] == "failed" and codes(env) == ["not_found"]
    assert "210987654321" in env["errors"][0]["message"]


@pytest.mark.parametrize("script", ["instance_config", "instance_status", "instance_metrics"])
@pytest.mark.parametrize(
    "stderr, code",
    [
        (DENIED, "permission_denied"),
        (NOT_FOUND, "not_found"),
        (THROTTLED, "rate_limited"),
        (err("InvalidInstanceID.Malformed", "DescribeInstances", "Invalid id"), "invalid_input"),
        (
            '\nCould not connect to the endpoint URL: "https://ec2.eu-west-1.amazonaws.com/"\n',
            "unreachable",
        ),
        (
            '\nUnable to locate credentials. You can configure credentials by running "aws login".\n',
            "permission_denied",
        ),
        (
            err(
                "ExpiredToken",
                "DescribeInstances",
                "The security token included in the request is expired",
            ),
            "permission_denied",
        ),
    ],
)
def test_a_failed_read_fails_the_task_with_the_classified_code_never_an_empty_instance(
    run_script, fake_aws, script, stderr, code
):
    fake_aws.on("ec2", "describe-instances", rc=254, stderr=stderr)
    fake_aws.on("ec2", "describe-instance-status", rc=254, stderr=stderr)
    env = run(run_script, script).envelope
    assert env["status"] == "failed" and codes(env) == [code]
    assert env["outputs"] == {}


def test_a_response_without_the_instance_is_internal(run_script, fake_aws):
    fake_aws.on("ec2", "describe-instances", body={"OwnerId": None, "Instance": None})
    env = run(run_script, "instance_config").envelope
    assert env["status"] == "failed" and codes(env) == ["internal"]


def test_a_target_that_is_not_an_instance_is_invalid_input_before_any_call(run_script, fake_aws):
    other = {**INSTANCE, "type": "aws/lambda-function"}
    for script in ("instance_config", "instance_status", "instance_metrics"):
        env = run(run_script, script, resource=other).envelope
        assert env["status"] == "failed" and codes(env) == ["invalid_input"]
    assert fake_aws.calls == []


@pytest.mark.parametrize(
    "name",
    ["", "i-123", "i-0A1B2C3D4E5F60718", "i-0a1b2c3d4e5f6071", "web-1", "i-0a1b2c3d4e5f60718; rm"],
)
def test_a_malformed_instance_id_is_refused_before_any_call(run_script, fake_aws, name):
    env = run(run_script, "instance_config", resource={**INSTANCE, "name": name}).envelope
    assert env["status"] == "failed" and codes(env) == ["invalid_input"]
    assert fake_aws.calls == []


def test_the_old_eight_digit_id_is_an_instance_id():
    target = ec2_target({**INSTANCE, "name": "i-0a1b2c3d"})
    assert target.instance_id == "i-0a1b2c3d"


@pytest.mark.parametrize("account", ["12345", "12345678901a", "", "1234567890123"])
def test_a_target_without_a_12_digit_account_is_invalid_input_before_any_call(
    run_script, fake_aws, account
):
    resource = {k: v for k, v in INSTANCE.items() if k != "account"}
    resource["path"] = f"aws/accounts/{account}/regions/eu-west-1/ec2-instances/{ID}"
    env = run(run_script, "instance_config", resource=resource).envelope
    assert env["status"] == "failed" and codes(env) == ["invalid_input"]
    assert fake_aws.calls == []


def test_region_and_account_come_from_the_path_when_the_resource_has_no_such_field(
    run_script, fake_aws
):
    describe(fake_aws)
    resource = {k: v for k, v in INSTANCE.items() if k not in ("region", "account")}
    run(run_script, "instance_config", resource=resource)
    assert [c["region"] for c in fake_aws.calls] == [REGION]


@pytest.mark.parametrize("region, partition", [
    ("eu-west-1", "aws"), ("cn-north-1", "aws-cn"), ("us-gov-west-1", "aws-us-gov"),
    ("eusc-de-east-1", "aws-eusc"),
])  # fmt: skip
def test_the_arn_partition_follows_the_region(region, partition):
    target = ec2_target({**INSTANCE, "region": region})
    assert target.arn == f"arn:{partition}:ec2:{region}:{ACCT}:instance/{ID}"


# --- instance-status ---------------------------------------------------------------------------------


def test_instance_status_reports_the_checks_and_no_events_for_a_healthy_instance(
    run_script, fake_aws
):
    status_answer(fake_aws)
    env = run(run_script, "instance_status").envelope
    assert env["status"] == "ok" and env["errors"] == []
    c = env["outputs"]["checks"]
    assert c["systemStatus"]["status"] == "ok" and c["instanceStatus"]["status"] == "ok"
    assert c["attachedEbsStatus"]["status"] == "ok"
    assert (c["systemStatusImpaired"], c["instanceStatusImpaired"]) == (False, False)
    assert (c["events"], c["eventCount"], c["hasPendingEvents"]) == ([], 0, False)
    assert c["checksApply"] is True and c["state"] == {"name": "running", "code": 16}
    # the stopped instances are asked for too
    words = fake_aws.words()[0]
    assert "--include-all-instances" in words and words[words.index("--instance-ids") + 1] == ID


def test_instance_status_reports_an_impaired_check_and_a_pending_retirement(run_script, fake_aws):
    status_answer(
        fake_aws,
        SystemStatus=IMPAIRED,
        Events=[
            {"InstanceEventId": "instance-event-1", "Code": "instance-retirement",
             "Description": "The instance is running on degraded hardware",
             "NotBefore": "2026-11-12T00:00:00+00:00", "NotAfter": None,
             "NotBeforeDeadline": "2026-11-12T00:00:00+00:00"},
            {"InstanceEventId": "instance-event-2", "Code": "system-reboot",
             "Description": "[Completed] Scheduled system maintenance reboot",
             "NotBefore": "2026-10-30T02:00:00+00:00", "NotAfter": None, "NotBeforeDeadline": None},
            {"InstanceEventId": "instance-event-3", "Code": "instance-stop",
             "Description": "[Canceled] The instance is scheduled to stop",
             "NotBefore": "2026-10-31T02:00:00+00:00", "NotAfter": None, "NotBeforeDeadline": None},
        ],
    )  # fmt: skip
    c = run(run_script, "instance_status").envelope["outputs"]["checks"]
    assert c["systemStatusImpaired"] is True and c["instanceStatusImpaired"] is False
    assert c["systemStatus"]["details"] == [
        {"name": "reachability", "status": "failed", "impairedSince": "2026-11-04T00:41:00+00:00"}
    ]
    assert (c["eventCount"], c["pendingEventCount"], c["hasPendingEvents"]) == (3, 1, True)
    retire, reboot, stop = c["events"]
    assert (retire["code"], retire["pending"], retire["completed"]) == (
        "instance-retirement",
        True,
        False,
    )
    assert reboot["completed"] is True and reboot["pending"] is False
    assert stop["canceled"] is True and stop["pending"] is False
    assert retire["notBeforeDeadline"] == "2026-11-12T00:00:00+00:00"


def test_checks_ec2_could_not_evaluate_are_unknown_never_passing(run_script, fake_aws):
    for summary in ("insufficient-data", "initializing", "not-applicable"):
        fake_aws.handlers.clear()
        shape = {"Details": [{"Name": "reachability", "Status": summary}], "Status": summary}
        status_answer(fake_aws, SystemStatus=shape, InstanceStatus=shape, AttachedEbsStatus=None)
        c = run(run_script, "instance_status").envelope["outputs"]["checks"]
        assert c["systemStatusImpaired"] is None and c["instanceStatusImpaired"] is None, summary
        assert c["attachedEbsStatus"] is None and c["attachedEbsStatusImpaired"] is None


def test_a_stopped_instance_has_checks_that_do_not_apply(run_script, fake_aws):
    status_answer(
        fake_aws,
        State="stopped",
        StateCode=80,
        SystemStatus=NA,
        InstanceStatus=NA,
        AttachedEbsStatus=NA,
    )
    c = run(run_script, "instance_status").envelope["outputs"]["checks"]
    assert c["checksApply"] is False and c["state"]["name"] == "stopped"
    assert c["systemStatus"]["status"] == "not-applicable"
    assert c["systemStatusImpaired"] is None  # unknown, not healthy


def test_an_answer_without_an_entry_for_the_instance_is_an_error_not_a_healthy_empty_one(
    run_script, fake_aws
):
    fake_aws.on("ec2", "describe-instance-status", body={"InstanceStatuses": [], "NextToken": None})
    env = run(run_script, "instance_status").envelope
    assert env["status"] == "failed" and codes(env) == ["internal"]


def test_a_status_answer_for_another_instance_is_not_this_ones(run_script, fake_aws):
    fake_aws.on(
        "ec2", "describe-instance-status",
        body=status_row(InstanceId="i-0b7c1d2e3f4a5b6c7"),
    )  # fmt: skip
    env = run(run_script, "instance_status").envelope
    assert env["status"] == "failed" and codes(env) == ["internal"]


def test_the_status_query_keeps_next_token_so_a_cut_list_cannot_read_as_complete(
    run_script, fake_aws
):
    assert "NextToken: NextToken" in st.STATUS_QUERY
    fake_aws.on(
        "ec2",
        "describe-instance-status",
        body={"InstanceStatuses": [status_row()["InstanceStatuses"][0]]},
    )
    env = run(run_script, "instance_status").envelope
    assert env["status"] == "failed" and codes(env) == ["internal"]


def test_events_are_cut_at_50_and_long_descriptions_to_1024(run_script, fake_aws):
    events = [
        {"InstanceEventId": f"instance-event-{n}", "Code": "system-maintenance",
         "Description": "x" * 3000 if n == 0 else "maintenance", "NotBefore": None, "NotAfter": None,
         "NotBeforeDeadline": None}
        for n in range(60)
    ]  # fmt: skip
    status_answer(fake_aws, Events=events)
    c = run(run_script, "instance_status").envelope["outputs"]["checks"]
    assert len(c["events"]) == 50 and c["eventCount"] == 60
    assert (
        len(c["events"][0]["description"]) <= MAX_TEXT_CHARS and c["events"][0]["truncated"] is True
    )


# --- instance-metrics --------------------------------------------------------------------------------


def test_instance_metrics_summarises_the_datapoints(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws)
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "ok" and env["errors"] == []
    m = env["outputs"]["metrics"]
    assert m["hasData"] is True and m["burstable"] is True and m["instanceState"] == "running"
    # the average is weighted by sample count: (5*20 + 15*60) / 20 = 50
    assert m["cpu"] == {"averagePercent": 50.0, "maxPercent": 75.5, "sampleCount": 20}
    assert m["network"] == {"inBytes": 3000, "outBytes": 500, "hasData": True}
    assert m["ebs"] == {
        "readBytes": 4096,
        "writeBytes": 8192,
        "readOps": 4,
        "writeOps": 8,
        "hasData": True,
    }
    assert m["cpuCredits"] == {"minimum": 90.5, "average": 115.0, "usage": 4.75, "hasData": True}
    assert m["datapoints"]["CPUUtilization"] == 2 and m["datapoints"]["CPUCreditUsage"] == 2
    assert m["instanceType"] == "t3.medium" and m["monitoring"] == "disabled"
    assert (m["cpuAverageAboveThreshold"], m["cpuMaxAboveThreshold"]) == (False, False)
    assert m["cpuCreditBalanceBelowThreshold"] is False


def test_the_window_comes_from_the_runs_clock_and_the_since_input(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws)
    run_ = run(run_script, "instance_metrics", since="90m")
    m = run_.envelope["outputs"]["metrics"]
    assert m["window"] == {
        "start": "2026-11-04T10:30:00Z",
        "end": "2026-11-04T12:00:00Z",
        "seconds": 5400,
    }
    call = next(c for c in fake_aws.calls if c["words"][0] == "cloudwatch")["words"]
    assert call[call.index("--start-time") + 1] == "2026-11-04T10:30:00Z"
    assert call[call.index("--end-time") + 1] == "2026-11-04T12:00:00Z"
    assert call[call.index("--dimensions") + 1] == f"Name=InstanceId,Value={ID}"
    assert call[call.index("--namespace") + 1] == "AWS/EC2"


def test_a_long_window_uses_hourly_periods_to_stay_under_the_datapoint_limit():
    assert mx.period_for(3600) == 300 and mx.period_for(3 * 86400) == 300
    assert mx.period_for(3 * 86400 + 1) == 3600 and mx.period_for(14 * 86400) == 3600


@pytest.mark.parametrize("since", ["0s", "15d", "banana", "-5m", "14d1s", ""])
def test_a_bad_since_is_invalid_input_before_any_call(run_script, fake_aws, since):
    env = run(run_script, "instance_metrics", since=since).envelope
    assert env["status"] == "failed" and codes(env) == ["invalid_input"]
    assert fake_aws.calls == []


def test_no_cpu_datapoints_is_no_data_not_zero_and_not_healthy(run_script, fake_aws):
    describe(fake_aws, State="stopped", StateCode=80, InstanceType="m5.large")
    empty = {"Label": "x", "Datapoints": []}
    metrics_for(fake_aws, **{n: empty for n, *_ in mx.METRICS})
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "ok" and env["errors"] == []
    m = env["outputs"]["metrics"]
    assert m["hasData"] is False and m["instanceState"] == "stopped"
    assert m["cpu"] == {"averagePercent": None, "maxPercent": None, "sampleCount": 0}
    assert m["network"] == {"inBytes": None, "outBytes": None, "hasData": False}
    assert m["ebs"]["readBytes"] is None and m["ebs"]["hasData"] is False
    assert m["cpuAverageAboveThreshold"] is None and m["cpuMaxAboveThreshold"] is None
    assert m["cpuCredits"] is None and m["cpuCreditBalanceBelowThreshold"] is None
    assert m["datapoints"]["CPUUtilization"] == 0 and m["datapoints"]["CPUCreditBalance"] is None


def test_a_metric_without_datapoints_is_null_never_zero_even_when_cpu_has_data(
    run_script, fake_aws
):
    describe(fake_aws)
    metrics_for(fake_aws, NetworkIn={"Label": "NetworkIn", "Datapoints": []})
    m = run(run_script, "instance_metrics").envelope["outputs"]["metrics"]
    assert m["hasData"] is True and m["network"]["inBytes"] is None
    assert m["network"]["outBytes"] == 500 and m["network"]["hasData"] is True


def test_credit_metrics_are_read_only_for_a_burstable_type(run_script, fake_aws):
    describe(fake_aws, InstanceType="m5.large")
    metrics_for(fake_aws)
    env = run(run_script, "instance_metrics").envelope
    m = env["outputs"]["metrics"]
    assert m["burstable"] is False and m["cpuCredits"] is None
    assert m["cpuCreditBalanceBelowThreshold"] is None
    asked = [w[w.index("--metric-name") + 1] for w in fake_aws.words() if w[0] == "cloudwatch"]
    assert "CPUCreditBalance" not in asked and "CPUCreditUsage" not in asked
    assert len(asked) == 7


def test_the_thresholds_are_settings_and_exclusive(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws)
    # the window's average is exactly 50 and its highest maximum exactly 75.5
    for threshold, avg, peak in (
        (50, False, True),
        (49.99, True, True),
        (75.5, False, False),
        (75.49, False, True),
    ):
        fake_aws.calls.clear()
        m = run(
            run_script, "instance_metrics", settings={"cpuUtilizationThreshold": threshold}
        ).envelope["outputs"]["metrics"]
        assert (m["cpuAverageAboveThreshold"], m["cpuMaxAboveThreshold"]) == (avg, peak), threshold
    # the lowest balance is exactly 90.5
    for threshold, low in ((90.5, False), (90.51, True), (20, False)):
        m = run(
            run_script, "instance_metrics", settings={"cpuCreditBalanceThreshold": threshold}
        ).envelope["outputs"]["metrics"]
        assert m["cpuCreditBalanceBelowThreshold"] is low, threshold


def test_the_classic_default_of_0_8_percent_is_not_reproduced(run_script, fake_aws):
    """The classic bundle compared CPUUtilization (0 to 100) with 0.8 and flagged every instance
    above 0.8 percent. The default here is 80 percent."""
    describe(fake_aws)
    metrics_for(fake_aws)
    m = run(run_script, "instance_metrics").envelope["outputs"]["metrics"]
    assert m["cpu"]["maxPercent"] > 0.8 and m["cpuMaxAboveThreshold"] is False
    assert (
        yaml.safe_load((SKILL / "tasks.yaml").read_text())["settings"]["cpuUtilizationThreshold"][
            "default"
        ]
        == 80
    )


@pytest.mark.parametrize("setting, value", [
    ("cpuUtilizationThreshold", 101), ("cpuUtilizationThreshold", -1), ("cpuUtilizationThreshold", "high"),
    ("cpuCreditBalanceThreshold", -1), ("cpuCreditBalanceThreshold", True), ("maxItems", 0),
])  # fmt: skip
def test_a_bad_setting_is_invalid_input_before_any_call(run_script, fake_aws, setting, value):
    env = run(run_script, "instance_metrics", settings={setting: value}).envelope
    assert env["status"] == "failed" and codes(env) == ["invalid_input"]
    assert fake_aws.calls == []


def test_one_failed_metric_is_a_scoped_error_and_the_rest_stand(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws, NetworkIn=(254, "", CW_THROTTLED))
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "partial" and scoped(env) == [("rate_limited", "NetworkIn")]
    m = env["outputs"]["metrics"]
    assert m["network"]["inBytes"] is None and m["network"]["outBytes"] == 500
    assert m["datapoints"]["NetworkIn"] is None and m["cpu"]["maxPercent"] == 75.5


def test_a_failed_cpu_call_leaves_the_cpu_and_its_booleans_null_not_false(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws, CPUUtilization=(254, "", CW_DENIED))
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "partial" and scoped(env) == [("permission_denied", "CPUUtilization")]
    m = env["outputs"]["metrics"]
    assert m["hasData"] is None and m["cpu"] is None
    assert m["cpuAverageAboveThreshold"] is None and m["cpuMaxAboveThreshold"] is None


def test_a_failed_credit_call_is_null_not_no_data(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws, CPUCreditBalance=(254, "", CW_DENIED))
    env = run(run_script, "instance_metrics").envelope
    m = env["outputs"]["metrics"]
    assert scoped(env) == [("permission_denied", "CPUCreditBalance")]
    assert m["cpuCredits"]["minimum"] is None and m["cpuCredits"]["hasData"] is None
    assert m["cpuCredits"]["usage"] == 4.75 and m["cpuCreditBalanceBelowThreshold"] is None


def test_when_every_metric_call_fails_the_task_fails_with_the_first_error(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws, **{n: (254, "", CW_DENIED) for n, *_ in mx.METRICS})
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "failed" and codes(env) == ["permission_denied"]
    assert env["outputs"] == {}


def test_a_missing_instance_is_not_found_not_idle(run_script, fake_aws):
    fake_aws.on("ec2", "describe-instances", rc=254, stderr=NOT_FOUND)
    metrics_for(fake_aws, **{n: {"Label": "x", "Datapoints": []} for n, *_ in mx.METRICS})
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "failed" and codes(env) == ["not_found"]
    assert [w[0] for w in fake_aws.words()] == ["ec2"]  # CloudWatch was never asked


def test_a_metric_response_without_datapoints_is_internal_not_empty(run_script, fake_aws):
    describe(fake_aws)
    metrics_for(fake_aws, CPUUtilization={"Label": "CPUUtilization"})
    env = run(run_script, "instance_metrics").envelope
    assert scoped(env) == [("internal", "CPUUtilization")]
    assert env["outputs"]["metrics"]["hasData"] is None


def test_the_metric_loop_has_a_deadline_and_the_unread_metrics_are_timeouts_not_empty(
    run_script, fake_aws, monkeypatch
):
    describe(fake_aws)
    metrics_for(fake_aws)
    monkeypatch.setattr(mx, "READ_BUDGET", 0)
    env = run(run_script, "instance_metrics").envelope
    # nothing was read: the first failure is the result
    assert env["status"] == "failed" and codes(env) == ["timeout"]
    assert [w[0] for w in fake_aws.words()] == ["ec2"]


def test_a_metric_loop_out_of_time_midway_keeps_what_it_read(run_script, fake_aws, monkeypatch):
    describe(fake_aws)
    metrics_for(fake_aws)
    clock = iter([0.0, 0.0, 0.0, 0.0, 1000.0] + [1000.0] * 40)
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    env = run(run_script, "instance_metrics").envelope
    assert env["status"] == "partial"
    assert {e["code"] for e in env["errors"]} == {"timeout"}
    m = env["outputs"]["metrics"]
    # the budget ran out after the fourth call started: CPU, NetworkIn, NetworkOut stand
    assert m["cpu"] is not None and m["datapoints"]["NetworkOut"] == 1
    assert m["datapoints"]["EBSReadBytes"] is None and m["ebs"]["hasData"] is None


def test_each_metric_call_gets_at_most_what_is_left_of_the_budget(
    run_script, fake_aws, monkeypatch
):
    describe(fake_aws)
    metrics_for(fake_aws)
    monkeypatch.setattr(mx, "READ_BUDGET", 20)
    run(run_script, "instance_metrics")
    timeouts = [c["timeout"] for c in fake_aws.calls if c["words"][0] == "cloudwatch"]
    assert timeouts and all(0 < t <= 20 for t in timeouts)


# --- what the tasks declare --------------------------------------------------------------------------


def tasks_doc():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def test_the_tasks_are_read_only_aws_tasks_with_the_credential_and_defaults():
    doc = tasks_doc()
    assert doc["runtime"] == "aws@1" and doc["defaults"]["credentials"] == "aws/sdk"
    assert doc["settings"]["maxItems"]["default"] == 200
    assert [t["name"] for t in doc["tasks"]] == [
        "instance-config",
        "instance-status",
        "instance-metrics",
    ]
    for task in doc["tasks"]:
        assert task["readOnly"] is True and (SKILL / task["script"]).is_file()
    metrics = next(t for t in doc["tasks"] if t["name"] == "instance-metrics")
    assert (
        metrics["inputs"]["since"]["default"] == "1h"
    )  # classic looked back 3 hours at 1-minute periods


def run_all_tasks(run_script, fake_aws):
    describe(fake_aws)
    status_answer(fake_aws)
    metrics_for(fake_aws)
    calls = {}
    for script in ("instance_config", "instance_status", "instance_metrics"):
        before = len(fake_aws.calls)
        assert run(run_script, script).envelope["status"] == "ok"
        calls[script] = fake_aws.calls[before:]
    return calls


def test_each_task_declares_exactly_the_iam_actions_its_calls_need(run_script, fake_aws):
    calls = run_all_tasks(run_script, fake_aws)
    for task in tasks_doc()["tasks"]:
        needed = {iam_action(c["words"]) for c in calls[task["name"].replace("-", "_")]} - {None}
        assert {p["action"] for p in task["permissions"]} == needed, task["name"]
    assert {a for c in calls.values() for a in {iam_action(x["words"]) for x in c}} == {
        "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "cloudwatch:GetMetricStatistics",
    }  # fmt: skip


def test_every_declared_permission_fits_the_permission_schema():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "aws-permission.json").read_text())
    for task in tasks_doc()["tasks"]:
        for permission in task["permissions"]:
            jsonschema.validate(permission, schema)


def test_outputs_fit_their_schemas_and_the_schemas_name_no_verdict(run_script, fake_aws):
    run_all_tasks(run_script, fake_aws)
    for task, script in (
        ("instance-config", "instance_config"),
        ("instance-status", "instance_status"),
        ("instance-metrics", "instance_metrics"),
    ):
        spec = next(t for t in tasks_doc()["tasks"] if t["name"] == task)
        out = run(run_script, script).envelope["outputs"]
        for name, decl in spec["outputs"].items():
            assert name not in {"status", "errors"}
            schema = json.loads((SKILL / decl["schema"]).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert not {"severity", "title", "nextSteps"} & set(schema["properties"])
            jsonschema.validate(out[name], schema)


def test_outputs_with_every_nullable_branch_still_fit_their_schemas(run_script, fake_aws):
    """A stopped, untagged instance with failed metric calls: the nulls the schemas must allow."""
    describe(fake_aws, State="stopped", StateCode=80, PublicIpAddress=None, Tags=None, BlockDevices=None,
             SecurityGroupIds=None, IamInstanceProfileArn=None, HttpTokens=None, HttpEndpoint=None)  # fmt: skip
    status_answer(
        fake_aws,
        State="stopped",
        StateCode=80,
        SystemStatus=NA,
        InstanceStatus=NA,
        AttachedEbsStatus=None,
    )
    metrics_for(
        fake_aws, NetworkIn=(254, "", CW_THROTTLED), CPUCreditBalance=(254, "", CW_THROTTLED)
    )
    for task, script, name in (
        ("instance-config", "instance_config", "instance"),
        ("instance-status", "instance_status", "checks"),
        ("instance-metrics", "instance_metrics", "metrics"),
    ):
        spec = next(t for t in tasks_doc()["tasks"] if t["name"] == task)
        out = run(run_script, script).envelope["outputs"][name]
        jsonschema.validate(out, json.loads((SKILL / spec["outputs"][name]["schema"]).read_text()))


def test_lib_and_scripts_never_read_the_wall_clock():
    for path in [*(SKILL / "lib").rglob("*.py"), *(SKILL / "scripts").glob("*.py")]:
        text = path.read_text()
        for banned in ("datetime.now(", "time.time(", "datetime.utcnow(", "date.today("):
            assert banned not in text, f"{path.name} reads the wall clock ({banned}); use ctx.now()"


def test_no_script_or_lib_shells_out_or_hides_a_failure():
    for path in [*(SKILL / "lib").rglob("*.py"), *(SKILL / "scripts").glob("*.py")]:
        text = path.read_text()
        for banned in ("shell=True", "2>/dev/null", "|| true", "os.system"):
            assert banned not in text, f"{path.name}: {banned}"


def test_skill_md_is_within_its_bounds():
    text = (SKILL / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == "aws-ec2-health" and len(front["description"]) <= 1024
    assert len(text.splitlines()) <= 150
    for section in (
        "When to use",
        "What each task tells you",
        "How to read the result",
        "What to try next",
    ):
        assert f"## {section}" in text


# --- against the CLI stub, as `rwtask test --record` runs it -----------------------------------------

TASKS = ("instance_config", "instance_status", "instance_metrics")
EXPECTED = {
    # scenario: {script: (status, error codes)}
    "ec2-healthy": {s: ("ok", []) for s in TASKS},
    "ec2-broken": {s: ("ok", []) for s in TASKS},
    "ec2-stopped": {s: ("ok", []) for s in TASKS},
}
RESOURCE = {
    "ec2-healthy": INSTANCE,
    "ec2-broken": {**INSTANCE, "name": "i-0b7c1d2e3f4a5b6c7"},
    "ec2-stopped": {**INSTANCE, "name": "i-0c0ffee0123456789"},
}


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_the_tasks_against_the_stub(run_with_stub, scenario):
    run_with_stub.use(scenario)
    for script, (status, wanted) in EXPECTED[scenario].items():
        run_ = run_with_stub("aws-ec2-health", script, resource=RESOURCE[scenario])
        assert run_.envelope["status"] == status, (scenario, script, run_.envelope["errors"])
        assert codes(run_.envelope) == wanted


def stub(run_with_stub, scenario, script, resource=None, **kw):
    run_with_stub.use(scenario)
    return run_with_stub("aws-ec2-health", script, resource=resource or INSTANCE, **kw).envelope


def test_the_broken_scenario_shows_what_the_tasks_detect(run_with_stub):
    broken = RESOURCE["ec2-broken"]
    i = stub(run_with_stub, "ec2-broken", "instance_config", broken)["outputs"]["instance"]
    assert (i["name"], i["instanceType"], i["monitoring"], i["imdsTokensRequired"]) == (
        "orders-batch-2", "t3.small", "enabled", False,
    )  # fmt: skip
    assert i["blockDeviceCount"] == 2 and i["tagsRedacted"] == 2
    shown = json.dumps(i)
    assert "hunter2" not in shown and "6f0b3c2a9d1e4b7a" not in shown
    c = stub(run_with_stub, "ec2-broken", "instance_status", broken)["outputs"]["checks"]
    assert (c["systemStatusImpaired"], c["instanceStatusImpaired"], c["hasPendingEvents"]) == (
        True,
        False,
        True,
    )
    assert [e["code"] for e in c["events"] if e["pending"]] == ["instance-retirement"]
    m = stub(run_with_stub, "ec2-broken", "instance_metrics", broken)["outputs"]["metrics"]
    assert (
        m["hasData"] is True
        and m["cpuAverageAboveThreshold"] is True
        and m["cpuMaxAboveThreshold"] is True
    )
    assert m["cpuCredits"]["minimum"] == 3.2 and m["cpuCreditBalanceBelowThreshold"] is True


def test_the_healthy_scenario_crosses_no_threshold(run_with_stub):
    m = stub(run_with_stub, "ec2-healthy", "instance_metrics")["outputs"]["metrics"]
    assert m["hasData"] is True and m["cpu"]["maxPercent"] == 41.0
    assert (
        m["cpuAverageAboveThreshold"],
        m["cpuMaxAboveThreshold"],
        m["cpuCreditBalanceBelowThreshold"],
    ) == (False, False, False)


def test_the_stopped_scenario_has_no_data_and_says_so(run_with_stub):
    stopped = RESOURCE["ec2-stopped"]
    m = stub(run_with_stub, "ec2-stopped", "instance_metrics", stopped)["outputs"]["metrics"]
    assert (m["instanceState"], m["hasData"], m["cpuMaxAboveThreshold"], m["burstable"]) == (
        "stopped",
        False,
        None,
        False,
    )
    c = stub(run_with_stub, "ec2-stopped", "instance_status", stopped)["outputs"]["checks"]
    assert c["checksApply"] is False and c["systemStatusImpaired"] is None
    i = stub(run_with_stub, "ec2-stopped", "instance_config", stopped)["outputs"]["instance"]
    assert i["stateReasonCode"] == "Client.UserInitiatedShutdown"


def test_the_stub_failure_scenarios(run_with_stub):
    for script in TASKS:
        env = stub(run_with_stub, "ec2-forbidden", script)
        assert env["status"] == "failed" and codes(env) == ["permission_denied"], script
        env = stub(
            run_with_stub, "ec2-not-found", script, {**INSTANCE, "name": "i-0123456789abcdef9"}
        )
        assert env["status"] == "failed" and codes(env) == ["not_found"], script
    env = stub(run_with_stub, "ec2-throttled", "instance_status")
    assert env["status"] == "failed" and codes(env) == ["rate_limited"]
    env = stub(run_with_stub, "ec2-throttled", "instance_metrics")
    assert env["status"] == "partial" and scoped(env) == [("rate_limited", "NetworkIn")]
    env = stub(run_with_stub, "ec2-metrics-forbidden", "instance_metrics")
    assert env["status"] == "failed" and codes(env) == ["permission_denied"]
