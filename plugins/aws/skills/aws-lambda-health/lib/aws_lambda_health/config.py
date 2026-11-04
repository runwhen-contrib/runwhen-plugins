"""function-config: what Lambda says about one function (get-function-configuration).

The call is narrowed with --query to the fields below. The raw response also holds the function's
environment variables (names and values), which no task here needs or keeps."""

from .awscli import aws_json
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


def read_config(ctx, region, name):
    """The raw narrowed configuration of function `name`. A missing function raises not_found,
    a forbidden read permission_denied; both propagate."""
    doc = aws_json(
        ctx,
        ["lambda", "get-function-configuration", "--function-name", name, "--query", CONFIG_QUERY],
        region=region,
    )
    if not isinstance(doc, dict) or not doc.get("FunctionName"):
        raise RwError("internal", "lambda get-function-configuration returned no function")
    return doc


def log_group(config, name):
    """The log group the function writes to: its logging configuration, else the default."""
    return config.get("LogGroup") or f"/aws/lambda/{name}"


def function_facts(config, region):
    """The `function` output: plain facts, text cut to 1024 characters."""
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
        "textTruncated": cut_a or cut_b or cut_c,
    }
