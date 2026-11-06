"""service: the Services that select the workload's pods, their endpoints and target ports."""

from k8s_litellm_health.common import NESTED_LIMIT, cap
from k8s_litellm_health.errors import RwError
from k8s_litellm_health.fetch import main_container, serving_port
from k8s_litellm_health.k8s import kubectl_json, kubectl_json_scoped

DEFAULT_SERVICE_PORT = 4000
SLICE_LABEL = "kubernetes.io/service-name"


def check_service_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RwError("invalid_input", f"servicePort must be an integer 1..65535, got {port!r}")
    return port


def template_spec(workload):
    return ((workload.get("spec") or {}).get("template") or {}).get("spec") or {}


def template_ports(workload):
    """[{name, port}] of the container ports the workload's pod template declares."""
    containers = template_spec(workload).get("containers") or []
    return [
        {"name": p.get("name"), "port": p.get("containerPort")}
        for c in containers
        for p in c.get("ports") or []
    ]


def selects(service, labels):
    """Whether the Service's selector picks the pods carrying `labels` (a non-empty selector whose
    every pair is in the labels)."""
    selector = (service.get("spec") or {}).get("selector") or {}
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def declared(target, declared_ports):
    """Whether a Service port's targetPort names a container port the pod template declares: a
    number is compared with the declared numbers, a name with the declared names. An undeclared
    number is legal (a container may listen on a port it does not declare); an undeclared name is
    not (the endpoints carry no port)."""
    if isinstance(target, int):
        return any(p["port"] == target for p in declared_ports)
    return any(p["name"] == target for p in declared_ports)


def resolve(target, main_ports):
    """The container port number a Service port's targetPort leads to: a number is itself, a name
    is looked up among the main container's declared ports (None when it names none)."""
    if isinstance(target, int):
        return target
    for p in main_ports:
        if p.get("name") == target:
            return p.get("containerPort")
    return None


def port_rows(service, declared_ports, expected, container_port, main_ports):
    rows = []
    for p in (service.get("spec") or {}).get("ports") or []:
        target = p.get("targetPort", p.get("port"))
        rows.append(
            {
                "name": p.get("name"),
                "port": p.get("port"),
                "targetPort": target,
                "protocol": p.get("protocol") or "TCP",
                "targetPortDeclared": declared(target, declared_ports),
                "targetPortMatches": resolve(target, main_ports) == container_port,
            }
        )
    return cap(rows, NESTED_LIMIT), any(r["port"] == expected for r in rows)


def endpoint_key(endpoint):
    """What names the pod behind an endpoint, so a dual-stack Service's IPv4 and IPv6 slices (one
    endpoint per pod in each) count it once: its targetRef, else its addresses."""
    ref = endpoint.get("targetRef") or {}
    if ref.get("name"):
        return (ref.get("kind"), ref.get("namespace"), ref.get("name"))
    return ("addresses", tuple(sorted(endpoint.get("addresses") or [])))


def endpoint_summary(slices):
    """Counts over a Service's EndpointSlices, one per pod (see endpoint_key): a pod listed in
    several slices (IPv4 and IPv6) is counted once, and is not ready when any of its entries is
    not. An endpoint whose `ready` is unset counts as ready (the API's documented reading);
    `ready: false` is not ready."""
    pods = {}
    ports = []
    for item in slices:
        for endpoint in item.get("endpoints") or []:
            not_ready = (endpoint.get("conditions") or {}).get("ready") is False
            key = endpoint_key(endpoint)
            pods[key] = pods.get(key, False) or not_ready
        for p in item.get("ports") or []:
            entry = {"name": p.get("name"), "port": p.get("port"), "protocol": p.get("protocol")}
            if entry not in ports:
                ports.append(entry)
    not_ready = sum(1 for v in pods.values() if v)
    return {
        "sliceCount": len(slices),
        "readyEndpoints": len(pods) - not_ready,
        "notReadyEndpoints": not_ready,
        "ports": cap(ports, NESTED_LIMIT),
    }


def view(ctx, workload, expected, limit):
    namespace = workload["metadata"]["namespace"]
    labels = (((workload.get("spec") or {}).get("template") or {}).get("metadata") or {}).get(
        "labels"
    ) or {}
    services = kubectl_json(ctx, "-n", namespace, "get", "services")["items"]
    matching = sorted(
        (s for s in services if selects(s, labels)), key=lambda s: s["metadata"]["name"]
    )
    shown = cap(matching, limit)
    slices = None
    if shown:
        names = ",".join(s["metadata"]["name"] for s in shown)
        doc = kubectl_json_scoped(
            ctx,
            "endpointslices",
            "-n",
            namespace,
            "get",
            "endpointslices",
            "-l",
            f"{SLICE_LABEL} in ({names})",
            hint="grant list on discovery.k8s.io endpointslices in the target's namespace",
        )
        slices = None if doc is None else doc["items"]
    declared_ports = template_ports(workload)
    template = template_spec(workload)
    container_port, port_declared = serving_port(template)
    main = main_container(template) or {}
    main_ports = main.get("ports") or []
    rows = []
    for service in shown:
        name = service["metadata"]["name"]
        ports, exposes = port_rows(service, declared_ports, expected, container_port, main_ports)
        mine = None
        if slices is not None:
            mine = endpoint_summary(
                [s for s in slices if (s["metadata"].get("labels") or {}).get(SLICE_LABEL) == name]
            )
        spec = service.get("spec") or {}
        rows.append(
            {
                "name": name,
                "type": spec.get("type"),
                "headless": spec.get("clusterIP") == "None",
                "selector": spec.get("selector"),
                "exposesPort": exposes,
                "targetPortMatches": any(p["targetPortMatches"] for p in ports),
                "ports": ports,
                "endpoints": mine,
            }
        )
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    return {
        "workload": {
            "kind": workload["kind"],
            "name": workload["metadata"]["name"],
            "replicas": spec.get("replicas"),
            "readyReplicas": status.get("readyReplicas"),
        },
        "servicePort": expected,
        "containerPort": container_port,
        "containerPortDeclared": port_declared,
        "containerPorts": cap(declared_ports, NESTED_LIMIT),
        "serviceCount": len(matching),
        "truncated": len(matching) > limit,
        "services": rows,
    }
