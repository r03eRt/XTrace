"""Barrido de configuraciones de frames/vídeo (PR-017 · SC-001 · spec §62/§77).

Helper REPRODUCIBLE del sweep de frames/vídeo (10/30/60) usado para el
informe de decisión `docs/handoffs/PR-017.md`. NO contiene lógica de
dominio: solo orquesta los comandos CLI existentes (`index`, PR-011, y
`benchmark`, PR-016) para cada configuración y consolida los informes
JSON del contrato CLI §1 (PR-016) en un directorio de salida, sin
timestamps (SC-007): mismo dataset + mismo manifest + misma semilla
producen los mismos informes.

Comandos equivalentes por configuración (lo que este script ejecuta):

    # N=10
    uv run xtrace-spike index --dataset <DATASET> --frames-per-video 10 --provider siglip
    uv run xtrace-spike benchmark --cases <CASES> --top-k 10 --min-score 0.8 --provider siglip

    # N=30
    uv run xtrace-spike index --dataset <DATASET> --frames-per-video 30 --provider siglip
    uv run xtrace-spike benchmark --cases <CASES> --top-k 10 --min-score 0.8 --provider siglip

    # N=60
    uv run xtrace-spike index --dataset <DATASET> --frames-per-video 60 --provider siglip
    uv run xtrace-spike benchmark --cases <CASES> --top-k 10 --min-score 0.8 --provider siglip

Uso:

    uv run python -m xtrace_spike.benchmark.sweep \
        --dataset <DATASET> --cases <CASES> [--frames 10 30 60] \
        [--min-score 0.8] [--provider siglip] [--out sweep-out/] [--dry-run]

Backend: `index` y `benchmark` deben compartir índice. Con
`SUPABASE_DB_URL` definida se usa pgvector/HNSW persistente (PR-007);
sin ella el backend in-memory es volátil entre procesos (ADR-0006) y el
sweep no es válido (el benchmark no vería los frames indexados).
Latencia/throughput fluctúan entre ejecuciones y se reportan con
precisión estable (ver PR-016.md).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

DEFAULT_FRAMES: tuple[int, ...] = (10, 30, 60)
DEFAULT_MIN_SCORE: float = 0.8
DEFAULT_PROVIDER: str = "siglip"
DEFAULT_TOP_K: int = 10

#: Claves del informe del contrato CLI §1 (PR-016) del resumen comparativo.
SUMMARY_KEYS: tuple[str, ...] = (
    "top1",
    "top5",
    "top10",
    "false_positive_rate_negatives",
    "frames_per_video_avg",
    "embedding_throughput_fps",
)


def _cli_prefix() -> list[str]:
    """Prefijo para invocar la CLI: entry point instalado o python -c.

    Devuelve el binario `xtrace-spike` si está en el PATH (venv/uv) y,
    si no, invoca la app Typer con `python -c` (mismo contrato CLI §1).
    """
    binary = shutil.which("xtrace-spike")
    if binary is not None:
        return [binary]
    return [sys.executable, "-c", "from xtrace_spike.cli import app; app()"]


def _run(cli: list[str], dry_run: bool, parse_stdout: bool) -> dict[str, Any] | None:
    """Ejecuta un comando CLI y, si parse_stdout, devuelve su JSON de stdout.

    Solo "benchmark" emite el informe JSON del contrato CLI §1 por stdout
    (PR-016) y se parsea, de forma defensiva (stdout no-JSON -> error
    claro). "index" no se parsea: su stdout no es JSON utilizable en todos
    los providers (con siglip aparecen mensajes de carga del modelo);
    solo interesa el exit code (0 = indexación correcta). None en dry-run.
    """
    if dry_run:
        print("$ " + " ".join(cli))
        return None
    proc = subprocess.run(cli, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"comando falló (exit {proc.returncode}): {' '.join(cli)} stderr: {proc.stderr[-2000:]}"
        )
    if not parse_stdout:
        return None
    try:
        return cast(dict[str, Any], json.loads(proc.stdout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"stdout del comando no es JSON utilizable: {exc} "
            f"(comando: {' '.join(cli)}; stderr: {proc.stderr[-2000:]})"
        ) from exc


def _sweep(
    dataset: Path,
    cases: Path,
    frames: Sequence[int],
    min_score: float,
    provider: str,
    top_k: int,
    out: Path,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Ejecuta index + benchmark por configuración y persiste los informes."""
    out.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    for n in frames:
        prefix = _cli_prefix()
        _run(
            [
                *prefix,
                "index",
                "--dataset",
                str(dataset),
                "--frames-per-video",
                str(n),
                "--provider",
                provider,
            ],
            dry_run=dry_run,
            parse_stdout=False,  # index: solo exit code (stdout no utilizable)
        )
        report = _run(
            [
                *prefix,
                "benchmark",
                "--cases",
                str(cases),
                "--top-k",
                str(top_k),
                "--min-score",
                str(min_score),
                "--provider",
                provider,
            ],
            dry_run=dry_run,
            parse_stdout=True,  # benchmark: informe JSON del contrato CLI §1
        )
        if report is None:
            continue
        target = out / f"report_frames_{n}.json"
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"informe {n} frames/vídeo -> {target}")
        reports.append({"frames_per_video_config": n, **report})
    return reports


def _print_summary(reports: Sequence[dict[str, Any]]) -> None:
    """Imprime la tabla comparativa precisión/coste del sweep."""
    header = " | ".join(["N", *SUMMARY_KEYS])
    print(f"\n== Resumen sweep ==\n{header}")
    for report in reports:
        row = [str(report["frames_per_video_config"])]
        for key in SUMMARY_KEYS:
            value = report.get(key)
            if value is None:
                row.append("-")
            elif isinstance(value, float):
                row.append(f"{value:.4f}")
            else:
                row.append(str(value))
        print(" | ".join(row))


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: ejecuta el sweep completo (index + benchmark por N)."""
    parser = argparse.ArgumentParser(
        description="Sweep de frames/vídeo (10/30/60) del benchmark (PR-017)."
    )
    parser.add_argument(
        "--dataset",
        required=True,
        type=Path,
        help="directorio raíz del dataset local de vídeos (FR-001)",
    )
    parser.add_argument(
        "--cases",
        required=True,
        type=Path,
        help="directorio del dataset de benchmark (manifest.json, PR-015)",
    )
    parser.add_argument(
        "--frames",
        nargs="+",
        type=int,
        default=list(DEFAULT_FRAMES),
        help="configuraciones de frames/vídeo (default: 10 30 60)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help="umbral de match para el FPR de negativas (SC-002)",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        help="proveedor de embeddings: siglip (default) o fake",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="frames candidatos del ANN (contracts §1)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("sweep-out"),
        help="directorio de salida de los informes (default: sweep-out/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="solo imprime los comandos, sin ejecutarlos",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.frames:
        parser.error("--frames requiere al menos un valor")
    if not args.dry_run and not os.environ.get("SUPABASE_DB_URL"):
        parser.error(
            "SUPABASE_DB_URL no está definida: index y benchmark deben "
            "compartir índice (backend pgvector/HNSW persistente, PR-007); "
            "sin ella el backend in-memory es volátil entre procesos "
            "(ADR-0006) y el sweep sería inválido"
        )

    reports = _sweep(
        dataset=args.dataset,
        cases=args.cases,
        frames=args.frames,
        min_score=args.min_score,
        provider=args.provider,
        top_k=args.top_k,
        out=args.out,
        dry_run=args.dry_run,
    )
    if reports:
        summary = {
            "frames_configs": [report["frames_per_video_config"] for report in reports],
            "reports": reports,
        }
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        _print_summary(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
