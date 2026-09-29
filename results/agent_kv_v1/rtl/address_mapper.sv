// Generated from validated DSL; byte offset is preserved.
module address_mapper(input wire [21:0] address,
output wire [1:0] channel,
output wire [1:0] bank,
output wire [3:0] column,
output wire [7:0] row,
output wire [5:0] byte_offset);
wire [15:0] mapped;
assign mapped[0] = address[18];
assign mapped[1] = address[19];
assign mapped[2] = address[20];
assign mapped[3] = address[10];
assign mapped[4] = address[14];
assign mapped[5] = address[11];
assign mapped[6] = address[12];
assign mapped[7] = address[13];
assign mapped[8] = address[9];
assign mapped[9] = address[15];
assign mapped[10] = address[16];
assign mapped[11] = address[17];
assign mapped[12] = address[6];
assign mapped[13] = address[7];
assign mapped[14] = address[8];
assign mapped[15] = address[21];
assign channel = mapped[1:0];
assign bank = mapped[3:2];
assign column = mapped[7:4];
assign row = mapped[15:8];
assign byte_offset = address[5:0];
endmodule
