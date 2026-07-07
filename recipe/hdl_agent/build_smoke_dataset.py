#!/usr/bin/env python3
"""Build a tiny HDLBits/VerilogEval-style smoke dataset for the HDL agent.

This is deliberately small: it validates the training plumbing, not model
quality.  The recommended next public benchmark for scale-up is VerilogEval;
the schema emitted here is compatible with the same judge/reward path.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """You are an expert digital hardware engineer.
Write synthesizable HDL that exactly matches the requested module interface.

You have access to HDL tools:
- compile_hdl: syntax, semantic, Chisel elaboration, and Verilator lint checks.
- simulate_hdl: Verilator compile/run simulation. Pass testbench_cpp to build a
  real C++-driven simulator and check behavior. If you pass only RTL, the tool
  runs an automatic smoke harness that checks simulator build/run viability,
  not functional correctness, and it does not receive functional simulation credit.
- synthesize_hdl: Yosys synthesizability check.

Preferred workflow:
1. Draft a complete implementation.
2. Call compile_hdl and fix any reported syntax, semantic, or lint issue.
3. After compile_hdl passes, call simulate_hdl to check functional behavior.
4. For functional checks, write your own Verilator C++ testbench and pass it as
   testbench_cpp, following the XiangShan-style pattern of building a Verilator
   executable and driving it from C++. If simulation fails, revise the RTL and
   repeat compile/sim.
5. After simulation passes, call synthesize_hdl to check implementation readiness.
6. Return the final corrected code block only.

Do not stop after a syntax-only pass when simulation or synthesis tools are
available. For Qwen tool calling, emit tool calls in the normal
<tool_call><function=...> XML format. After using tools, revise the code based
on the feedback. Your final answer must be one complete code block in the
requested language with no prose outside the code block."""


def _task_mux2() -> dict[str, Any]:
    return {
        "task_id": "sv_mux2_8bit",
        "name": "8-bit 2:1 mux",
        "language": "systemverilog",
        "top_module": "Mux2",
        "synthesis": True,
        "testbench_cpp": r'''
#include "VMux2.h"
#include "verilated.h"

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VMux2 dut;
    for (int a = 0; a < 256; a += 17) {
        for (int b = 0; b < 256; b += 31) {
            dut.a = a;
            dut.b = b;
            dut.sel = 0;
            dut.eval();
            if (dut.y != a) return 10;
            dut.sel = 1;
            dut.eval();
            if (dut.y != b) return 11;
        }
    }
    return 0;
}
'''.strip(),
    }


def _task_counter() -> dict[str, Any]:
    return {
        "task_id": "sv_counter4_enable_reset",
        "name": "4-bit enabled counter",
        "language": "systemverilog",
        "top_module": "Counter",
        "synthesis": True,
        "testbench_cpp": r'''
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
    for (int i = 0; i < 5; ++i) tick(dut);
    if (dut.count != 5) return 11;
    dut.en = 0;
    tick(dut);
    if (dut.count != 5) return 12;
    dut.en = 1;
    for (int i = 0; i < 12; ++i) tick(dut);
    if (dut.count != 1) return 13;
    return 0;
}
'''.strip(),
    }


def _task_popcount() -> dict[str, Any]:
    return {
        "task_id": "sv_popcount4",
        "name": "4-bit population count",
        "language": "systemverilog",
        "top_module": "PopCount4",
        "synthesis": True,
        "testbench_cpp": r'''
#include "VPopCount4.h"
#include "verilated.h"

static int popcount4(int value) {
    int count = 0;
    for (int i = 0; i < 4; ++i) count += (value >> i) & 1;
    return count;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VPopCount4 dut;
    for (int value = 0; value < 16; ++value) {
        dut.in = value;
        dut.eval();
        if (dut.count != popcount4(value)) return 20 + value;
    }
    return 0;
}
'''.strip(),
    }


def _task_edge_detect() -> dict[str, Any]:
    return {
        "task_id": "sv_rising_edge_detector",
        "name": "Rising-edge detector",
        "language": "systemverilog",
        "top_module": "EdgeDetect",
        "synthesis": True,
        "testbench_cpp": r'''
#include "VEdgeDetect.h"
#include "verilated.h"

static void tick(VEdgeDetect& dut) {
    dut.clk = 0;
    dut.eval();
    dut.clk = 1;
    dut.eval();
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VEdgeDetect dut;
    dut.rst = 1;
    dut.signal = 0;
    tick(dut);
    if (dut.rise != 0) return 10;
    dut.rst = 0;
    tick(dut);
    if (dut.rise != 0) return 11;
    dut.signal = 1;
    tick(dut);
    if (dut.rise != 1) return 12;
    tick(dut);
    if (dut.rise != 0) return 13;
    dut.signal = 0;
    tick(dut);
    if (dut.rise != 0) return 14;
    dut.signal = 1;
    tick(dut);
    if (dut.rise != 1) return 15;
    return 0;
}
'''.strip(),
    }


def _task_chisel_adder() -> dict[str, Any]:
    return {
        "task_id": "chisel_tiny_adder8",
        "name": "Chisel 8-bit adder",
        "language": "chisel",
        "top_module": "TinyAdder",
        "synthesis": True,
        "verilator_lint_flags": ["-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSED"],
        "testbench_cpp": r'''
#include "VTinyAdder.h"
#include "verilated.h"

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VTinyAdder dut;
    dut.clock = 0;
    dut.reset = 0;
    for (int a = 0; a < 256; a += 19) {
        for (int b = 0; b < 256; b += 37) {
            dut.io_a = a;
            dut.io_b = b;
            dut.eval();
            if (dut.io_y != ((a + b) & 0xff)) return 30;
        }
    }
    return 0;
}
'''.strip(),
    }


def _task_reference_solution(task_id: str) -> str:
    references = {
        "sv_mux2_8bit": """```systemverilog
module Mux2 (
  input  logic       sel,
  input  logic [7:0] a,
  input  logic [7:0] b,
  output logic [7:0] y
);
  assign y = sel ? b : a;
endmodule
```""",
        "sv_counter4_enable_reset": """```systemverilog
module Counter (
  input  logic       clk,
  input  logic       rst,
  input  logic       en,
  output logic [3:0] count
);
  always_ff @(posedge clk) begin
    if (rst) begin
      count <= 4'd0;
    end else if (en) begin
      count <= count + 4'd1;
    end
  end
endmodule
```""",
        "sv_popcount4": """```systemverilog
module PopCount4 (
  input  logic [3:0] in,
  output logic [2:0] count
);
  assign count = {2'b0, in[0]} + {2'b0, in[1]} + {2'b0, in[2]} + {2'b0, in[3]};
endmodule
```""",
        "sv_rising_edge_detector": """```systemverilog
module EdgeDetect (
  input  logic clk,
  input  logic rst,
  input  logic signal,
  output logic rise
);
  logic prev;
  always_ff @(posedge clk) begin
    if (rst) begin
      prev <= 1'b0;
      rise <= 1'b0;
    end else begin
      rise <= signal & ~prev;
      prev <= signal;
    end
  end
endmodule
```""",
        "chisel_tiny_adder8": """```scala
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
```""",
    }
    return references[task_id]


def task_specs() -> list[dict[str, Any]]:
    return [_task_mux2(), _task_counter(), _task_popcount(), _task_edge_detect(), _task_chisel_adder()]


def _visible_task(task: dict[str, Any]) -> dict[str, Any]:
    visible = dict(task)
    visible.pop("testbench_cpp", None)
    return visible


def _prompt_for_task(task: dict[str, Any]) -> str:
    task_id = task["task_id"]
    if task_id == "sv_mux2_8bit":
        spec = """Write SystemVerilog for module Mux2.

Interface:
module Mux2(
  input  logic       sel,
  input  logic [7:0] a,
  input  logic [7:0] b,
  output logic [7:0] y
);

Behavior: y equals a when sel is 0, otherwise y equals b."""
    elif task_id == "sv_counter4_enable_reset":
        spec = """Write SystemVerilog for module Counter.

Interface:
module Counter(
  input  logic       clk,
  input  logic       rst,
  input  logic       en,
  output logic [3:0] count
);

Behavior: on each rising clock edge, rst synchronously clears count to 0.
When rst is low and en is high, count increments modulo 16. When en is low,
count holds its value."""
    elif task_id == "sv_popcount4":
        spec = """Write SystemVerilog for module PopCount4.

Interface:
module PopCount4(
  input  logic [3:0] in,
  output logic [2:0] count
);

Behavior: count is the number of one bits in in."""
    elif task_id == "sv_rising_edge_detector":
        spec = """Write SystemVerilog for module EdgeDetect.

Interface:
module EdgeDetect(
  input  logic clk,
  input  logic rst,
  input  logic signal,
  output logic rise
);

Behavior: rst synchronously clears internal state and rise to 0. Otherwise,
rise is a one-cycle pulse when signal changes from 0 in the previous cycle to
1 in the current cycle."""
    elif task_id == "chisel_tiny_adder8":
        spec = """Write a complete Scala/Chisel source file.

Requirements:
- import chisel3._ and circt.stage.ChiselStage.
- define class TinyAdder(width: Int) extends Module.
- TinyAdder has IO fields a, b, and y, all UInt(width.W).
- y is the low width bits of a + b.
- define object Elaborate extends App that prints ChiselStage.emitSystemVerilog(new TinyAdder(8), ...).

Return the Scala code in a single ```scala code block."""
    else:
        raise KeyError(task_id)
    return spec


def _make_sample(task: dict[str, Any], index: int, split: str, hidden_tests_path: Path) -> dict[str, Any]:
    visible_task = _visible_task(task)
    task_payload = json.dumps(visible_task, ensure_ascii=False, sort_keys=True)
    meta = {
        "benchmark": "hdlbits_style_smoke",
        "public_scaleup_recommendation": "VerilogEval",
        "task_id": task["task_id"],
        "split": split,
    }
    return {
        "data_source": "hdl_agent_smoke",
        "metric_data_source": "hdlbits_style_smoke",
        "agent_name": "hdl_agent",
        "prompt": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _prompt_for_task(task)},
        ],
        "ability": "hdl_generation",
        "domain": "hardware_design",
        "reward_model": {"style": "rule", "ground_truth": task_payload},
        "extra_info": {
            "index": index,
            "hdl_task_id": task["task_id"],
            "hdl_language": task["language"],
            "hdl_hidden_tests_path": str(hidden_tests_path),
            "meta_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "interaction_kwargs": {},
            "tools_kwargs": {},
            "need_tools_kwargs": False,
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_dataset(output_dir: Path, *, train_repeat: int, seed: int) -> None:
    tasks = task_specs()
    rng = random.Random(seed)
    hidden_tests_path = output_dir / "hidden_tests.jsonl"
    hidden_rows = [
        {
            "task_id": task["task_id"],
            "language": task["language"],
            "top_module": task.get("top_module"),
            "testbench_cpp": task.get("testbench_cpp"),
        }
        for task in tasks
    ]
    _write_jsonl(hidden_tests_path, hidden_rows)

    train_rows: list[dict[str, Any]] = []
    for repeat in range(train_repeat):
        order = list(tasks)
        rng.shuffle(order)
        for task in order:
            train_rows.append(_make_sample(task, len(train_rows), "train", hidden_tests_path))

    val_rows = [_make_sample(task, index, "val", hidden_tests_path) for index, task in enumerate(tasks)]
    ref_rows = [
        {
            "task_id": task["task_id"],
            "language": task["language"],
            "solution": _task_reference_solution(task["task_id"]),
            "ground_truth": json.dumps(_visible_task(task), ensure_ascii=False, sort_keys=True),
            "extra_info": {
                "hdl_hidden_tests_path": str(hidden_tests_path),
            },
        }
        for task in tasks
    ]

    _write_jsonl(output_dir / "train.jsonl", train_rows)
    _write_jsonl(output_dir / "val.jsonl", val_rows)
    _write_jsonl(output_dir / "reference_solutions.jsonl", ref_rows)
    (output_dir / "README.md").write_text(
        "# HDL Agent Smoke Benchmark\n\n"
        "Small HDLBits-style tasks for validating the HDL agent loop, reward computation, "
        "and PPO training plumbing.  Use VerilogEval as the recommended lightweight "
        "public scale-up benchmark once the workflow is stable.\n\n"
        "The train/val ground_truth payloads intentionally omit hidden testbenches. "
        "Evaluation testbenches live in hidden_tests.jsonl and are merged only by "
        "the reward/evaluation path. Rollout-time simulate_hdl calls must use a "
        "model-provided testbench_cpp or the automatic smoke harness.\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="data/hdl_agent_smoke")
    parser.add_argument("--train-repeat", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    build_dataset(Path(args.output_dir), train_repeat=args.train_repeat, seed=args.seed)


if __name__ == "__main__":
    main()
