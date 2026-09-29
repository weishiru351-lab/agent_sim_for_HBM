"""Generate a mapper and testbench; never claim RTL validation without a simulator."""

from pathlib import Path
import random
import shutil
import subprocess

from .experiment import save
from .model import Policy


def export_rtl(policy, cfg, folder):
    Policy.parse(policy.to_dict(), cfg)
    if min(cfg.channel_bits, cfg.bank_bits, cfg.column_bits, cfg.row_bits) < 1 or cfg.granule_bytes < 2:
        raise ValueError("RTL exporter requires nonzero fields and granule_bytes >= 2")
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    offset_bits, bits = cfg.granule_bytes.bit_length() - 1, cfg.block_bits
    width = bits + offset_bits
    lines = ["// Generated from validated DSL; byte offset is preserved.",
             f"module address_mapper(input wire [{width-1}:0] address,",
             f"output wire [{cfg.channel_bits-1}:0] channel,",
             f"output wire [{cfg.bank_bits-1}:0] bank,",
             f"output wire [{cfg.column_bits-1}:0] column,",
             f"output wire [{cfg.row_bits-1}:0] row,",
             f"output wire [{offset_bits-1}:0] byte_offset);",
             f"wire [{bits-1}:0] mapped;"]
    for dst, mask in enumerate(policy.masks(cfg)):
        sources = [f"address[{src+offset_bits}]" for src in range(bits) if mask & (1 << src)]
        lines.append(f"assign mapped[{dst}] = " + " ^ ".join(sources) + ";")
    start = 0
    for field, size in (("channel", cfg.channel_bits), ("bank", cfg.bank_bits),
                        ("column", cfg.column_bits), ("row", cfg.row_bits)):
        lines.append(f"assign {field} = mapped[{start+size-1}:{start}];")
        start += size
    lines += [f"assign byte_offset = address[{offset_bits-1}:0];", "endmodule", ""]
    (folder / "address_mapper.sv").write_text("\n".join(lines), encoding="utf-8")
    rng = random.Random(0)
    addresses = {0, (1 << width) - 1, *(1 << bit for bit in range(width))}
    addresses.update(range(1 << width) if width <= 12 else (rng.randrange(1 << width) for _ in range(2048)))
    addresses = sorted(addresses)
    vectors = []
    for address in addresses:
        block, offset = divmod(address, cfg.granule_bytes)
        expected = policy.apply(block) * cfg.granule_bytes + offset
        vectors.append(f"{(address << width) | expected:0{(2*width+3)//4}x}")
    (folder / "vectors.mem").write_text("\n".join(vectors) + "\n", encoding="ascii")
    testbench = f'''module tb;
reg [{width-1}:0] address;
wire [{cfg.channel_bits-1}:0] channel;
wire [{cfg.bank_bits-1}:0] bank;
wire [{cfg.column_bits-1}:0] column;
wire [{cfg.row_bits-1}:0] row;
wire [{offset_bits-1}:0] byte_offset;
reg [{2*width-1}:0] vectors [0:{len(addresses)-1}];
integer i;
address_mapper dut(address, channel, bank, column, row, byte_offset);
initial begin
    $readmemh("vectors.mem", vectors);
    for (i=0; i<{len(addresses)}; i=i+1) begin
        address = vectors[i][{2*width-1}:{width}];
        #1;
        if ({{row,column,bank,channel,byte_offset}} !== vectors[i][{width-1}:0]) begin
            $display("FAIL address=%h", address);
            $fatal(1);
        end
    end
    $display("PASS {len(addresses)} vectors");
    $finish;
end
endmodule
'''
    (folder / "tb.sv").write_text(testbench, encoding="ascii")
    compiler, runtime = shutil.which("iverilog"), shutil.which("vvp")
    result = {"status": "not_run", "reason": "iverilog and/or vvp not available on PATH",
              "vectors": len(addresses), "coverage": "exhaustive" if width <= 12 else "sampled",
              "dsl": policy.to_dict(), "config": cfg.to_dict()}
    if compiler and runtime:
        try:
            compiled = subprocess.run([compiler, "-g2012", "-s", "tb", "-o", "simulation.vvp",
                                       "address_mapper.sv", "tb.sv"], cwd=folder, capture_output=True, text=True, timeout=30)
            result["compile_log"] = compiled.stdout + compiled.stderr
            result["status"] = "fail"
            result.pop("reason", None)
            if compiled.returncode == 0:
                run = subprocess.run([runtime, "simulation.vvp"], cwd=folder, capture_output=True, text=True, timeout=30)
                result["simulation_log"] = run.stdout + run.stderr
                result["status"] = "pass" if run.returncode == 0 and f"PASS {len(addresses)} vectors" in run.stdout else "fail"
        except subprocess.TimeoutExpired:
            result.update(status="timeout", reason="RTL tool exceeded 30 seconds")
    save(folder / "verification.json", result)
    return result
