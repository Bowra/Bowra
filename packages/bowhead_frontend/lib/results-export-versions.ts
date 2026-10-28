import type { ExportJobDTO } from "@/lib/api";

export type ResultExportVersion = {
  artifactId: string;
  jobId: string;
  createdAt: string;
  updatedAt: string;
};

export type ResultExportVersionGroup = {
  key: string;
  canvasId: string;
  fmt: string;
  latestJobId: string;
  latestStatus: ExportJobDTO["status"];
  currentArtifactId: string | null;
  exportJobCount: number;
  versions: ResultExportVersion[];
};

function compareJobsNewestFirst(a: ExportJobDTO, b: ExportJobDTO): number {
  const created = b.createdAt.localeCompare(a.createdAt);
  return created || b.jobId.localeCompare(a.jobId);
}

/** Project durable export job history into format-scoped version groups. */
export function projectResultExportVersions(
  jobs: readonly ExportJobDTO[],
): ResultExportVersionGroup[] {
  const byGroup = new Map<string, ExportJobDTO[]>();
  for (const job of jobs) {
    if (!job.canvasId || !job.fmt) continue;
    const key = `${job.canvasId}:${job.fmt.toLowerCase()}`;
    const group = byGroup.get(key);
    if (group) group.push(job);
    else byGroup.set(key, [job]);
  }

  const groups: ResultExportVersionGroup[] = [];
  for (const [key, unsorted] of byGroup) {
    const sorted = [...unsorted].sort(compareJobsNewestFirst);
    const latest = sorted[0];
    if (!latest) continue;
    const seenArtifacts = new Set<string>();
    const versions: ResultExportVersion[] = [];
    for (const job of sorted) {
      if (job.status !== "done" || !job.artifactId) continue;
      if (seenArtifacts.has(job.artifactId)) continue;
      seenArtifacts.add(job.artifactId);
      versions.push({
        artifactId: job.artifactId,
        jobId: job.jobId,
        createdAt: job.createdAt,
        updatedAt: job.updatedAt,
      });
    }
    groups.push({
      key,
      canvasId: latest.canvasId,
      fmt: latest.fmt.toLowerCase(),
      latestJobId: latest.jobId,
      latestStatus: latest.status,
      currentArtifactId: versions[0]?.artifactId ?? null,
      exportJobCount: sorted.length,
      versions,
    });
  }
  return groups.sort((a, b) => a.key.localeCompare(b.key));
}
