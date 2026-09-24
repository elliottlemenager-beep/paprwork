import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/**
 * Issue: a destructive reset proceeded after its own snapshot failed.
 *
 * `writeBootstrapPendingMarker` snapshots the replica with `VACUUM INTO` before sidecars are
 * deleted, so the rows can be replayed once the bootstrap pull lands. The copy was best
 * effort — on failure it logged a warning, recorded `snapshotPath: undefined`, and the
 * caller deleted the sidecars anyway. The next open then reseeded from Turso and
 * `settleBootstrapMarker` saw cloud rows, called the bootstrap verified, and cleared the
 * marker. Local rows that had never been pushed were gone, and the run ended in a success
 * state with nothing surfaced.
 *
 * `VACUUM INTO` writes a full second copy of the database, so the realistic trigger is a
 * disk with less free space than the replica's size — the file itself reads perfectly.
 *
 * These tests pin the narrowing: "readable, populated, and the copy failed" is the one
 * combination that declines the reset. Unreadable (-1) still proceeds, because every caller
 * closes the worker first, so -1 means the file genuinely cannot be read and a copy would
 * not have rescued those rows either.
 */

const calls: string[] = [];

const countUserRows = vi.fn<(p: string) => number>(() => 0);
const snapshotResult = vi.fn<() => string | null>(() => "snapshot.db");
const removeTursoReplicaSidecarsOnly = vi.fn((_p: string) => {
  calls.push("delete");
});
const detectReplicaSidecarWedge = vi.fn(() => true);

vi.mock(
  "../src/gateway/services/tursoReplica/tursoReplicaBootstrapMarker.js",
  () => ({
    countUserRows: (p: string) => countUserRows(p),
    writeBootstrapPendingMarker: (_p: string, reason: string) => {
      calls.push("marker");
      const rows = countUserRows(_p);
      const snapshotPath =
        rows !== 0 ? (snapshotResult() ?? undefined) : undefined;
      const preservationFailed = rows > 0 && !snapshotPath;
      return {
        reason,
        rowsAtRepair: rows,
        snapshotPath,
        preservationFailed: preservationFailed || undefined,
        writtenAtMs: Date.now(),
        attempts: 0,
      };
    },
  }),
);

vi.mock(
  "../src/gateway/services/tursoReplica/tursoReplicaFileGuard.js",
  () => ({
    removeTursoReplicaSidecarsOnly,
  }),
);

const sidecarWedge =
  await import("../src/gateway/services/tursoReplica/tursoReplicaSidecarWedge.js");
const { resetReplicaSidecars, repairReplicaSidecarsOnCheckpointError } =
  sidecarWedge;

let dir: string;
let dbPath: string;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  calls.length = 0;
  vi.clearAllMocks();
  countUserRows.mockReturnValue(0);
  snapshotResult.mockReturnValue("snapshot.db");
  detectReplicaSidecarWedge.mockReturnValue(true);
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "papr-preserve-"));
  dbPath = path.join(dir, "data.db");
  fs.writeFileSync(dbPath, "");
});

afterEach(() => {
  errorSpy.mockRestore();
  fs.rmSync(dir, { recursive: true, force: true });
});

describe("destructive reset declines when preservation failed", () => {
  it("does not delete sidecars when the file holds rows and the snapshot failed", () => {
    countUserRows.mockReturnValue(163);
    snapshotResult.mockReturnValue(null);

    resetReplicaSidecars(dbPath, "engine_panic");

    expect(calls).toContain("marker");
    expect(calls).not.toContain("delete");
    expect(removeTursoReplicaSidecarsOnly).not.toHaveBeenCalled();
  });

  it("says how many rows it refused to discard", () => {
    countUserRows.mockReturnValue(163);
    snapshotResult.mockReturnValue(null);

    resetReplicaSidecars(dbPath, "engine_panic");

    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringContaining("163 rows present"),
    );
    expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("Declining"));
  });

  it("still deletes sidecars when the snapshot succeeded", () => {
    countUserRows.mockReturnValue(163);
    snapshotResult.mockReturnValue("snapshot.db");

    resetReplicaSidecars(dbPath, "engine_panic");

    expect(calls).toEqual(["marker", "delete"]);
    expect(removeTursoReplicaSidecarsOnly).toHaveBeenCalledWith(dbPath);
  });

  it("still deletes sidecars on an unreadable file — -1 is not a preservable row count", () => {
    // Every caller closes the worker first, so -1 means the file genuinely cannot be read.
    // A snapshot would have failed for the same reason, and declining here would wedge a
    // replica that has nothing left to protect.
    countUserRows.mockReturnValue(-1);
    snapshotResult.mockReturnValue(null);

    repairReplicaSidecarsOnCheckpointError(dbPath);

    expect(removeTursoReplicaSidecarsOnly).toHaveBeenCalledWith(dbPath);
  });

  it("still deletes sidecars on an empty file — nothing to preserve", () => {
    countUserRows.mockReturnValue(0);

    repairReplicaSidecarsOnCheckpointError(dbPath);

    expect(removeTursoReplicaSidecarsOnly).toHaveBeenCalledWith(dbPath);
  });
});
