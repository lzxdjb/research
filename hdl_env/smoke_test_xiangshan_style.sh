#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/env.sh" ]]; then
  printf 'Missing %s. Run bash hdl_env/rebuild_hdl_env.sh first.\n' "$SCRIPT_DIR/env.sh" >&2
  exit 1
fi

source "$SCRIPT_DIR/env.sh"

WORK="$SCRIPT_DIR/smoke/xiangshan_style"
rm -rf "$WORK"
mkdir -p "$WORK/build" "$WORK/obj_dir"

log() {
  printf '[hdl-xs-smoke] %s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command on PATH: %s\n' "$1" >&2
    exit 1
  fi
}

need_cmd slang
need_cmd verilator
need_cmd yosys

cat > "$WORK/TinyCore.sv" <<'SV'
module TinyCore (
  input  logic       clock,
  input  logic       reset,
  input  logic       valid,
  input  logic [7:0] instr,
  output logic [7:0] acc,
  output logic       done
);
  always_ff @(posedge clock) begin
    if (reset) begin
      acc <= 8'd0;
      done <= 1'b0;
    end else if (valid) begin
      case (instr[7:6])
        2'b00: acc <= acc + {2'b00, instr[5:0]};
        2'b01: acc <= acc ^ {2'b00, instr[5:0]};
        2'b10: acc <= {acc[6:0], acc[7]};
        default: begin
          acc <= acc;
          done <= 1'b1;
        end
      endcase
    end
  end
endmodule
SV

cat > "$WORK/tb_tinycore.cpp" <<'CPP'
#include "VTinyCore.h"
#include "verilated.h"
#include <cstdint>
#include <vector>

static void tick(VTinyCore& dut) {
    dut.clock = 0;
    dut.eval();
    dut.clock = 1;
    dut.eval();
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VTinyCore dut;
    dut.reset = 1;
    dut.valid = 0;
    dut.instr = 0;
    tick(dut);
    tick(dut);
    if (dut.acc != 0 || dut.done != 0) return 10;

    dut.reset = 0;
    dut.valid = 1;
    std::vector<uint8_t> program = {
        0x05,       // add 5
        0x43,       // xor 3
        0x80,       // rotate left
        0xc0        // finish
    };
    for (uint8_t instr : program) {
        dut.instr = instr;
        tick(dut);
    }
    if (dut.acc != 12) return 20;
    if (dut.done != 1) return 21;
    return 0;
}
CPP

log "Frontend check with Slang"
slang "$WORK/TinyCore.sv" >/dev/null

log "Verilator lint"
verilator --lint-only -Wall -Wno-DECLFILENAME "$WORK/TinyCore.sv"

log "Build Verilator simulator executable"
verilator -cc --exe \
  --top-module TinyCore \
  -Mdir "$WORK/obj_dir" \
  "$WORK/TinyCore.sv" "$WORK/tb_tinycore.cpp" >/dev/null
make -C "$WORK/obj_dir" -f VTinyCore.mk >/dev/null

log "Run simulator workload"
"$WORK/obj_dir/VTinyCore"

log "Yosys synthesis"
yosys -q -p "read_verilog -sv $WORK/TinyCore.sv; synth -top TinyCore; stat"

log "XiangShan-style Verilator executable flow passed"
