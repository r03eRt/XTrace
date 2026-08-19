import { describe, expect, it } from "vitest";

import { searchResponseSchema } from "@/lib/api/schemas";

const baseResult = {
  video_id: "1a2b3c4d-0000-0000-0000-000000000001",
  local_ref: null,
  match_score: 0.938,
  matching_frames: 1,
  match_timestamp_ms: 454000,
  evidence: { visual: 0.99, phash: 0.91 },
};

describe("temporal refinement API contract", () => {
  it("accepts summary and refined timestamp provenance", () => {
    const parsed = searchResponseSchema.safeParse({
      search_id: "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
      processing_ms: 1234,
      refinement: {
        status: "completed",
        candidates_requested: 3,
        candidates_processed: 2,
        assets_evaluated: 18,
        assets_discarded: 1,
        errors_count: 0,
        bytes_downloaded: 184320,
        embedding_count: 18,
        embedding_elapsed_ms: 72,
        improved_results: 1,
        elapsed_ms: 940,
      },
      results: [
        {
          ...baseResult,
          timestamp_provenance: {
            origin: "refined_asset",
            status: "improved",
            source: "xvideos",
            asset_kind: "thumbnail",
            asset_url: "https://thumb-cdn77.xvideos-cdn.com/xv_12_t.jpg",
            asset_position: 12,
          },
        },
      ],
    });

    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.refinement?.status).toBe("completed");
      expect(parsed.data.results[0]!.timestamp_provenance?.origin).toBe("refined_asset");
    }
  });

  it.each(["limited", "unavailable"] as const)(
    "accepts the %s refinement fallback state with base-index provenance",
    (status) => {
      const parsed = searchResponseSchema.safeParse({
        search_id: "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
        processing_ms: 1234,
        refinement: {
          status,
          candidates_requested: 3,
          candidates_processed: 1,
          assets_evaluated: 0,
          assets_discarded: 0,
          errors_count: status === "unavailable" ? 1 : 0,
          bytes_downloaded: 0,
          embedding_count: 0,
          embedding_elapsed_ms: 0,
          improved_results: 0,
          elapsed_ms: 120,
        },
        results: [
          {
            ...baseResult,
            timestamp_provenance: {
              origin: "base_index",
              status,
              source: null,
              asset_kind: null,
              asset_url: null,
              asset_position: null,
            },
          },
        ],
      });

      expect(parsed.success).toBe(true);
      if (parsed.success) {
        expect(parsed.data.refinement?.status).toBe(status);
        expect(parsed.data.results[0]!.timestamp_provenance?.origin).toBe("base_index");
        expect(parsed.data.results[0]!.timestamp_provenance?.status).toBe(status);
      }
    },
  );

  it("accepts a legacy response without refinement fields", () => {
    const parsed = searchResponseSchema.safeParse({
      search_id: "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
      processing_ms: 10,
      results: [baseResult],
    });
    expect(parsed.success).toBe(true);
  });

  it("rejects an unknown refinement summary state", () => {
    const invalidStatus = searchResponseSchema.safeParse({
      search_id: "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
      processing_ms: 10,
      refinement: {
        status: "invented",
        candidates_requested: 0,
        candidates_processed: 0,
        assets_evaluated: 0,
        assets_discarded: 0,
        errors_count: 0,
        bytes_downloaded: 0,
        embedding_count: 0,
        embedding_elapsed_ms: 0,
        improved_results: 0,
        elapsed_ms: 0,
      },
      results: [],
    });
    expect(invalidStatus.success).toBe(false);
  });

  it.each([
    ["origin", "invented"],
    ["status", "invented"],
    ["asset_kind", "preview"],
  ])("rejects invalid provenance %s", (field, value) => {
    const invalidProvenance = searchResponseSchema.safeParse({
      search_id: "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
      processing_ms: 10,
      results: [
        {
          ...baseResult,
          timestamp_provenance: {
            origin: "base_index",
            status: "unchanged",
            source: null,
            asset_kind: null,
            asset_url: null,
            asset_position: null,
            [field]: value,
          },
        },
      ],
    });
    expect(invalidProvenance.success).toBe(false);
  });
});
