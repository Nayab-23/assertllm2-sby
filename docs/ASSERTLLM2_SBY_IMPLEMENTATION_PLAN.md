# AssertLLM2-SBY Implementation Plan

## Objective

Build **AssertLLM2-SBY**, an open-source adaptation of the AssertLLM2 evaluation that uses Polaris/Sable for assertion generation and Yosys/SymbiYosys/Z3 for formal judging. Results must be reported as backend-specific AssertLLM2-SBY results, never as official AssertLLM2 JasperGold scores.

## Non-Goals

- Do not modify `third_party/AssertLLM2`.
- Do not modify benchmark RTL, specs, raw documents, or cached mutants in place.
- Do not claim JasperGold coverage, JasperGold FPV status, or official AssertLLM2 comparability.
- Do not run full benchmark designs until one real design passes the complete adapter path.

## Proposed Directory Layout

- `assertllm2_sby/`
  - `config_loader.py`: read official AssertLLM2 config JSON and normalize design records.
  - `materialize.py`: copy benchmark inputs into immutable per-run work directories.
  - `generator.py`: invoke Polaris/Sable assertion generation with the selected prompt context.
  - `sby_builder.py`: build harnesses, bind files, `.sby` files, and Yosys scripts.
  - `judge.py`: run SBY or `yosys-smtbmc`, classify statuses, collect logs/traces.
  - `mutants.py`: load cached AssertLLM2 mutation metadata and materialize mutant filelists.
  - `manifest.py`: write run manifests and input hashes.
  - `report.py`: aggregate backend-specific metrics.
  - `cli.py`: expose `preflight`, `pilot`, `run-design`, and later `run-suite`.
- `docs/`
  - Keep these project rules, audit, and implementation plan current.
- `runs/assertllm2-sby/<timestamp-or-run-id>/`
  - All generated artifacts, logs, manifests, and reports.

## Phase 1: Adapter Preflight

1. Add a read-only AssertLLM2 design registry loader.
2. Validate official checkout path, commit, and clean state.
3. Load `AssertLLM2/configs/assertllm2_design_configs.json`.
4. Normalize each design into:
   - design key
   - category
   - design directory
   - `spec.md`
   - raw spec documents
   - ordered RTL filelist
   - include dirs
   - top module
   - clocks
   - reset
   - blackbox modules
   - mutation cache paths
5. Emit a preflight report without running formal tools.

Exit criteria: `preflight` writes a manifest with all 83 design keys and no benchmark-source writes.

## Phase 2: One Real Design Materialization

1. Select one real AssertLLM2 design with a small Verilog-only filelist.
2. Copy all inputs to a fresh run work directory.
3. Preserve file order and include dirs exactly from official config.
4. Hash every copied input.
5. Record the AssertLLM2 commit and ASSERTNEURO tree state.

Exit criteria: materialized design directory is complete, deterministic, and independent from `third_party/AssertLLM2`.

## Phase 3: Generator Integration

1. Define bug-prevention prompt mode using only `spec.md` or raw spec input.
2. Ensure the generator cannot access golden RTL or mutants in bug-prevention mode.
3. Define bug-hunting prompt mode separately and record exact visible RTL context.
4. Reuse `polaris-sable/sable/llm_client.py` and `spec_synth.py` where possible.
5. Store generated assertions in the run directory with model, temperature, prompt mode, and source hashes.

Exit criteria: one design produces a syntactically inspectable assertion artifact without seeing disallowed inputs.

## Phase 4: SBY Formal Judge

1. Build an SBY/Yosys path that reads the copied RTL filelist and generated assertions.
2. Support include dirs, defines, parameters, top module, clock/reset metadata, and blackboxes.
3. Prefer `sby` for orchestration; keep a direct `yosys-smtbmc` fallback if needed.
4. Use Z3 initially because Boolector is missing on this machine.
5. Classify outcomes as:
   - `PROVEN`
   - `COUNTEREXAMPLE`
   - `UNKNOWN`
   - `TIMEOUT`
   - `UNSUPPORTED_SYNTAX`
   - `ELABORATION_ERROR`
   - `INFRASTRUCTURE_ERROR`
6. Store all logs, generated scripts, traces, and solver outputs.

Exit criteria: one real design elaborates, assertions bind, and a baseline status report is written.

## Phase 5: Baseline Metrics

1. Report SBY syntax/elaboration success separately from proof outcomes.
2. Count supported assertions, rejected assertions, proven assertions, counterexamples, unknowns, and timeouts.
3. Add vacuity checks only where the open-source flow can support them soundly.
4. Do not emit JasperGold coverage fields.

Exit criteria: baseline report contains only AssertLLM2-SBY-defined metrics and clear unsupported-status accounting.

## Phase 6: Mutation Evaluation

1. Load cached AssertLLM2 mutation metadata from each design's `mutations/` directory.
2. Detect whether full mutant RTL files are present; summaries alone are not scoreable.
3. For each scoreable mutant, construct the mutant filelist by replacing only the mutated copied RTL files in a work directory.
4. First prove or accept usable baseline conditions on golden RTL.
5. A mutant is killed only if the same property set produces a valid counterexample on the mutant and the run did not fail elaboration.
6. Record non-scoreable mutants separately:
   - missing mutant RTL
   - elaboration error
   - unsupported syntax
   - timeout/unknown
   - infrastructure error

Exit criteria: one real design produces a mutation report with honest eligible denominator accounting.

## Phase 7: Reporting

1. Write per-design manifests and summaries.
2. Write suite-level reports only after multiple individual design reports are stable.
3. Include:
   - AssertLLM2 commit
   - ASSERTNEURO commit/tree state
   - Polaris/Sable tree state
   - tool versions
   - solver
   - model and temperature
   - prompt mode
   - exact design key
   - input hashes
   - unsupported reasons
   - fresh output path
4. Label every metric as AssertLLM2-SBY.

Exit criteria: report can be read without confusing it for official AssertLLM2/JasperGold output.

## Phase 8: Smoke Then Suite

1. After one real design succeeds, define a small smoke set with diverse design shapes.
2. Run smoke only in fresh output directories.
3. Expand to a suite only after the smoke report has stable unsupported-status handling.
4. Never overwrite previous smoke or suite outputs.

Exit criteria: smoke suite results are reproducible from committed code, recorded tool versions, and the official AssertLLM2 commit.

## Immediate Next Action

Implement Phase 1 only: create the `assertllm2_sby` package and a `preflight` CLI that reads the official config, validates files, records checkout provenance, and writes a read-only audit manifest. Do not run Yosys, SBY, or any benchmark design in that phase.
