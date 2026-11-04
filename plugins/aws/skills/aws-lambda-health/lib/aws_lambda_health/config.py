"""function-config: what Lambda says about one function (get-function-configuration).

The call is narrowed with --query to the fields below. The raw response also holds the function's
environment variables (names and values), which no task here needs or keeps.

Every call passes the function's full ARN (built from the run's target, see target.py) as
`--function-name` and names no qualifier, so what is read is the function's `$LATEST`: published
versions and aliases have their own configuration, concurrency and metrics and are not read.
Reserved and provisioned concurrency are two further reads that may be forbidden on their own: a
failure of either is reported by the caller as an error scoped to it and the configuration stands."""

from .awscli import aws_json, aws_paged
from .errors import RwError
from .logtext import clip

CONFIG_QUERY = (
    "{FunctionName: FunctionName, FunctionArn: FunctionArn, Runtime: Runtime, "
    "PackageType: PackageType, Handler: Handler, MemorySize: MemorySize, Timeout: Timeout, "
    "EphemeralStorageMb: EphemeralStorage.Size, Architectures: Architectures, State: State, "
    "StateReasonCode: StateReasonCode, StateReason: StateReason, "
    "LastUpdateStatus: LastUpdateStatus, "
    "LastUpdateStatusReasonCode: LastUpdateStatusReasonCode, "
    "LastUpdateStatusReason: LastUpdateStatusReason, LastModified: LastModified, "
    "CodeSize: CodeSize, Version: Version, LayerCount: length(Layers || `[]`), "
    "SubnetCount: length(VpcConfig.SubnetIds || `[]`), "
    "SecurityGroupCount: length(VpcConfig.SecurityGroupIds || `[]`), "
    "DeadLetterTargetArn: DeadLetterConfig.TargetArn, TracingMode: TracingConfig.Mode, "
    "LogGroup: LoggingConfig.LogGroup, LogFormat: LoggingConfig.LogFormat, "
    "EnvironmentErrorCode: Environment.Error.ErrorCode, "
    "EnvironmentErrorMessage: Environment.Error.Message}"
)


# provisioned concurrency is configured per alias or version; NextToken must stay in the projection
PROVISIONED_QUERY = (
    "{ProvisionedConcurrencyConfigs: ProvisionedConcurrencyConfigs[].{FunctionArn: FunctionArn, "
    "Requested: RequestedProvisionedConcurrentExecutions, "
    "Available: AvailableProvisionedConcurrentExecutions, "
    "Allocated: AllocatedProvisionedConcurrentExecutions, Status: Status, "
    "StatusReason: StatusReason}, NextToken: NextToken}"
)
MAX_PROVISIONED = 50  # configs returned (a function has one per alias or version that has any)


def read_config(ctx, target):
    """The raw narrowed configuration of the function `target`. A missing function raises
    not_found, a forbidden read permission_denied; both propagate."""
    doc = aws_json(
        ctx,
        [
            "lambda",
            "get-function-configuration",
            "--function-name",
            target.arn,
            "--query",
            CONFIG_QUERY,
        ],
        region=target.region,
    )
    if not isinstance(doc, dict) or not doc.get("FunctionName"):
        raise RwError("internal", "lambda get-function-configuration returned no function")
    return doc


def read_reserved_concurrency(ctx, target):
    """{configured, executions}: the function's reserved concurrency. `configured: false` is a
    function that draws from the account's unreserved pool; executions 0 throttles every call."""
    doc = aws_json(
        ctx,
        ["lambda", "get-function-concurrency", "--function-name", target.arn],
        region=target.region,
    )
    value = doc.get("ReservedConcurrentExecutions") if isinstance(doc, dict) else None
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise RwError("internal", "lambda get-function-concurrency returned a non-integer value")
    return {"configured": value is not None, "executions": value}


def read_provisioned_concurrency(ctx, target):
    """{configs[], count, truncated}: the provisioned concurrency of the function's aliases and
    versions (none is configured on `$LATEST`), at most MAX_PROVISIONED of them."""
    rows, truncated = aws_paged(
        ctx,
        [
            "lambda",
            "list-provisioned-concurrency-configs",
            "--function-name",
            target.arn,
            "--query",
            PROVISIONED_QUERY,
        ],
        region=target.region,
        items_key="ProvisionedConcurrencyConfigs",
        limit=MAX_PROVISIONED,
    )
    configs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        reason, _ = clip(row.get("StatusReason") or "")
        arn = row.get("FunctionArn")
        configs.append(
            {
                # arn:aws:lambda:<region>:<account>:function:<name>:<qualifier>
                "qualifier": arn.rsplit(":", 1)[-1]
                if isinstance(arn, str) and arn.count(":") >= 7
                else None,
                "requested": row.get("Requested"),
                "allocated": row.get("Allocated"),
                "available": row.get("Available"),
                "status": row.get("Status"),
                "statusReason": reason or None,
            }
        )
    return {"configs": configs, "count": len(configs), "truncated": truncated}


def log_group(config, name):
    """The log group the function writes to: its logging configuration, else the default."""
    return config.get("LogGroup") or f"/aws/lambda/{name}"


def function_facts(config, region, reserved=None, provisioned=None):
    """The `function` output: plain facts, text cut to 1024 characters. `reserved` and
    `provisioned` are the two concurrency reads, None when that read failed."""
    state_reason, cut_a = clip(config.get("StateReason") or "")
    update_reason, cut_b = clip(config.get("LastUpdateStatusReason") or "")
    env_message, cut_c = clip(config.get("EnvironmentErrorMessage") or "")
    vpc = None
    if config.get("SubnetCount"):  # 0 (or null): not attached to a VPC
        vpc = {
            "subnetCount": config["SubnetCount"],
            "securityGroupCount": config.get("SecurityGroupCount") or 0,
        }
    return {
        "name": config["FunctionName"],
        "region": region,
        "arn": config.get("FunctionArn"),
        "runtime": config.get("Runtime"),
        "packageType": config.get("PackageType"),
        "handler": config.get("Handler"),
        "memoryMb": config.get("MemorySize"),
        "timeoutSeconds": config.get("Timeout"),
        "ephemeralStorageMb": config.get("EphemeralStorageMb"),
        "architectures": list(config.get("Architectures") or [])[:50],
        "state": config.get("State"),
        "stateReasonCode": config.get("StateReasonCode"),
        "stateReason": state_reason or None,
        "lastUpdateStatus": config.get("LastUpdateStatus"),
        "lastUpdateStatusReasonCode": config.get("LastUpdateStatusReasonCode"),
        "lastUpdateStatusReason": update_reason or None,
        "lastModified": config.get("LastModified"),
        "codeSizeBytes": config.get("CodeSize"),
        "version": config.get("Version"),
        "layerCount": config.get("LayerCount") or 0,
        "vpc": vpc,
        "deadLetterTargetArn": config.get("DeadLetterTargetArn"),
        "tracingMode": config.get("TracingMode"),
        "logGroup": log_group(config, config["FunctionName"]),
        "logFormat": config.get("LogFormat"),
        "environmentError": (
            {"code": config["EnvironmentErrorCode"], "message": env_message or None}
            if config.get("EnvironmentErrorCode")
            else None
        ),
        "reservedConcurrency": reserved,
        "provisionedConcurrency": provisioned,
        "textTruncated": cut_a or cut_b or cut_c,
    }
