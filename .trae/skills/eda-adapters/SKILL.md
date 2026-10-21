# Agent Workbench EDA Adapter Skills

Use the adapter matching the tool that actually produced the report. Never guess a
verdict: the acceptance gate is three-state (met / unmet / uncertain) and a missing
report must stay **uncertain**, never "pass".

Two ways to get facts, both supported by `POST /api/eda/run` and
`POST /api/eda/parse-report`:

- **Run the tool** — give a script, the backend executes the real binary and parses
  its stdout. Requires explicit confirmation; only the registered binaries
  (`vivado` / `klayout` / `vcs`) can be launched, never an arbitrary shell.
- **Parse an existing report** — paste or fetch the report text. The parser is
  chosen by `(tool, kind)`; an unknown pair fails closed with
  `unsupported_parser:<tool>/<kind>`.

## Vivado
- Kinds: `timing` (`report_timing_summary`), `utilization` (`report_utilization`).
- The timing parser wants the **Design Summary table**: a header line containing
  both `WNS(ns)` and `TNS(ns)`, followed by a row of six numbers
  `WNS TNS <Failing> <Total> WHS THS`. Clock rows look like
  `clk_name {0.000 3.333} 6.667 150.000`. If the flow only prints `WNS = -0.123 ns`,
  that fallback is parsed too.
- Utilization parser wants the resource table (`LUT as Logic`, `Slice LUTs`,
  `Block RAM Tile`, `DSPs`); a resource it cannot find is listed in `missing`,
  never counted as zero.
- Script entry point: `vivado -mode batch -nojournal -source <script>.tcl`.
- Never edit a `.dcp`/checkpoint or a netlist; work on the source RTL and re-run.

## KLayout
- Kind: `drc` (magic `klayout -rd mode=drc -zz`).
- The parser prefers the **XML** report (`<report><checks><check description=…><item/>`),
  counting `<item>` per `<check>`; it falls back to text summaries like
  `Total violations: N`.
- A DRC run with zero violations is a **met** fact only when the report really was
  produced; an empty or unreadable report stays uncertain.
- KLayout DRC decks are Python; keep them inside the task region and do not run
  arbitrary user-supplied decks without confirmation.

## Verdi
- Kind: `simulation` (driven by `vcs` / `verdi`).
- Simulation facts must carry the failing check names and their severity; a
  simulation that did not reach the assertion is not a pass.
- Do not shorten a regression suite to make it green; report the reduced scope.

## Quartus
- Not yet parsed by `eda_tcl`; use it for guidance only, and route its reports
  through the generic `parse-report` path knowing the structured facts will be
  `unsupported_parser:quartus/…` (uncertain, by design).

## Innovus
- Same as Quartus: guidance only until a parser exists. Never translate a
  Innovus summary into a pass by eye.

## Shared workflow
1. Identify the tool and the report kind (`tool` + `kind`) before doing anything.
2. Read the project's report scripts / constraints in the active task region.
3. Produce the report — run the real tool (`/api/eda/run`, with confirmation) or
   collect the existing report text.
4. Parse it into structured facts (`/api/eda/parse-report`); keep the provenance
   lines so every number stays traceable to a report line.
5. Feed facts and the visual signal into the acceptance gate
   (`/api/eda/verify` or `/api/eda/report-gate`); state `critical` explicitly when
   the project has its own decisive metrics.
6. Treat `uncertain` as "needs a human": escalate, never silently pass.
7. The gate only **judges**. Rollback stays a suggestion and must go through the
   human approval gate (`regions.rollback_changeset` with `require_approval`).
