const CRM_BASE = 'https://crm.wetzelwest.com';

// Default relationship types used when the API is unavailable
const DEFAULT_RELATIONSHIP_TYPES = [
  { id: 'prospect',   name: 'Prospect' },
  { id: 'client',     name: 'Client' },
  { id: 'partner',    name: 'Partner' },
  { id: 'vendor',     name: 'Vendor' },
  { id: 'colleague',  name: 'Colleague' },
  { id: 'recruiter',  name: 'Recruiter' },
  { id: 'investor',   name: 'Investor' },
  { id: 'mentor',     name: 'Mentor' },
  { id: 'other',      name: 'Other' },
];

const STORAGE_KEY = 'wetzelcrm_form_draft';

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  restoreDraft();
  loadCompanies();
  loadRelationshipTypes();
  setupAutosave();
});

// ── Relationship Types ────────────────────────────────────────────────────────

async function loadRelationshipTypes() {
  const sel = document.getElementById('relationshipType');
  const banner = document.getElementById('errorBanner');

  try {
    const resp = await fetch(`${CRM_BASE}/api/relationship-types`, {
      credentials: 'include',
      signal: AbortSignal.timeout(5000),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const data = await resp.json();
    const types = Array.isArray(data) ? data : (data.types || data.data || []);

    if (!types.length) throw new Error('empty response');

    populateRelationshipTypes(sel, types);
    banner.style.display = 'none';
  } catch (err) {
    // API failed — fall back to defaults so the form stays usable
    console.warn('[WetzelCRM] Relationship types API failed, using defaults:', err.message);
    populateRelationshipTypes(sel, DEFAULT_RELATIONSHIP_TYPES);

    banner.textContent = 'Using default relationship types (CRM server unreachable)';
    banner.className = 'alert alert-warn';
    banner.style.display = 'block';
  }
}

function populateRelationshipTypes(select, types) {
  // Keep the placeholder option, remove the rest
  while (select.options.length > 1) select.remove(1);

  for (const t of types) {
    const opt = document.createElement('option');
    opt.value = t.id || t.value || t.slug || t.name;
    opt.textContent = t.name || t.label || t.value;
    select.appendChild(opt);
  }
}

// ── Companies ─────────────────────────────────────────────────────────────────

async function loadCompanies() {
  const sel = document.getElementById('company');
  try {
    const resp = await fetch(`${CRM_BASE}/api/companies`, {
      credentials: 'include',
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const companies = Array.isArray(data) ? data : (data.companies || data.data || []);

    for (const c of companies) {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.name;
      sel.appendChild(opt);
    }

    // Add a "new company" sentinel option
    const newOpt = document.createElement('option');
    newOpt.value = '__new__';
    newOpt.textContent = '+ Add new company…';
    sel.appendChild(newOpt);
  } catch {
    // Companies list not critical — just leave the select empty
  }

  sel.addEventListener('change', () => {
    const newCompanyGroup = document.getElementById('newCompanyGroup');
    const hint = document.getElementById('newCompanyHint');
    if (sel.value === '__new__') {
      newCompanyGroup.style.display = 'block';
      hint.style.display = 'none';
    } else if (sel.value === '') {
      newCompanyGroup.style.display = 'none';
      hint.style.display = 'none';
    } else {
      newCompanyGroup.style.display = 'none';
      hint.style.display = 'none';
    }
    saveDraft();
  });
}

// ── LinkedIn extraction ───────────────────────────────────────────────────────

async function fillFromLinkedIn() {
  const btn = document.getElementById('linkedinBtn');
  btn.disabled = true;
  btn.textContent = 'Extracting…';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url?.includes('linkedin.com')) {
      showInfo('Navigate to a LinkedIn profile first.');
      return;
    }

    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const text = (sel) => document.querySelector(sel)?.innerText?.trim() || '';
        const attr = (sel, a) => document.querySelector(sel)?.getAttribute(a)?.trim() || '';

        const fullName  = text('h1');
        const nameParts = fullName.split(/\s+/);
        const firstName = nameParts[0] || '';
        const lastName  = nameParts.slice(1).join(' ') || '';

        const title   = text('.text-body-medium');
        const company = text('.pv-text-details__right-panel span[aria-hidden="true"]') ||
                        text('.inline-show-more-text') || '';

        const email = text('a[href^="mailto:"]') ||
                      attr('a[href^="mailto:"]', 'href').replace('mailto:', '');
        const phone = text('a[href^="tel:"]') ||
                      attr('a[href^="tel:"]', 'href').replace('tel:', '');

        return { firstName, lastName, title, company, email, phone };
      },
    });

    const profile = results?.[0]?.result;
    if (!profile) {
      showInfo('Could not extract profile data from this page.');
      return;
    }

    if (profile.firstName) document.getElementById('firstName').value = profile.firstName;
    if (profile.lastName)  document.getElementById('lastName').value  = profile.lastName;
    if (profile.email)     document.getElementById('email').value     = profile.email;
    if (profile.phone)     document.getElementById('phone').value     = profile.phone;
    if (profile.title)     document.getElementById('title').value     = profile.title;

    // Try to match company
    if (profile.company) {
      const sel = document.getElementById('company');
      let matched = false;
      for (const opt of sel.options) {
        if (opt.textContent.trim().toLowerCase() === profile.company.toLowerCase()) {
          sel.value = opt.value;
          matched = true;
          break;
        }
      }
      if (!matched) {
        sel.value = '__new__';
        document.getElementById('newCompanyGroup').style.display = 'block';
        document.getElementById('newCompany').value = profile.company;
      }
    }

    document.getElementById('restoreNote').style.display = 'block';
    document.getElementById('restoreNote').querySelector('em').textContent = 'Data extracted from LinkedIn';
    saveDraft();
  } catch (err) {
    showInfo('Extraction failed: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'From LinkedIn';
  }
}

// ── Save contact ──────────────────────────────────────────────────────────────

async function saveContact() {
  const firstName        = document.getElementById('firstName').value.trim();
  const lastName         = document.getElementById('lastName').value.trim();
  const email            = document.getElementById('email').value.trim();
  const phone            = document.getElementById('phone').value.trim();
  const title            = document.getElementById('title').value.trim();
  const companyId        = document.getElementById('company').value;
  const newCompany       = document.getElementById('newCompany').value.trim();
  const relationshipType = document.getElementById('relationshipType').value;
  const notes            = document.getElementById('notes').value.trim();

  // Validation
  if (!firstName) { showStatus('First name is required.', 'error'); return; }
  if (!relationshipType) { showStatus('Please select a relationship type.', 'error'); return; }
  if (!companyId && !newCompany) { showStatus('Please select or enter a company.', 'error'); return; }

  const payload = {
    first_name: firstName,
    last_name: lastName,
    email,
    phone,
    title,
    relationship_type: relationshipType,
    notes,
  };

  if (companyId && companyId !== '__new__') {
    payload.company_id = companyId;
  } else if (newCompany) {
    payload.new_company_name = newCompany;
  }

  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const resp = await fetch(`${CRM_BASE}/api/contacts`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || err.message || `HTTP ${resp.status}`);
    }

    showStatus('Contact saved!', 'success');
    clearDraft();
    setTimeout(() => { showStatus('', ''); }, 3000);
  } catch (err) {
    showStatus('Save failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Contact';
  }
}

// ── Draft persistence ─────────────────────────────────────────────────────────

const FIELDS = ['firstName', 'lastName', 'email', 'phone', 'title', 'company', 'newCompany', 'relationshipType', 'notes'];

function saveDraft() {
  const draft = {};
  for (const id of FIELDS) {
    draft[id] = document.getElementById(id)?.value || '';
  }
  chrome.storage.local.set({ [STORAGE_KEY]: draft });
}

function restoreDraft() {
  chrome.storage.local.get(STORAGE_KEY, ({ [STORAGE_KEY]: draft }) => {
    if (!draft) return;
    let restored = false;
    for (const id of FIELDS) {
      const el = document.getElementById(id);
      if (el && draft[id]) { el.value = draft[id]; restored = true; }
    }
    if (restored) {
      document.getElementById('restoreNote').style.display = 'block';
      if (draft.company === '__new__') {
        document.getElementById('newCompanyGroup').style.display = 'block';
      }
    }
  });
}

function clearDraft() {
  chrome.storage.local.remove(STORAGE_KEY);
}

function setupAutosave() {
  for (const id of FIELDS) {
    document.getElementById(id)?.addEventListener('input', saveDraft);
  }
}

function clearForm() {
  for (const id of FIELDS) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  document.getElementById('newCompanyGroup').style.display = 'none';
  document.getElementById('restoreNote').style.display = 'none';
  document.getElementById('errorBanner').style.display = 'none';
  document.getElementById('infoBanner').style.display = 'none';
  clearDraft();
  showStatus('', '');
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showStatus(msg, type) {
  const el = document.getElementById('statusMsg');
  el.textContent = msg;
  el.className = type;
  el.style.display = msg ? 'block' : 'none';
}

function showInfo(msg) {
  const el = document.getElementById('infoBanner');
  el.textContent = msg;
  el.style.display = msg ? 'block' : 'none';
}
