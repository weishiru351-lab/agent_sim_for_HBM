module tb;
reg [21:0] address;
wire [1:0] channel;
wire [1:0] bank;
wire [3:0] column;
wire [7:0] row;
wire [5:0] byte_offset;
reg [43:0] vectors [0:2070];
integer i;
address_mapper dut(address, channel, bank, column, row, byte_offset);
initial begin
    $readmemh("vectors.mem", vectors);
    for (i=0; i<2071; i=i+1) begin
        address = vectors[i][43:22];
        #1;
        if ({row,column,bank,channel,byte_offset} !== vectors[i][21:0]) begin
            $display("FAIL address=%h", address);
            $fatal(1);
        end
    end
    $display("PASS 2071 vectors");
    $finish;
end
endmodule
