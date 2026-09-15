// ── Events Management (events.html) ────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const editModal = document.getElementById('modal-edit-event');
  const editForm = document.getElementById('edit-event-form');
  const editInput = document.getElementById('edit-event-name');

  const deleteModal = document.getElementById('modal-delete-event');
  const deleteForm = document.getElementById('delete-event-form');
  const deleteNameDisplay = document.getElementById('delete-event-name-display');

  // Open edit modal
  document.querySelectorAll('.btn-edit-event').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-event-id');
      const name = btn.getAttribute('data-event-name');
      if (editForm && editInput) {
        editForm.action = `/studio/events/${id}/edit`;
        editInput.value = name || '';
      }
      if (editModal) {
        editModal.classList.add('active');
        if (editInput) editInput.focus();
      }
    });
  });

  // Close edit modal
  document.querySelectorAll('.btn-close-edit-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (editModal) editModal.classList.remove('active');
    });
  });

  // Open delete modal
  document.querySelectorAll('.btn-delete-event').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-event-id');
      const name = btn.getAttribute('data-event-name') || 'this event';
      if (deleteForm && deleteNameDisplay) {
        deleteForm.action = `/studio/events/${id}/delete`;
        deleteNameDisplay.textContent = `"${name}"`;
      }
      if (deleteModal) {
        deleteModal.classList.add('active');
      }
    });
  });

  // Close delete modal
  document.querySelectorAll('.btn-close-delete-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (deleteModal) deleteModal.classList.remove('active');
    });
  });

  // Close modals when clicking backdrop
  [editModal, deleteModal].forEach(modal => {
    if (modal) {
      modal.addEventListener('click', (e) => {
        if (e.target === modal) {
          modal.classList.remove('active');
        }
      });
    }
  });

  // ── API Keys Modal Handling ─────────────────────────────────────
  const apiKeysModal = document.getElementById('modal-api-keys');
  const apiKeysEventName = document.getElementById('api-keys-event-name-display');
  const apiKeysEventId = document.getElementById('api-keys-event-id-display');
  const apiKeysTbody = document.getElementById('api-keys-tbody');
  const apiKeysEmpty = document.getElementById('api-keys-empty');
  const newKeyAlert = document.getElementById('new-key-alert');
  const newKeyInput = document.getElementById('new-key-input');
  const btnGenerateKey = document.getElementById('btn-generate-api-key');
  const btnCopyNewKey = document.getElementById('btn-copy-new-key');
  const btnCopyEventId = document.querySelector('.btn-copy-event-id');

  let currentActiveEventId = null;

  async function loadApiKeys(eventId) {
    if (!apiKeysTbody) return;
    apiKeysTbody.innerHTML = '<tr><td colspan="6" class="text-muted" class="text-danger text-center api-keys-empty-msg">Loading API keys...</td></tr>';
    if (apiKeysEmpty) apiKeysEmpty.classList.add('api-key-alert-hidden');

    try {
      const resp = await fetch(`/events/${eventId}/api-keys`);
      if (!resp.ok) {
        apiKeysTbody.innerHTML = `<tr><td colspan="6" class="text-danger" class="text-danger text-center api-keys-empty-msg">Failed to load keys (${resp.status})</td></tr>`;
        return;
      }
      const keys = await resp.json();
      apiKeysTbody.innerHTML = '';

      if (!keys || keys.length === 0) {
        if (apiKeysEmpty) apiKeysEmpty.classList.remove('api-key-alert-hidden');
        return;
      }

      if (apiKeysEmpty) apiKeysEmpty.classList.add('api-key-alert-hidden');
      keys.forEach(k => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="td-mono">#${k.id}</td>
          <td>${escapeHtml(k.name || 'API Key')}</td>
          <td><code class="input-mono">${escapeHtml(k.masked_key)}</code></td>
          <td class="col-actions-right">
            <button type="button" class="btn btn-ghost btn-xs btn-danger-ghost btn-revoke-key" data-key-id="${k.id}">Revoke</button>
          </td>
        `;
        apiKeysTbody.appendChild(tr);
      });

      // Bind revoke buttons
      document.querySelectorAll('.btn-revoke-key').forEach(b => {
        b.addEventListener('click', async () => {
          const keyId = b.getAttribute('data-key-id');
          if (!confirm('Are you sure you want to revoke this API key? External integrations using this key will stop working.')) {
            return;
          }
          b.disabled = true;
          b.textContent = 'Revoking...';
          try {
            const delResp = await fetch(`/events/${eventId}/api-keys/${keyId}`, { method: 'DELETE' });
            if (delResp.ok) {
              loadApiKeys(eventId);
            } else {
              alert('Failed to revoke key');
              b.disabled = false;
              b.textContent = 'Revoke';
            }
          } catch (e) {
            alert('Error revoking key: ' + e);
            b.disabled = false;
            b.textContent = 'Revoke';
          }
        });
      });

    } catch (err) {
      apiKeysTbody.innerHTML = `<tr><td colspan="6" class="text-danger" class="text-danger text-center api-keys-empty-msg">Error loading keys: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  document.querySelectorAll('.btn-api-keys').forEach(btn => {
    btn.addEventListener('click', () => {
      const eventId = btn.getAttribute('data-event-id');
      const eventName = btn.getAttribute('data-event-name') || `Event #${eventId}`;
      currentActiveEventId = eventId;

      if (apiKeysEventName) apiKeysEventName.textContent = `"${eventName}"`;
      if (apiKeysEventId) apiKeysEventId.textContent = `#${eventId}`;
      if (newKeyAlert) newKeyAlert.classList.add('api-key-alert-hidden');

      if (apiKeysModal) {
        apiKeysModal.classList.add('active');
        loadApiKeys(eventId);
      }
    });
  });

  document.querySelectorAll('.btn-close-api-keys-modal').forEach(btn => {
    btn.addEventListener('click', () => {
      if (apiKeysModal) apiKeysModal.classList.remove('active');
      currentActiveEventId = null;
    });
  });

  if (apiKeysModal) {
    apiKeysModal.addEventListener('click', (e) => {
      if (e.target === apiKeysModal) {
        apiKeysModal.classList.remove('active');
        currentActiveEventId = null;
      }
    });
  }

  if (btnGenerateKey) {
    btnGenerateKey.addEventListener('click', async () => {
      if (!currentActiveEventId) return;
      btnGenerateKey.disabled = true;
      btnGenerateKey.textContent = 'Generating...';

      try {
        const resp = await fetch(`/events/${currentActiveEventId}/api-keys`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'Eventyay Integration Key' })
        });
        if (resp.ok) {
          const data = await resp.json();
          if (newKeyInput) newKeyInput.value = data.api_key;
          if (newKeyAlert) newKeyAlert.classList.remove('api-key-alert-hidden');
          loadApiKeys(currentActiveEventId);
        } else {
          alert('Failed to generate key');
        }
      } catch (err) {
        alert('Error generating key: ' + err);
      } finally {
        btnGenerateKey.disabled = false;
        btnGenerateKey.textContent = '+ Generate API Key';
      }
    });
  }

  if (btnCopyNewKey && newKeyInput) {
    btnCopyNewKey.addEventListener('click', () => {
      navigator.clipboard.writeText(newKeyInput.value).then(() => {
        const orig = btnCopyNewKey.textContent;
        btnCopyNewKey.textContent = 'Copied!';
        setTimeout(() => { btnCopyNewKey.textContent = orig; }, 2000);
      });
    });
  }

  if (btnCopyEventId) {
    btnCopyEventId.addEventListener('click', () => {
      if (currentActiveEventId) {
        navigator.clipboard.writeText(currentActiveEventId).then(() => {
          const orig = btnCopyEventId.textContent;
          btnCopyEventId.textContent = 'Copied!';
          setTimeout(() => { btnCopyEventId.textContent = orig; }, 2000);
        });
      }
    });
  }

});
