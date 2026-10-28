"""Interface fixtures only: no MQSim executable is installed or invoked."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_design_search.performance.mqsim import adapter
from agent_design_search.contracts import BlockIO, Request
from agent_design_search.inputs.hardware import SSDTarget, BlockGeometry, NANDGeometry


CONFIG = """<Execution_Parameter_Set><Device_Parameter_Set>
<Memory_Type>FLASH</Memory_Type><HostInterface_Type>NVME</HostInterface_Type>
<Address_Mapping>PAGE_LEVEL</Address_Mapping><Enabled_Preconditioning>false</Enabled_Preconditioning>
<Flash_Channel_Count>2</Flash_Channel_Count><Chip_No_Per_Channel>1</Chip_No_Per_Channel>
<Overprovisioning_Ratio>0.25</Overprovisioning_Ratio><IO_Queue_Depth>4</IO_Queue_Depth>
<Flash_Parameter_Set><Die_No_Per_Chip>1</Die_No_Per_Chip><Plane_No_Per_Die>1</Plane_No_Per_Die>
<Block_No_Per_Plane>4</Block_No_Per_Plane><Page_No_Per_Block>4</Page_No_Per_Block>
<Page_Capacity>4096</Page_Capacity></Flash_Parameter_Set></Device_Parameter_Set></Execution_Parameter_Set>"""


def trace():
    return dict(version=1, kind="ssd_host", arrival_mode="fixed_arrivals", capacity_bytes=8192,
        requests=[dict(request_id=5, arrival_ps=2000, address_bytes=4096, size_bytes=512, kind="read"),
                  dict(request_id=3, arrival_ps=0, address_bytes=0, size_bytes=4096, kind="write")],
        provenance=dict(kind="synthetic", source="test fixture", note="No simulator measurement"))


def result_xml():
    values = dict(Request_Count=2, Read_Request_Count=1, Write_Request_Count=1,
        Bytes_Transferred=4608, Bytes_Transferred_Read=512, Bytes_Transferred_Write=4096, IOPS=12.5,
        Device_Response_Time=10, Min_Device_Response_Time=5, Max_Device_Response_Time=15,
        End_to_End_Request_Delay=12, Min_End_to_End_Request_Delay=6, Max_End_to_End_Request_Delay=18)
    return "<MQSim_Results><Host.IO_Flow>" + "".join(f"<{k}>{v}</{k}>" for k, v in values.items()) + "</Host.IO_Flow></MQSim_Results>"


class MQSimInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "fixture.xml"
        self.config.write_text(CONFIG)
        self.bundle = adapter.prepare(trace(), self.config, self.root / "bundle")

    def test_exact_sector_time_direction_and_integrity(self):
        self.assertEqual((self.root / "bundle/requests.trace").read_text(), "0 0 0 8 0\n2 0 8 1 1\n")
        self.assertIn("<Time_Unit>NANOSECOND</Time_Unit>", (self.root / "bundle/workload.xml").read_text())
        self.assertFalse(self.bundle["contract"]["python_dsl_equivalent"])
        self.assertEqual(adapter.load_bundle(self.root / "bundle"), self.bundle)
        (self.root / "bundle/requests.trace").write_text("0 0 0 8 1\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            adapter.load_bundle(self.root / "bundle")

    def test_reject_lossy_conversion_wrapping_or_silent_raw_nand(self):
        for field, value in (("arrival_ps", 999), ("size_bytes", 8), ("address_bytes", 8192),
                             ("request_id", True), ("kind", "erase")):
            data = trace()
            data["requests"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                adapter.compile_trace(data, 8192)
        data = trace(); data["kind"] = "raw_nand"
        with self.assertRaises(ValueError): adapter.compile_trace(data, 8192)
        data = trace(); data["arrival_mode"] = "serial_groups"
        with self.assertRaises(ValueError): adapter.compile_trace(data, 8192)

    def test_native_microseconds_coverage_and_corrupt_outputs(self):
        result = adapter.parse_results(self.bundle, result_xml())
        self.assertEqual(result["status"], "aggregate_only")
        self.assertEqual(result["metrics"]["End_to_End_Request_Delay_us"], 12)
        self.assertNotIn("completion_ps", result["metrics"])
        for old, new in (("<Bytes_Transferred>4608", "<Bytes_Transferred>4096"),
                         ("<Request_Count>2", "<Request_Count>1"),
                         ("<IOPS>12.5", "<IOPS>NaN"),
                         ("<Device_Response_Time>10", "<Device_Response_Time>16"),
                         ("<Device_Response_Time>10", "<Device_Response_Time>10.5")):
            with self.subTest(old=old), self.assertRaises(ValueError):
                adapter.parse_results(self.bundle, result_xml().replace(old, new))

    def test_missing_process_and_started_timeout_accounting(self):
        missing = adapter.run(self.root / "bundle", self.root / "missing", self.root / "missing_run")
        self.assertEqual((missing["status"], missing["backend_calls"]), ("not_run", 0))
        fake = self.root / "fixture_binary"; fake.write_bytes(b"not executable; mocked only")
        with patch.object(adapter.subprocess, "Popen") as popen:
            process = popen.return_value
            process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 1), (b"partial", b"")]
            failure = adapter.run(self.root / "bundle", fake, self.root / "timeout", 1)
            self.assertEqual((failure["status"], failure["backend_calls"]), ("backend_error", 1))
            process.kill.assert_called_once()

    def test_block_adapter_and_explicit_raw_driver_boundary(self):
        target = SSDTarget("m", BlockGeometry(16, 512), 8, 512, ())
        commands = [BlockIO(Request(1, 0, 0, "write"), 0, 8)]
        data = adapter.host_trace(commands, target, trace()["provenance"])
        self.assertEqual(adapter.compile_trace(data, 8192), "0 0 0 8 0\n")
        geometry = NANDGeometry(2, 1, 1, 4, 4, 4096)
        bundle = adapter.prepare_raw_nand(geometry.to_dict(),
            [dict(kind="program", physical_page=0), dict(kind="read", physical_page=0), dict(kind="erase", physical_page=0)],
            dict(source="test fixture", note="serialized physical replay"), self.root / "raw.json")
        self.assertEqual((bundle["status"], bundle["backend_calls"]), ("needs_driver", 0))
        self.assertEqual(bundle["operations"][2]["after"], [1])
        self.assertFalse(bundle["contract"]["native_mqsim_supported"])
        result = dict(bundle_id=bundle["bundle_id"], time_unit="ps", events=[
            dict(operation_id=i, accepted_ps=i * 10, completed_ps=(i + 1) * 10, status="ok") for i in range(3)])
        self.assertEqual(adapter.validate_raw_result(bundle, result)["physical_replay_completion_ps"], 30)
        result["events"][1]["accepted_ps"] = 5
        with self.assertRaisesRegex(ValueError, "dependencies"):
            adapter.validate_raw_result(bundle, result)
        with self.assertRaises(ValueError):
            adapter.prepare_raw_nand(geometry.to_dict(), [dict(kind="erase", physical_page=1)],
                                     dict(source="fixture"), self.root / "bad.json")


if __name__ == "__main__":
    unittest.main()
