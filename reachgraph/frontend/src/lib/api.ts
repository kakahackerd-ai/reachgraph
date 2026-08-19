import type { Ecosystem, LookupResponse, ScanJob, ScanRepoAck } from './types'

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = (await res.json()) as T
  if (!res.ok && !(data as { status?: string }).status) {
    throw new Error(`request failed: ${res.status}`)
  }
  return data
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`request failed: ${res.status}`)
  return (await res.json()) as T
}

export function lookupPackage(
  ecosystem: Ecosystem,
  packageName: string,
  version?: string,
  maxDependents = 100,
): Promise<LookupResponse> {
  return postJson<LookupResponse>('/api/v2/lookup', {
    ecosystem,
    package: packageName,
    version: version || undefined,
    max_dependents: maxDependents,
  })
}

export function scanRepo(target: string): Promise<ScanRepoAck> {
  return postJson<ScanRepoAck>('/api/v2/scan-repo', { target })
}

export function getJob(jobId: string): Promise<ScanJob> {
  return getJson<ScanJob>(`/api/v2/jobs/${jobId}`)
}

export async function pollJob(
  jobId: string,
  onUpdate: (job: ScanJob) => void,
  { intervalMs = 900, timeoutMs = 120_000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<ScanJob> {
  const start = Date.now()
  for (;;) {
    const job = await getJob(jobId)
    onUpdate(job)
    if (job.status === 'completed' || job.status === 'failed') return job
    if (Date.now() - start > timeoutMs) throw new Error('scan timed out')
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}
