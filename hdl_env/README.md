# Isolated HDL Toolchain

This directory contains a repo-local HDL toolchain bootstrap for the HDL RL
environment. It does not install packages globally and does not modify the
existing RL Python environment.

Build or rebuild everything:

```bash
bash hdl_env/rebuild_hdl_env.sh
```

Verify the installed tools:

```bash
bash hdl_env/smoke_test_hdl_env.sh
```

Verify the XiangShan-style simulator pattern used by the RL tools:

```bash
bash hdl_env/smoke_test_xiangshan_style.sh
```

Use the environment in an interactive shell:

```bash
source hdl_env/env.sh
```

The current core path is:

- Slang-backed SystemVerilog parsing and semantic checking via the official
  `pyslang` bindings and a local `slang` wrapper.
- Verilator for linting and fast executable simulation with a C++ harness.
- Yosys for synthesis checks.
- Maven plus Chisel for Scala/Chisel elaboration into SystemVerilog.

For this project, "aligned with XiangShan" means the open-source side of the
flow should look like a compiled simulator workflow rather than an ad hoc
SystemVerilog testbench workflow:

1. Scala/Chisel, when used, elaborates to SystemVerilog.
2. Slang checks SystemVerilog syntax and semantics.
3. Verilator builds a simulator executable from RTL plus a C++ harness.
4. The harness drives tests/workloads and returns pass/fail through process
   status.
5. Yosys checks synthesizability for lightweight implementation feedback.

XiangShan itself layers a much larger Chisel generator, Verilator/VCS
simulation, workloads, and difftest infrastructure around this idea. The local
RL environment intentionally keeps the same executable-simulator shape while
remaining small enough for rollout-time tool calls.

On this machine there is no Docker/Podman/Singularity runtime on `PATH`, so the
default bootstrap uses a repo-local prefix at `hdl_env/prefix` instead of a
container. It extracts Ubuntu `.deb` packages into that prefix, installs Python
packages into `hdl_env/prefix/python_site`, and caches Maven dependencies under
`hdl_env/cache/m2`.

The optional OSS CAD Suite path is still available on machines with reliable
GitHub release downloads:

```bash
HDL_ENV_INSTALL_MODE=oss_cad_suite bash hdl_env/rebuild_hdl_env.sh
```

Generated tools, downloads, and caches live under `hdl_env/` and are ignored by
git.
