# AssertLLM2-SBY Project Rules

## Name And Scope

- The project name is **AssertLLM2-SBY**.
- Polaris/Sable is the assertion generator for this adaptation.
- Yosys, SymbiYosys, `yosys-smtbmc`, and an SMT solver are the formal judge for this adaptation.
- AssertLLM2-SBY results are backend-specific open-source-flow results. They are not official AssertLLM2 JasperGold results and must not be presented as directly comparable to published JasperGold scores.

## Repository Boundaries

- The official AssertLLM2 checkout is read-only.
- Integration code must live outside `third_party/AssertLLM2`.
- Original benchmark RTL, specs, raw documents, cached mutants, and configs must not be modified in place.
- Runtime materialization must happen in new output directories outside the official checkout.
- Previous results must never be overwritten. Every run must create a fresh, timestamped or content-addressed output directory.

## Evaluation Semantics

- JasperGold-only metrics must not be fabricated.
- Timeout, unknown, unsupported syntax, and infrastructure errors are not proofs.
- An elaboration error is not a mutation kill.
- Unsupported SVA must not be silently weakened. It must be rejected, rewritten with explicit provenance, or marked unsupported.
- Mutation kill requires a usable golden baseline and a real counterexample on the mutant under the same property set.
- Results must distinguish syntax/elaboration failure, proof, counterexample, timeout, unsupported construct, and infrastructure error.

## Generator Visibility

- In bug-prevention mode, the generator may not see golden RTL or mutants.
- In bug-hunting mode, the run manifest must record exactly which RTL context was visible to the generator.
- Any human-in-the-loop correction must be recorded separately from autonomous generation.

## Provenance And Secrets

- Every run must record the exact AssertLLM2 commit.
- Every run must record the exact ASSERTNEURO commit, Polaris/Sable commit or tree state, tool versions, solver, model, temperature, prompt mode, design key, and all input file hashes used by the adapter.
- Secrets must never appear in logs, prompts, manifests, generated artifacts, or reports.
- `.env` must not be printed, parsed into manifests, or copied into run directories.

## Gating Rules

- One real AssertLLM2 design must succeed end-to-end before any smoke run is called successful.
- A smoke or full run may not start until a real design can elaborate, bind generated assertions, and produce a valid AssertLLM2-SBY manifest.
- Any deviation from official AssertLLM2 semantics must be documented in the run report before metrics are reported.
