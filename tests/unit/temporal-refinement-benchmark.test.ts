import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

type BenchmarkCase = {
  case_id: string;
  expected_video_id: string;
  source: "local" | "web";
  duration_ms: number;
  truth_timestamp_ms: number;
};

function syntheticCases(): BenchmarkCase[] {
  const durations = [240_000, 600_000, 1_200_000];
  const sources = ["local", "web"] as const;
  const cases: BenchmarkCase[] = [];
  let index = 0;
  for (const source of sources) {
    for (const duration_ms of durations) {
      for (let segmentCase = 0; segmentCase < 5; segmentCase += 1) {
        cases.push({
          case_id: `positive-${String(index).padStart(3, "0")}`,
          expected_video_id: `video-${String(index).padStart(3, "0")}`,
          source,
          duration_ms,
          truth_timestamp_ms: Math.floor(duration_ms / 2),
        });
        index += 1;
      }
    }
  }
  return cases;
}

function runBenchmark(manifest: unknown): { status: number; output: string } {
  const root = resolve(process.cwd());
  const fixtureRoot = mkdtempSync(join(tmpdir(), "xtrace-temporal-benchmark-"));
  const manifestPath = join(fixtureRoot, "manifest.json");
  const outputPath = join(fixtureRoot, "report.json");
  writeFileSync(manifestPath, JSON.stringify(manifest), "utf8");
  try {
    try {
      execFileSync(
        "uv",
        [
          "run",
          "--project",
          join(root, "services/api"),
          "python",
          join(root, "scripts/benchmark_temporal_refinement.py"),
          "--manifest",
          manifestPath,
          "--output",
          outputPath,
        ],
        { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
      );
      return { status: 0, output: readFileSync(outputPath, "utf8") };
    } catch (error) {
      const processError = error as { status?: number; stdout?: string; stderr?: string };
      return {
        status: processError.status ?? 1,
        output: `${processError.stdout ?? ""}${processError.stderr ?? ""}${String(error)}`,
      };
    }
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true });
  }
}

describe("temporal refinement benchmark fixture contract", () => {
  it("builds 30 unique paired positives across both sources and duration segments", () => {
    const cases = syntheticCases();
    expect(cases).toHaveLength(30);
    expect(new Set(cases.map((item) => item.case_id)).size).toBe(30);
    expect(new Set(cases.map((item) => item.expected_video_id)).size).toBe(30);
    expect(new Set(cases.map((item) => item.source))).toEqual(new Set(["local", "web"]));
    expect(new Set(cases.map((item) => item.duration_ms))).toEqual(
      new Set([240_000, 600_000, 1_200_000]),
    );
  });

  it("keeps an insufficient manifest fail-closed before any media/network work", () => {
    const result = runBenchmark({ cases: syntheticCases().slice(0, 29) });

    expect(result.status).not.toBe(0);
    expect(result.output).toMatch(/minimum_positive_cases|cobertura insuficiente/i);
  });
});
