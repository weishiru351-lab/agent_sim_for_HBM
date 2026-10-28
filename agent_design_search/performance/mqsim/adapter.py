"""Prepare/replay fixed SSD host traces; native MQSim does not consume our DSL."""

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from agent_design_search.inputs.hardware import NANDGeometry
from agent_design_search.json_io import read, save, digest


REVISION = read(Path(__file__).with_name("lock.json"))["commit"]
PROTOCOL = "mqsim-host-trace-v1"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def xml_root(text):
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("External XML declarations are unsupported")
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("Invalid XML") from error


def one(node, name):
    values = node.findall(name)
    if len(values) != 1 or values[0].text is None:
        raise ValueError(f"Expected exactly one XML field {name}")
    return values[0].text.strip()


def number(value):
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Invalid numeric result") from error
    if not value.is_finite() or value < 0:
        raise ValueError("Expected a finite nonnegative number")
    return value


def positive_int(value):
    value = number(value)
    if value != int(value) or value < 1:
        raise ValueError("Expected a positive integer")
    return int(value)


def profile(xml):
    root = xml_root(xml)
    if root.tag != "Execution_Parameter_Set":
        raise ValueError("Expected MQSim Execution_Parameter_Set")
    dev = root.find("Device_Parameter_Set")
    flash = root.find("Device_Parameter_Set/Flash_Parameter_Set")
    if dev is None or flash is None:
        raise ValueError("Missing device/flash configuration")
    for name, value in (("Memory_Type", "FLASH"), ("HostInterface_Type", "NVME"),
                        ("Address_Mapping", "PAGE_LEVEL"), ("Enabled_Preconditioning", "false")):
        if one(dev, name) != value:
            raise ValueError(f"Initial adapter requires {name}={value}")
    dimensions = {k: positive_int(one(node, tag)) for k, node, tag in (
        ("channels", dev, "Flash_Channel_Count"), ("chips", dev, "Chip_No_Per_Channel"),
        ("dies", flash, "Die_No_Per_Chip"), ("planes", flash, "Plane_No_Per_Die"),
        ("blocks", flash, "Block_No_Per_Plane"), ("pages", flash, "Page_No_Per_Block"),
        ("page_bytes", flash, "Page_Capacity"))}
    if dimensions["page_bytes"] % 512:
        raise ValueError("MQSim pages must contain whole 512-byte sectors")
    reserve = number(one(dev, "Overprovisioning_Ratio"))
    if not 0 < reserve < 1:
        raise ValueError("Require an explicit overprovisioning ratio between zero and one")
    # Conservative bound: reserve whole blocks per plane, avoiding native LBA wrapping.
    user_blocks = int(dimensions["blocks"] * (1 - reserve))
    capacity = user_blocks
    for k in ("channels", "chips", "dies", "planes", "pages", "page_bytes"):
        capacity *= dimensions[k]
    return dict(**dimensions, conservative_capacity_bytes=capacity,
                queue_depth=positive_int(one(dev, "IO_Queue_Depth")))


def compile_trace(data, capacity):
    fields = {"version", "kind", "arrival_mode", "capacity_bytes", "requests", "provenance"}
    if (not isinstance(data, dict) or set(data) != fields or type(data["version"]) is not int or data["version"] != 1
            or data["kind"] != "ssd_host" or data["arrival_mode"] != "fixed_arrivals"
            or type(data["capacity_bytes"]) is not int or not 0 < data["capacity_bytes"] <= capacity
            or data["capacity_bytes"] % 512):
        raise ValueError("Native MQSim requires a fixed-arrival, sector-aligned SSD host trace within configured capacity")
    provenance = data["provenance"]
    if (not isinstance(provenance, dict) or set(provenance) != {"kind", "source", "note"}
            or provenance["kind"] not in ("synthetic", "captured", "frozen_model_replay")
            or any(type(provenance[k]) is not str or not provenance[k].strip() for k in ("source", "note"))):
        raise ValueError("Declare trace provenance kind, source and conversion note")
    if not isinstance(data["requests"], list) or not data["requests"]:
        raise ValueError("Nonempty host trace required")
    ids, lines, records = set(), [], []
    for req in data["requests"]:
        if (not isinstance(req, dict) or set(req) != {"request_id", "arrival_ps", "address_bytes", "size_bytes", "kind"}
                or any(type(req[k]) is not int or req[k] < 0 for k in ("request_id", "arrival_ps", "address_bytes", "size_bytes"))
                or req["request_id"] in ids or req["kind"] not in ("read", "write")
                or not req["size_bytes"] or req["size_bytes"] >= 2**32 * 512
                or req["address_bytes"] + req["size_bytes"] > data["capacity_bytes"]
                or req["address_bytes"] % 512 or req["size_bytes"] % 512
                or req["arrival_ps"] % 1000 or req["arrival_ps"] // 1000 >= 2**63):
            raise ValueError("Unique IDs, whole sectors and exact integer nanosecond arrivals are required; no rounding")
        ids.add(req["request_id"])
        records.append(req)
    for req in sorted(records, key=lambda r: (r["arrival_ps"], r["request_id"])):
        lines.append(f"{req['arrival_ps'] // 1000} 0 {req['address_bytes'] // 512} "
                     f"{req['size_bytes'] // 512} {0 if req['kind'] == 'write' else 1}")
    return "\n".join(lines) + "\n"


def host_trace(commands, target, provenance):
    """BlockIO -> byte-addressed trace; caller explicitly declares arrival origin."""
    if target.medium != "ssd":
        raise ValueError("Native MQSim host commands require an SSD target, not a raw NAND target")
    width = target.geometry.block_bytes
    requests = []
    for index, cmd in enumerate(commands):
        if (type(cmd.lba) is not int or type(cmd.blocks) is not int
                or cmd.lba < 0 or not 1 <= cmd.blocks <= target.max_transfer_blocks
                or cmd.request.address != cmd.lba * width):
            raise ValueError("Invalid host block command")
        # RMW uses a read and a write with the same logical ID; native rows get unique IDs.
        requests.append(dict(request_id=index, arrival_ps=cmd.request.arrival,
                             address_bytes=cmd.lba * width, size_bytes=cmd.blocks * width, kind=cmd.request.kind))
    data = dict(version=1, kind="ssd_host", arrival_mode="fixed_arrivals", capacity_bytes=target.capacity_bytes,
                requests=requests, provenance=provenance)
    compile_trace(data, target.capacity_bytes)
    return data


def prepare(data, config_path, output):
    config = Path(config_path).read_text(encoding="utf-8")
    geometry = profile(config)
    trace = compile_trace(data, geometry["conservative_capacity_bytes"])
    root = ET.Element("MQSim_IO_Scenarios")
    flow = ET.SubElement(ET.SubElement(root, "IO_Scenario"), "IO_Flow_Parameter_Set_Trace_Based")
    fields = dict(Priority_Class="HIGH", Device_Level_Data_Caching_Mode="TURNED_OFF",
                  Initial_Occupancy_Percentage="0", File_Path="requests.trace",
                  Percentage_To_Be_Executed="100", Relay_Count="1", Time_Unit="NANOSECOND")
    for name, dim in (("Channel", "channels"), ("Chip", "chips"), ("Die", "dies"), ("Plane", "planes")):
        fields[name + "_IDs"] = ",".join(map(str, range(geometry[dim])))
    for key, value in fields.items():
        ET.SubElement(flow, key).text = value
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "ssdconfig.xml").write_text(config, encoding="utf-8")
    (output / "requests.trace").write_text(trace, encoding="ascii")
    ET.ElementTree(root).write(output / "workload.xml", encoding="utf-8", xml_declaration=True)
    bundle = dict(protocol=PROTOCOL, upstream_commit=REVISION, input=data, profile=geometry,
        files={n: sha256(output / n) for n in ("ssdconfig.xml", "requests.trace", "workload.xml")},
        contract=dict(scope="standalone SSD host stream with MQSim-owned fixed controller",
            initial_state="empty, preconditioning off; no TRIM", input_time="exact ps / 1000 to ns",
            completion="native aggregate counters and integer-us latency; no per-request callback evidence",
            python_dsl_equivalent=False, unified_search_score=False))
    bundle["bundle_id"] = digest(bundle)
    save(output / "bundle.json", bundle)
    return bundle


def load_bundle(folder):
    folder = Path(folder)
    bundle = read(folder / "bundle.json")
    identity = bundle.pop("bundle_id")
    if bundle.get("protocol") != PROTOCOL or bundle.get("upstream_commit") != REVISION or digest(bundle) != identity:
        raise ValueError("Invalid MQSim bundle identity/version")
    bundle["bundle_id"] = identity
    expected_files = {"ssdconfig.xml", "requests.trace", "workload.xml"}
    if set(bundle["files"]) != expected_files or any(sha256(folder / n) != bundle["files"][n] for n in expected_files):
        raise ValueError("MQSim bundle contents changed")
    compile_trace(bundle["input"], profile((folder / "ssdconfig.xml").read_text(encoding="utf-8"))["conservative_capacity_bytes"])
    return bundle


def parse_results(bundle, xml):
    """Validate native aggregate output. No invented makespan or per-I/O p95."""
    root = xml_root(xml)
    flows = [e for e in root.iter() if e.tag.endswith(".IO_Flow")]
    if root.tag != "MQSim_Results" or len(flows) != 1:
        raise ValueError("Expected exactly one native MQSim IO flow")
    flow, requests = flows[0], bundle["input"]["requests"]
    expected = {"Request_Count": len(requests), "Bytes_Transferred": sum(r["size_bytes"] for r in requests)}
    for kind, title in (("read", "Read"), ("write", "Write")):
        group = [r for r in requests if r["kind"] == kind]
        expected[title + "_Request_Count"] = len(group)
        expected["Bytes_Transferred_" + title] = sum(r["size_bytes"] for r in group)
    if any(number(one(flow, k)) != v for k, v in expected.items()):
        raise ValueError("MQSim generated counts/completed byte totals do not cover the frozen input")
    metrics = dict(expected)
    for stem in ("Device_Response_Time", "End_to_End_Request_Delay"):
        values = [number(one(flow, prefix + stem)) for prefix in ("Min_", "", "Max_")]
        if any(v != int(v) or v >= 2**32 for v in values) or not values[0] <= values[1] <= values[2]:
            raise ValueError("Native latency must be ordered, truncated uint32 microseconds")
        metrics[stem + "_us"] = int(values[1])
        metrics["Min_" + stem + "_us"] = int(values[0])
        metrics["Max_" + stem + "_us"] = int(values[2])
    metrics["IOPS"] = float(number(one(flow, "IOPS")))
    if not math.isfinite(metrics["IOPS"]):
        raise ValueError("IOPS is outside finite float range")
    return dict(status="aggregate_only", bundle_id=bundle["bundle_id"], metrics=metrics,
                completion_verified_per_request=False, unified_search_score=False,
                limits=["native microsecond truncation/uint32 range", "aggregate coverage is not content or per-request verification",
                        "fixed native controller is not the Python NAND DSL implementation"])


def run(folder, executable, output, timeout_seconds=60):
    """Optional future runner. Nothing is installed or compiled by this module."""
    bundle, folder = load_bundle(folder), Path(folder)
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise ValueError("Positive integer timeout required")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    binary = Path(executable).resolve()
    result = dict(status="not_run", backend_calls=0, bundle_id=bundle["bundle_id"])
    if not binary.is_file():
        result["reason"] = "MQSim executable missing"
    else:
        for name in bundle["files"]:
            shutil.copyfile(folder / name, output / name)
        result["binary_sha256"] = sha256(binary)
        try:
            # communicate() drains pipes; no shell and no background process.
            process = subprocess.Popen([str(binary), "-i", "ssdconfig.xml", "-w", "workload.xml"],
                                       cwd=output, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            result["reason"] = str(error)
        else:
            result.update(status="backend_error", backend_calls=1)
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
                result["returncode"] = process.returncode
                if process.returncode:
                    raise ValueError("MQSim returned a failure code")
                path = output / "workload_scenario_1.xml"
                parsed = parse_results(bundle, path.read_text(encoding="utf-8"))
                result.update(parsed, result_sha256=sha256(path))
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                result["reason"] = "MQSim process timed out"
            except (OSError, ValueError) as error:
                result["reason"] = str(error)
            (output / "stdout.log").write_bytes(stdout)
            (output / "stderr.log").write_bytes(stderr)
    save(output / "result.json", result)
    return result


def import_results(folder, xml_path, output):
    """Validate a user-associated XML; native output cannot attest its input hash."""
    bundle = load_bundle(folder)
    result = parse_results(bundle, Path(xml_path).read_text(encoding="utf-8"))
    result.update(origin="user_associated_unverified_run", result_sha256=sha256(xml_path),
                  backend_calls=0)  # Parsing is not a new simulator call.
    output = Path(output)
    if output.exists():
        raise ValueError("Do not overwrite an imported result")
    save(output, result)
    return result


def prepare_raw_nand(geometry, actions, provenance, output):
    """Custom-driver contract: ordered physical operations, never a native SSD trace.

    Caller supplies the already chosen actions. Serial dependencies deliberately
    prevent pretending a frozen replay can re-evaluate adaptive GC policies.
    """
    geometry = NANDGeometry.parse(geometry)
    if not isinstance(actions, list) or not actions or not isinstance(provenance, dict) or not provenance:
        raise ValueError("Raw NAND export needs ordered actions and explicit provenance")
    records = []
    for index, action in enumerate(actions):
        if (not isinstance(action, dict) or set(action) != {"kind", "physical_page"}
                or action["kind"] not in ("read", "program", "erase")):
            raise ValueError("Export only explicit raw read/program/erase operations")
        coordinate = geometry.decode(action["physical_page"])
        if action["kind"] == "erase" and coordinate[-1] != 0:
            raise ValueError("Erase must identify the first page of its block")
        records.append(dict(operation_id=index, **action, coordinate=list(coordinate),
                            after=[] if index == 0 else [index - 1]))
    bundle = dict(protocol="mqsim-raw-nand-driver-contract-v1", status="needs_driver", backend_calls=0,
        upstream_commit=REVISION, geometry=geometry.to_dict(), operations=records, provenance=provenance,
        contract=dict(native_mqsim_supported=False, execution="serial physical replay",
            result_fields=["operation_id", "accepted_ps", "completed_ps", "status"],
            requirements=["bypass host NVMe/FTL", "preserve selected page coordinates and operation dependencies",
                          "validate NAND legality and drain all operations", "do not compare with full-system completion"],
            policy_search_equivalent=False))
    bundle["bundle_id"] = digest(bundle)
    output = Path(output)
    if output.exists():
        raise ValueError("Do not overwrite a driver contract")
    save(output, bundle)
    return bundle


def validate_raw_result(bundle, result):
    """Future custom driver must return one completed event for every operation."""
    if (bundle.get("protocol") != "mqsim-raw-nand-driver-contract-v1"
            or bundle.get("bundle_id") != digest({k: v for k, v in bundle.items() if k != "bundle_id"})
            or result.get("bundle_id") != bundle["bundle_id"] or result.get("time_unit") != "ps"):
        raise ValueError("Raw driver bundle identity or time unit mismatch")
    events = result.get("events")
    if not isinstance(events, list) or len(events) != len(bundle["operations"]):
        raise ValueError("Every raw operation needs a completion event")
    by_id = {}
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"operation_id", "accepted_ps", "completed_ps", "status"}
                or any(type(event[k]) is not int or event[k] < 0 for k in ("operation_id", "accepted_ps", "completed_ps"))
                or event["status"] != "ok" or event["operation_id"] in by_id
                or event["accepted_ps"] > event["completed_ps"]):
            raise ValueError("Invalid or duplicate raw completion event")
        by_id[event["operation_id"]] = event
    if set(by_id) != set(range(len(events))):
        raise ValueError("Unknown raw operation IDs")
    for operation in bundle["operations"]:
        if any(by_id[p]["completed_ps"] > by_id[operation["operation_id"]]["accepted_ps"] for p in operation["after"]):
            raise ValueError("Raw driver violated operation dependencies")
    return dict(status="ok", completed_operations=len(events),
                physical_replay_completion_ps=max(e["completed_ps"] for e in events),
                unified_search_score=False, content_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = commands.add_parser("prepare")
    prepare_cmd.add_argument("--trace", required=True, type=Path)
    prepare_cmd.add_argument("--config", required=True, type=Path)
    prepare_cmd.add_argument("--output", required=True, type=Path)
    run_cmd = commands.add_parser("run")
    run_cmd.add_argument("--bundle", required=True, type=Path)
    run_cmd.add_argument("--executable", required=True, type=Path)
    run_cmd.add_argument("--output", required=True, type=Path)
    run_cmd.add_argument("--timeout", type=int, default=60)
    raw_cmd = commands.add_parser("prepare-raw")
    raw_cmd.add_argument("--input", required=True, type=Path, help="geometry, actions and provenance JSON")
    raw_cmd.add_argument("--output", required=True, type=Path)
    import_cmd = commands.add_parser("import")
    import_cmd.add_argument("--bundle", required=True, type=Path)
    import_cmd.add_argument("--xml", required=True, type=Path)
    import_cmd.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(read(args.trace), args.config, args.output)
        elif args.command == "prepare-raw":
            result = prepare_raw_nand(**read(args.input), output=args.output)
        elif args.command == "import":
            result = import_results(args.bundle, args.xml, args.output)
        else:
            result = run(args.bundle, args.executable, args.output, args.timeout)
        print(result.get("status", "prepared"), result["bundle_id"])
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
