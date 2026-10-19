// Conservative admission check, not a billing meter. Reserve the longest job's
// full 20 minutes for EVERY admitted run (including the shorter ingest).
const token = process.env.GH_TOKEN;
const repo = process.env.GITHUB_REPOSITORY;
if (!token || !repo) throw new Error('Budget guard requires Actions read token and repository');
const month = new Date().toISOString().slice(0, 7) + '-01';
let count = 0;
for (let page = 1; ; page++) {
  if (page > 10) throw new Error('More than 1000 monthly runs: fail closed');
  const url = `https://api.github.com/repos/${repo}/actions/runs?created=%3E%3D${month}&per_page=100&page=${page}`;
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' } });
  if (!response.ok) throw new Error(`Cannot verify monthly budget: HTTP ${response.status}`);
  const data = await response.json();
  // All workflows count, including historic ones and rejected runs. Deliberately
  // conservative; no billing API permission or paid service is required.
  count += data.workflow_runs.length;
  if (data.workflow_runs.length < 100) break;
}
if (count * 20 > 1800) throw new Error(`Monthly admission closed: ${count} runs × 20 min exceeds 1800 min reserve`);
console.log(`Budget admission: ${count} runs × 20 min <= 1800 min (200 min safety reserve)`);
