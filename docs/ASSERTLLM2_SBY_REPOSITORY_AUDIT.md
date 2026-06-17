# AssertLLM2-SBY Repository Audit

Audit date: 2026-06-17.

## Initial Repository State

- Working directory: `/Users/nayab/Downloads/Neuro/AssertNeuro`
- ASSERTNEURO root confirmed by `git rev-parse --show-toplevel`.
- Current branch: `main`
- Current ASSERTNEURO commit: `c95c4d4f9683c8bd201eac1e9ef267771daba7f7`
- Pre-existing dirty worktree entries:
  - `M polaris-sable/sable/oracle_contracts.py`
  - `?? .DS_Store`
  - `?? polaris-sable/marsh/TEST_FIXTURES/memory_request_bug.sv`
  - `?? polaris-sable/marsh/TEST_FIXTURES/memory_request_clean.sv`
  - `?? polaris-sable/tests/test_memory_request.py`
- `.env` is listed in `.gitignore`; its contents were not read or printed.

## AssertLLM2 Checkout

- Location: `third_party/AssertLLM2`
- Checkout type: Git submodule
- Upstream remote: `https://github.com/hkust-zhiyao/AssertLLM2.git`
- Branch: `main`
- Commit: `f66fd20679dfff1de2f6d6e90bc4922d04e6ff62`
- Upstream working tree: clean
- `.gitmodules` now registers `third_party/AssertLLM2`.

## Existing Repository Structure

- `paper1/` and `paper2/` contain Saarthi/agentic formal verification papers. They emphasize multi-agent SVA generation, CEX repair, HIL escalation, RAG/rulebook grounding, coverage closure, and known risks around syntax errors, vacuity, hallucinated signals, and weak coverage.
- `VERIFICATION_ENGINE_BUILD_PLAN.md` is the existing Polaris-Sable/Saarthi master build plan. It says Polaris/Sable is an existing engine to extend, not rebuild, and locks mutation-kill plus vacuity-clean coverage as the honest metric.
- `polar_data/` is a staged external corpus upload, not a git repo. It contains `polaris_real_test_3/`, `sable_benchmark_repos/`, and a repro bundle. It does not appear to contain AssertLLM2 data or an incomplete AssertLLM2 duplicate.
- `polaris-sable/` contains the active engine, tests, local project manifest support, legacy model artifacts, benchmark bundles, prior results, and generated/provenance artifacts.

## Polaris/Sable Invocation Path

- Strict project-mode CLI: `polaris-sable/sable/project_cli.py`
  - Commands: `preflight`, `analyze`, `report`
  - Inputs: `sable.project.json`, explicit source closure, include dirs, defines, parameters, blackboxes, clock/reset, local specs, and `spec_contracts.jsonl`
  - Outputs: `preflight.json`, `analysis_results.jsonl`, `report.json`, `report.md`, and artifacts for non-PROVEN records
- Lower-level oracle CLI: `polaris-sable/sable/oracle_contracts.py`
  - Commands accept `--file`, `--repo`, `--depth`, `--results`, `--keep-workdirs`, `--sv-frontend`
  - Builds harnesses, emits SMT2, invokes `yosys-smtbmc`, parses statuses, and writes JSONL records.
- Existing result behavior:
  - `project_cli.analyze_project` unlinks `analysis_results.jsonl` in its output dir before writing.
  - For AssertLLM2-SBY, a wrapper must avoid reusing this behavior on shared output paths and must allocate a fresh run directory.

## Polaris/Sable Formal Flow

- Tool resolution in `oracle_contracts.py` uses `$YOSYS_BIN`, `$YOSYS_SMTBMC_BIN`, and `$SABLE_FORMAL_SOLVER`, else local defaults.
- Yosys frontend support includes built-in `read_verilog` and optional `read_slang`.
- The formal path emits `write_smt2 -wires` and runs `yosys-smtbmc`.
- Status vocabulary includes `PROVEN`, `COUNTEREXAMPLE`, `INCONCLUSIVE`, `ERROR`, and `SKIPPED`.
- The active oracle generates ready/valid and related contract harnesses from RTL structure. Structured spec contracts are supported by `spec_synth.run_structured_contracts`, but arbitrary official AssertLLM2-style generated SVA binding is not yet an adapter.

## Model And Provider Interface

- `polaris-sable/sable/llm_client.py` uses Anthropic Messages API through `requests`.
- Relevant environment variables include `ANTHROPIC_API_KEY`, `ANTHROPIC_API_URL`, `SABLE_LLM_MODEL`, `SABLE_LLM_TEMPERATURE`, `SABLE_LLM_MAX_TOKENS`, `SABLE_LLM_TIMEOUT`, `SABLE_LLM_LOG`, and `SABLE_ENABLE_CLOUD_LLM`.
- The client logs model, temperature, max tokens, input sizes, and outcome; it does not log API keys.
- `spec_synth.make_spec_llm()` uses a deterministic stub unless cloud LLM is explicitly enabled and an API key is available.

## Reusable Components

- AssertLLM2 design/config discovery can reuse `AssertLLM2/configs/assertllm2_design_configs.json`.
- Polaris/Sable reusable modules:
  - `project_manifest.py` for explicit source closure normalization
  - `project_cli.py` preflight logic, with output-overwrite changes required
  - `oracle_contracts.py` tool resolution, Yosys frontend construction, SMT2 lowering, status classification, and trace capture
  - `spec_synth.py` structured contract execution and validated LLM proposal path
  - `llm_client.py` provider abstraction and provenance
  - `sable_forensics.py` artifact preservation
  - `mutation/scorer.py` kill-rate concepts, but not directly as an AssertLLM2 cache adapter

## RISC-V Specific Code

- RISC-V-related material remains in `polar_data` and prior Polaris/Sable benchmark docs/results, especially CVA6/OpenPiton and repos with `riscv` names.
- The active Sable engine is not exclusively riscv-formal-specific, but old Ariane/OpenPiton benchmark code and results could contaminate reporting if reused as AssertLLM2-SBY outputs.

## Duplicate Or Obsolete Data Risks

- `polaris-sable/sable-model-2/` contains legacy sim-synth divergence results and synthetic mutation data. These must not be reported as AssertLLM2-SBY.
- `polaris-sable/repro/benchmark_bundle/seven_module_benchmark_results.json` is prior benchmark output and unrelated to AssertLLM2-SBY.
- `polaris-sable/sable/findings/` contains previous proof artifacts. They are useful examples but not inputs to AssertLLM2-SBY scoring.
- `polar_data/` is ignored by git and includes large external repos; it is not the AssertLLM2 dataset.

## Official AssertLLM2 Structure

- Dataset layout: `designs/<CATEGORY>/<design_name>/`
- Count observed: 83 designs, 13 top-level categories, 83 `spec.md` files.
- Stable design IDs are keys in `AssertLLM2/configs/assertllm2_design_configs.json`, such as `assertllm2/arithmetic_core/gaussian_noise_generator`.
- Each design config records:
  - `spec_file`
  - `rtl.filelist`
  - `rtl.incdir`
  - `rtl.top_module`
  - `clock_reset.clocks`
  - `clock_reset.reset`
  - `output_dir`
- `spec.md` is stored in each design directory.
- Raw specification documents are stored beside `spec.md` as PDF/PPT/ODT/TXT/MD files.
- Golden RTL top files are stored in the design directory; support RTL is generally under `include/`.
- `jg_bbox.json` exists for 3 designs and declares JasperGold blackbox modules.
- Cached mutation summaries exist for 84 `mutations` directories; many include only `mutation_summary.json`, and at least `COMMUNICATION_CONTROLLER/sata_phy` includes `mutants_index.json` and `spec_mutation_targets.json`.
- Mutation summaries use schema `assertbench_mutations_v1`, `mutant_id` values such as `M_0000`, and mutation logs naming operators such as AVC, SMC, CSW, DMO, CSE, CMC, LOR, DMI, DIE, and CCW.

## Official AssertLLM2 Pipeline

- `AssertLLM2/runner.py` loads a config, detects the top module from the top file, applies bug-hunting/raw-spec output-directory segments, and runs `BenchmarkPipeline`.
- `assertbench/core/pipeline.py` stages:
  - `init_run`
  - `execute_method_bundle`
  - `evaluate_baseline`
  - `evaluate_mutants`
  - `finalize_manifest`
- `assertbench/formal/jasper.py` and `run_jasper.py` are JasperGold-specific.
- Official generated outputs are written under `AssertLLM2/out/<top_module>/`, with files including `manifest.json`, `method_submission.json`, `baseline_eval.json`, `metrics.json`, `mutation_results.json`, `scorecard`, and Jasper work/report directories.
- The Jasper template runs `analyze -sv12`, `elaborate`, `clock`, `reset`, coverage commands, and `report -summary`.
- Official scripts can modify generated assertion files during syntax cleanup by removing erroneous assertion blocks. They should not be used against benchmark source files in AssertLLM2-SBY.

## Official Outcome Semantics

- Baseline evaluation records syntax correctness, FPV result rows, coverage reports, and counts of proven, cex, and undetermined assertions.
- Mutation evaluation runs each mutant RTL filelist through JasperGold and treats failed assertions on a mutant as kills.
- JasperGold-specific coverage types are formal, stimuli, checker COI, and checker proof. AssertLLM2-SBY must not fabricate these metrics.

## Installed And Missing Tools

- OS: macOS Darwin 24.1.0, ARM64 (`arm64`)
- Disk: 460 GiB filesystem, 293 GiB used, 131 GiB available at audit time
- Python: `Python 3.14.5`
- Git: `git version 2.52.0`
- Yosys: `Yosys 0.64`
- SymbiYosys: `sby --version` exits successfully and prints `SBY`
- Z3: `Z3 version 4.15.4 - 64 bit`
- Boolector: missing (`command not found`)
- Docker: `Docker version 29.0.1`
- Homebrew: `Homebrew 6.0.2`
- Active Python virtualenv: none (`VIRTUAL_ENV` empty)

## Major Technical Risks

- Python version mismatch: Polaris/Sable pins Python 3.11, but the active `python3` is 3.14.5.
- Boolector is missing; Sable can use Z3, but any default-to-Boolector path must be forced to Z3.
- SymbiYosys exists, but version reporting is minimal; implementation should verify functional `sby` behavior before relying on it.
- AssertLLM2 contains VHDL extensions in discovery code; Yosys/SBY adaptation may need to mark VHDL or mixed-language designs unsupported unless a sound open-source frontend is added.
- Official configs do not uniformly encode defines/macros beyond include dirs and file order. Some designs may need extra preprocessing or unsupported-syntax classification.
- Official AssertLLM2 results rely on JasperGold coverage and FPV semantics. AssertLLM2-SBY must define a separate status/coverage schema.
- Cached mutation summaries may not always include full mutant files locally; the adapter must detect cache completeness before scoring.
- Existing Sable output routines overwrite fixed output filenames inside a selected output dir; AssertLLM2-SBY must enforce unique run dirs.
- LLM bug-prevention mode must keep the generator away from golden RTL and mutants; existing Sable spec layer can see ports/RTL in some modes, so prompt context must be explicitly controlled.

## Unanswered Implementation Questions

- Which AssertLLM2 designs elaborate under Yosys/SBY without modification?
- Which official SVA constructs generated by Polaris/Sable can be supported by Yosys directly, and which require lowering to immediate/assert constructs?
- How should AssertLLM2-SBY represent clock/reset for multi-clock designs when SBY requires explicit harness semantics?
- How many mutation caches contain complete mutant RTL directories versus summaries only?
- What minimum manifest schema should bridge AssertLLM2 config keys to Sable project configs?
- Should the first real design pilot use an easy single-file design or one with include files to validate source-closure handling?
