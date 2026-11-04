"""instance-config: what EC2 says about one instance (describe-instances).

The call is narrowed with --query to the fields below. Never requested, so never held by this task
or its recording: the instance's user data (a separate `describe-instance-attribute` call that no
task here makes, and that routinely carries bootstrap scripts with credentials), the console output,
the key pair name and any key material (the API never returns it for an existing instance), the
network interfaces' details and the instance's public DNS names. Tags are free text and may hold
secrets: a tag whose key names a secret or whose value looks like one is reported by key only
(see text.py).

Every call names the instance by id and the target's region. The reservation's OwnerId is compared
with the target's account, so an id that belongs to another account than the target is `not_found`
here, never read as the target's."""

from .awscli import aws_json
from .errors import RwError
from .text import MAX_LIST, clip, display_name, tag_facts

# T-family instances run on CPU credits: t2, t3, t3a, t4g (t1 is long gone)
BURSTABLE_FAMILIES = ("t2", "t3", "t3a", "t4g")

INSTANCE_QUERY = (
    "{OwnerId: Reservations[0].OwnerId, Instance: Reservations[0].Instances[0].{"
    "InstanceId: InstanceId, InstanceType: InstanceType, State: State.Name, StateCode: State.Code, "
    "StateReasonCode: StateReason.Code, StateReasonMessage: StateReason.Message, "
    "StateTransitionReason: StateTransitionReason, LaunchTime: LaunchTime, "
    "AvailabilityZone: Placement.AvailabilityZone, Tenancy: Placement.Tenancy, "
    "Architecture: Architecture, PlatformDetails: PlatformDetails, "
    "VirtualizationType: VirtualizationType, InstanceLifecycle: InstanceLifecycle, "
    "EbsOptimized: EbsOptimized, Monitoring: Monitoring.State, RootDeviceType: RootDeviceType, "
    "ImageId: ImageId, IamInstanceProfileArn: IamInstanceProfile.Arn, VpcId: VpcId, "
    "SubnetId: SubnetId, PrivateIpAddress: PrivateIpAddress, PublicIpAddress: PublicIpAddress, "
    "SecurityGroupIds: SecurityGroups[].GroupId, "
    "NetworkInterfaceCount: length(NetworkInterfaces || `[]`), "
    "HttpTokens: MetadataOptions.HttpTokens, HttpEndpoint: MetadataOptions.HttpEndpoint, "
    "BlockDevices: BlockDeviceMappings[].{DeviceName: DeviceName, VolumeId: Ebs.VolumeId, "
    "Status: Ebs.Status, DeleteOnTermination: Ebs.DeleteOnTermination, "
    "AttachTime: Ebs.AttachTime}, "
    "Tags: Tags[].{Key: Key, Value: Value}}}"
)


def is_burstable(instance_type):
    """Whether the type earns and spends CPU credits (t2.micro, t3a.large, t4g.small...)."""
    family = str(instance_type or "").split(".", 1)[0]
    return family in BURSTABLE_FAMILIES


def read_instance(ctx, target):
    """The raw narrowed description of the instance `target`. A missing instance raises
    not_found (InvalidInstanceID.NotFound), a forbidden read permission_denied; both propagate. An
    answer with no instance, or one owned by another account than the target, is never read as an
    empty instance."""
    doc = aws_json(
        ctx,
        [
            "ec2",
            "describe-instances",
            "--instance-ids",
            target.instance_id,
            "--query",
            INSTANCE_QUERY,
        ],
        region=target.region,
    )
    instance = doc.get("Instance") if isinstance(doc, dict) else None
    if not isinstance(instance, dict) or instance.get("InstanceId") != target.instance_id:
        raise RwError(
            "internal", f"ec2 describe-instances returned no instance {target.instance_id}"
        )
    owner = doc.get("OwnerId")
    if owner is not None and owner != target.account:
        raise RwError(
            "not_found",
            f"instance {target.instance_id} belongs to account {owner}, not the target's "
            f"{target.account}",
        )
    return instance


def block_devices(rows):
    """[{deviceName, volumeId, status, deleteOnTermination, attachTime}] of the EBS volumes, at
    most MAX_LIST, and the total."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    return [
        {
            "deviceName": r.get("DeviceName"),
            "volumeId": r.get("VolumeId"),
            "status": r.get("Status"),
            "deleteOnTermination": r.get("DeleteOnTermination"),
            "attachTime": r.get("AttachTime"),
        }
        for r in rows[:MAX_LIST]
    ], len(rows)


def instance_facts(doc, target):
    """The `instance` output: plain facts, text cut to 1024 characters, tags that look secret
    reported by key only."""
    reason, cut_a = clip(doc.get("StateReasonMessage") or "")
    transition, cut_b = clip(doc.get("StateTransitionReason") or "")
    devices, device_count = block_devices(doc.get("BlockDevices"))
    all_tags = [
        tag_facts(t["Key"], t.get("Value"))
        for t in (doc.get("Tags") or [])
        if isinstance(t, dict) and isinstance(t.get("Key"), str)
    ]
    groups = [g for g in (doc.get("SecurityGroupIds") or []) if isinstance(g, str)]
    return {
        "id": doc["InstanceId"],
        "region": target.region,
        "arn": target.arn,
        "name": display_name(all_tags),
        "instanceType": doc.get("InstanceType"),
        "burstable": is_burstable(doc.get("InstanceType")),
        "state": {"name": doc.get("State"), "code": doc.get("StateCode")},
        "stateReasonCode": doc.get("StateReasonCode"),
        "stateReason": reason or None,
        "stateTransitionReason": transition or None,
        "launchTime": doc.get("LaunchTime"),
        "availabilityZone": doc.get("AvailabilityZone"),
        "tenancy": doc.get("Tenancy"),
        "architecture": doc.get("Architecture"),
        "platform": doc.get("PlatformDetails"),
        "virtualizationType": doc.get("VirtualizationType"),
        "lifecycle": doc.get("InstanceLifecycle") or "on-demand",
        "ebsOptimized": doc.get("EbsOptimized"),
        "monitoring": doc.get("Monitoring"),
        "rootDeviceType": doc.get("RootDeviceType"),
        "imageId": doc.get("ImageId"),
        "iamInstanceProfileArn": doc.get("IamInstanceProfileArn"),
        "vpcId": doc.get("VpcId"),
        "subnetId": doc.get("SubnetId"),
        "privateIpAddress": doc.get("PrivateIpAddress"),
        "publicIpAddress": doc.get("PublicIpAddress"),
        "securityGroupIds": groups[:MAX_LIST],
        "networkInterfaceCount": doc.get("NetworkInterfaceCount") or 0,
        "imdsTokensRequired": (
            None if doc.get("HttpTokens") is None else doc["HttpTokens"] == "required"
        ),
        "imdsEndpointEnabled": (
            None if doc.get("HttpEndpoint") is None else doc["HttpEndpoint"] == "enabled"
        ),
        "blockDevices": devices,
        "blockDeviceCount": device_count,
        "tags": all_tags[:MAX_LIST],
        "tagCount": len(all_tags),
        "tagsRedacted": sum(1 for t in all_tags if t["redacted"]),
        "textTruncated": cut_a or cut_b,
    }
