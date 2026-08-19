"""Run the metadata-only temporal-refinement adoption benchmark.

The manifest must contain independent truth and observations for the ``base`` and
``refined`` policies.  This command never opens a query image, downloads an asset,
starts a socket, or invokes the search service.  It only reads JSON metadata and
writes the resulting report to a path outside the Git checkout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Keep the documented ``python scripts/...`` invocation usable from a checkout,
# while uv-installed API environments continue to resolve the same package.
_API_ROOT = Path(__file__).resolve().parents[1] / "services" / "api"
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from xtrace_api.refinement.benchmark import (
    BenchmarkError,
    compare_refinement_policies,
    load_temporal_benchmark_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="JSON metadata manifest with cases, base observations, and refined observations",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="report JSON path outside the Git checkout",
    )
    return parser


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_output_is_external(output: Path) -> Path:
    resolved = output.expanduser().resolve()
    root = _repository_root()
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise BenchmarkError(
        "la salida del benchmark debe estar fuera del checkout Git "
        f"({root}); se rechazó '{resolved}'"
    )


def run(manifest_path: Path, output_path: Path) -> int:
    """Load, compare, and persist one report; return a process exit status."""

    output = _ensure_output_is_external(output_path)
    manifest = load_temporal_benchmark_manifest(manifest_path)
    report = compare_refinement_policies(
        manifest.cases,
        base=manifest.base,
        refined=manifest.refined,
    )

    # The output is deliberately created only after parsing and comparison.  No
    # partial/invalid manifest can trigger any media or network work, and the
    # report remains useful as an explicit rejected-run diagnostic.
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_json(), encoding="utf-8")
    if not report.accepted:
        details = "; ".join(report.coverage.errors)
        if not details:
            details = ", ".join(
                (
                    *report.missing_base_case_ids,
                    *report.missing_refined_case_ids,
                    *report.duplicate_observation_ids,
                    *report.extra_observation_ids,
                    *report.observation_id_mismatches,
                    *report.invalid_observation_ids,
                )
            )
        if not details:
            details = "la comparación no supera las puertas de adopción"
        raise BenchmarkError(
            f"cobertura o pares inválidos; benchmark rechazado: {details}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args.manifest, args.output)
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"benchmark temporal rechazado: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
