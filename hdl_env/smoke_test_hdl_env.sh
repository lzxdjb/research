#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/env.sh" ]]; then
  printf 'Missing %s. Run bash hdl_env/rebuild_hdl_env.sh first.\n' "$SCRIPT_DIR/env.sh" >&2
  exit 1
fi

source "$SCRIPT_DIR/env.sh"

WORK="$SCRIPT_DIR/smoke/work"
rm -rf "$WORK"
mkdir -p "$WORK/verilator" "$WORK/chisel"

log() {
  printf '[hdl-smoke] %s\n' "$*"
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
need_cmd mvn

log "Checking tool versions"
yosys -V
verilator --version
slang --version
mvn --version | head -n 1

cat > "$WORK/Counter.sv" <<'SV'
module Counter #(
  parameter WIDTH = 4
) (
  input  logic             clk,
  input  logic             rst,
  input  logic             en,
  output logic [WIDTH-1:0] count
);
  always_ff @(posedge clk) begin
    if (rst) begin
      count <= '0;
    end else if (en) begin
      count <= count + 1'b1;
    end
  end
endmodule
SV

cat > "$WORK/tb_counter.cpp" <<'CPP'
#include "VCounter.h"
#include "verilated.h"

static void tick(VCounter& dut) {
    dut.clk = 0;
    dut.eval();
    dut.clk = 1;
    dut.eval();
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VCounter dut;
    dut.rst = 1;
    dut.en = 0;
    tick(dut);
    tick(dut);
    if (dut.count != 0) return 10;
    dut.rst = 0;
    dut.en = 1;
    for (int i = 0; i < 3; ++i) tick(dut);
    if (dut.count != 3) return 11;
    dut.en = 0;
    tick(dut);
    if (dut.count != 3) return 12;
    return 0;
}
CPP

log "Running SystemVerilog parse/lint with slang"
slang "$WORK/Counter.sv" >/dev/null

log "Running Verilator lint"
verilator --lint-only -Wall -Wno-DECLFILENAME "$WORK/Counter.sv"

log "Running Verilator simulation"
verilator -cc --exe \
  --top-module Counter \
  -Mdir "$WORK/verilator" \
  "$WORK/Counter.sv" "$WORK/tb_counter.cpp" >/dev/null
make -C "$WORK/verilator" -f VCounter.mk >/dev/null
"$WORK/verilator/VCounter"

log "Running Yosys synthesis"
yosys -q -p "read_verilog -sv $WORK/Counter.sv; synth -top Counter; stat"

mkdir -p "$WORK/chisel/src/main/scala"

cat > "$WORK/chisel/pom.xml" <<'XML'
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>local.hdl</groupId>
  <artifactId>chisel-smoke</artifactId>
  <version>0.1.0</version>
  <properties>
    <scala.version>2.13.17</scala.version>
    <chisel.version>7.2.0</chisel.version>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.chipsalliance</groupId>
      <artifactId>chisel_2.13</artifactId>
      <version>${chisel.version}</version>
    </dependency>
  </dependencies>
  <build>
    <sourceDirectory>src/main/scala</sourceDirectory>
    <plugins>
      <plugin>
        <groupId>net.alchim31.maven</groupId>
        <artifactId>scala-maven-plugin</artifactId>
        <version>4.9.10</version>
        <configuration>
          <scalaVersion>${scala.version}</scalaVersion>
          <compilerPlugins>
            <compilerPlugin>
              <groupId>org.chipsalliance</groupId>
              <artifactId>chisel-plugin_2.13.17</artifactId>
              <version>${chisel.version}</version>
            </compilerPlugin>
          </compilerPlugins>
        </configuration>
        <executions>
          <execution>
            <goals>
              <goal>compile</goal>
            </goals>
          </execution>
        </executions>
      </plugin>
      <plugin>
        <groupId>org.codehaus.mojo</groupId>
        <artifactId>exec-maven-plugin</artifactId>
        <version>3.5.0</version>
        <configuration>
          <mainClass>Elaborate</mainClass>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
XML

cat > "$WORK/chisel/src/main/scala/TinyAdder.scala" <<'SCALA'
import chisel3._
import circt.stage.ChiselStage

class TinyAdder(width: Int) extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(width.W))
    val b = Input(UInt(width.W))
    val y = Output(UInt(width.W))
  })

  io.y := io.a + io.b
}

object Elaborate extends App {
  print(
    ChiselStage.emitSystemVerilog(
      new TinyAdder(8),
      firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
    )
  )
}
SCALA

log "Elaborating Chisel to SystemVerilog"
mvn -q -f "$WORK/chisel/pom.xml" compile exec:java > "$WORK/chisel/TinyAdder.full.sv"
awk '/^\/\/ ----- 8< ----- FILE / { exit } { print }' "$WORK/chisel/TinyAdder.full.sv" > "$WORK/chisel/TinyAdder.sv"

log "Checking Chisel-emitted SystemVerilog with slang"
slang "$WORK/chisel/TinyAdder.sv" >/dev/null

log "Checking Chisel-emitted SystemVerilog with Verilator"
verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-UNUSED "$WORK/chisel/TinyAdder.sv"

log "Synthesizing Chisel-emitted SystemVerilog with Yosys"
yosys -q -p "read_verilog -sv $WORK/chisel/TinyAdder.sv; synth -top TinyAdder; stat"

log "All HDL smoke tests passed"

if [[ "${HDL_ENV_RUN_XIANGSHAN_STYLE_SMOKE:-1}" == "1" || "${HDL_ENV_RUN_XIANGSHAN_STYLE_SMOKE:-1}" == "true" ]]; then
  bash "$SCRIPT_DIR/smoke_test_xiangshan_style.sh"
fi
