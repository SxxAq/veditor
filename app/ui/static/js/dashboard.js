/**
 * VEditor Dashboard — Live Status Polling + Instant Filter Logic
 */

const ACTIVE_STATUSES = new Set([
  'detecting', 'cutting', 'normalizing', 'rendering', 'transcoding', 'publishing'
]);

const STATUS_BADGE_MAP = {
  waiting_for_files: '<span class="badge badge-gray"><span class="badge-dot"></span>Waiting</span>',
  detecting:         '<span class="badge badge-amber badge-pulse"><span class="badge-dot"></span>Detecting</span>',
  approval_pending:  '<span class="badge badge-orange badge-pulse"><span class="badge-dot"></span>Pending Review</span>',
  cutting:           '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Cutting</span>',
  normalizing:       '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Normalizing</span>',
  rendering:         '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Rendering</span>',
  transcoding:       '<span class="badge badge-blue badge-pulse"><span class="badge-dot"></span>Transcoding</span>',
  preview:           '<span class="badge badge-teal"><span class="badge-dot"></span>Preview Ready</span>',
  publishing:        '<span class="badge badge-purple badge-pulse"><span class="badge-dot"></span>Publishing</span>',
  done:              '<span class="badge badge-green"><span class="badge-dot"></span>Done</span>',
  failed:            '<span class="badge badge-red"><span class="badge-dot"></span>Failed</span>',
};

function getActiveTalkIds() {
  return [...document.querySelectorAll('tr[data-talk-id]')]
    .filter(row => ACTIVE_STATUSES.has(row.dataset.status))
    .map(row => parseInt(row.dataset.talkId, 10));
}

async function pollTalk(talkId) {
  try {
    const r = await fetch(`/talks/${talkId}`, {
      headers: { 'X-API-Key': window._veditorApiKey || '' }
    });
    if (!r.ok) return;
    const data = await r.json();
    const cell = document.querySelector(`.status-cell[data-talk-id="${talkId}"]`);
    const row  = document.querySelector(`tr[data-talk-id="${talkId}"]`);
    if (!cell) return;
    const newBadge = STATUS_BADGE_MAP[data.status] ?? STATUS_BADGE_MAP.waiting_for_files;
    cell.innerHTML = newBadge;
    if (row) row.dataset.status = data.status;
    if (data.status === 'done' || data.status === 'failed') {
      setTimeout(() => location.reload(), 1500);
    }
  } catch { /* skip */ }
}

function startPolling() {
  const ids = getActiveTalkIds();
  if (ids.length === 0) return;
  ids.forEach(id => pollTalk(id));
  setInterval(() => {
    getActiveTalkIds().forEach(id => pollTalk(id));
  }, 5000);
}

// ── Instant Live Filter on Typing ───────────────────────────────
const searchInput = document.getElementById('search-input');
const statusSelect = document.getElementById('status-select');
const rows = document.querySelectorAll('tbody tr[data-talk-id]');

function applyFilters() {
  const q = (searchInput ? searchInput.value : '').toLowerCase().trim();
  const selectedStatus = statusSelect ? statusSelect.value : '';

  let visibleCount = 0;
  rows.forEach(row => {
    const titleText = (row.querySelector('.td-title') ? row.querySelector('.td-title').textContent : '').toLowerCase();
    const rowStatus = row.dataset.status || '';

    const matchesQuery = !q || titleText.includes(q);
    const matchesStatus = !selectedStatus || rowStatus === selectedStatus;

    if (matchesQuery && matchesStatus) {
      row.style.display = '';
      visibleCount++;
    } else {
      row.style.display = 'none';
    }
  });

  const countEl = document.querySelector('.table-count');
  if (countEl) {
    countEl.textContent = `${visibleCount} result${visibleCount !== 1 ? 's' : ''}`;
  }
}

if (searchInput) {
  searchInput.addEventListener('input', applyFilters);
}

if (statusSelect) {
  statusSelect.addEventListener('change', applyFilters);
}

// Keyboard accessibility
rows.forEach(row => {
  row.setAttribute('tabindex', '0');
  row.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      window.location = `/ui/talks/${row.dataset.talkId}`;
    }
  });
});

document.addEventListener('DOMContentLoaded', startPolling);
