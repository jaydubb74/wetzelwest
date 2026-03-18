const API_BASE = 'https://wetzelwest.com';

const DEFAULT_TYPES = [
  { id: 'prospect',  name: 'Prospect' },
  { id: 'client',    name: 'Client' },
  { id: 'partner',   name: 'Partner' },
  { id: 'vendor',    name: 'Vendor' },
  { id: 'colleague', name: 'Colleague' },
  { id: 'recruiter', name: 'Recruiter' },
  { id: 'investor',  name: 'Investor' },
  { id: 'mentor',    name: 'Mentor' },
  { id: 'other',     name: 'Other' },
];

const DRAFT_KEY = 'crm_draft_v2';
const DRAFT_FIELDS = ['firstName','lastName','email','phone','title','company','newCompany','relationship','linkedinUrl','notes'];

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  restoreDraft();
  await Promise.all([loadRelationshipTypes(), loadCompanies()]);
  prefillFromCurrentTab();

  document.getElementById('extractBtn').addEventListener('click', extractFromLinkedIn);
  document.getElementById('saveBtn').addEventListener('click', saveContact);
  document.getElementById('loginLink').addEventListener('click', (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: `${API_BASE}/login` });
  });

  for (const id of DRAFT_FIELDS) {
    document.getElementById(id)?.addEventListener('input', saveDraft);
    document.getElementById(id)?.addEventListener('change', saveDraft);
  }
});

// ── Auth / Relationship Types ─────────────────────────────────────────────────

async function loadRelationshipTypes() {
  const sel = document.getElementById('relationship');
  let types = DEFAULT_TYPES;

  try {
    const resp = await fetch(`${API_BASE}/api/relationship_types`, {
      credentials: 'include',
      signal: AbortSignal.timeout(5000),
    });

    if (resp.status === 401) {
      showAuthBanner();
    } else if (resp.ok) {
      const data = await resp.json();
      const t = Array.isArray(data) ? data : (data.types || data.data || []);
      if (t.length) types = t;
    }
  } catch {
    // Network error — use defaults, extension is still functional
  }

  for (const t of types) {
    const opt = document.createElement('option');
    opt.value = t.id || t.slug || t.value || t.name;
    opt.textContent = t.name || t.label || t.value;
    sel.appendChild(opt);
  }
}

function showAuthBanner() {
  document.getElementById('authBanner').style.display = 'block';
}

// ── Companies ─────────────────────────────────────────────────────────────────

async function loadCompanies() {
  const sel = document.getElementById('company');
  try {
    const resp = await fetch(`${API_BASE}/api/companies`, {
      credentials: 'include',
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) return;
    const data = await resp.json();
    const companies = Array.isArray(data) ? data : (data.companies || data.data || []);
    for (const c of companies) {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.name;
      sel.appendChild(opt);
    }
  } catch {
    // Companies list not critical
  }
}

// ── LinkedIn Extraction ───────────────────────────────────────────────────────

// Auto-fill when popup opens on a LinkedIn profile page
async function prefillFromCurrentTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url?.match(/linkedin\.com\/in\//)) return;
    // Only auto-fill if form is empty
    if (document.getElementById('firstName').value) return;
    await doExtract(tab);
  } catch {
    // Silent — auto-fill is best-effort
  }
}

async function extractFromLinkedIn() {
  const btn = document.getElementById('extractBtn');
  btn.disabled = true;
  btn.textContent = 'Extracting…';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url?.match(/linkedin\.com\/in\//)) {
      showStatus('Please navigate to a LinkedIn profile page first (linkedin.com/in/…)', 'error');
      return;
    }
    await doExtract(tab);
  } catch (err) {
    showStatus('Extraction error: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Re-Extract Data';
  }
}

async function doExtract(tab) {
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: extractLinkedInProfile,
  });

  const profile = results?.[0]?.result;
  if (!profile) {
    showStatus('Could not extract profile data.', 'error');
    return;
  }
  if (profile.error) {
    showStatus(profile.error, 'error');
    return;
  }

  if (profile.firstName) document.getElementById('firstName').value = profile.firstName;
  if (profile.lastName)  document.getElementById('lastName').value  = profile.lastName;
  if (profile.email)     document.getElementById('email').value     = profile.email;
  if (profile.phone)     document.getElementById('phone').value     = profile.phone;
  if (profile.title)     document.getElementById('title').value     = profile.title;
  if (profile.linkedinUrl) document.getElementById('linkedinUrl').value = profile.linkedinUrl;

  // Try to match company in dropdown, otherwise set new company field
  const matchDiv = document.getElementById('companyMatch');
  if (profile.company) {
    const compSel = document.getElementById('company');
    let matched = false;
    for (const opt of compSel.options) {
      if (opt.textContent.trim().toLowerCase() === profile.company.trim().toLowerCase()) {
        compSel.value = opt.value;
        matched = true;
        break;
      }
    }
    if (matched) {
      matchDiv.textContent = `Matched existing company: "${profile.company}"`;
      matchDiv.className = 'company-match existing-company';
    } else {
      document.getElementById('newCompany').value = profile.company;
      matchDiv.textContent = `New company will be created: "${profile.company}"`;
      matchDiv.className = 'company-match new-company';
    }
  }

  document.getElementById('sourceBadge').innerHTML =
    '<span class="source-badge source-linkedin">LinkedIn</span>';
  document.getElementById('restoredBadge').style.display = 'none';
  saveDraft();
  showStatus('Profile extracted successfully.', 'success');
  setTimeout(() => showStatus('', ''), 3000);
}

// Runs inside the LinkedIn page — NO access to extension APIs here
function extractLinkedInProfile() {
  try {
    const $ = (sel) => document.querySelector(sel)?.innerText?.trim() || '';
    const attr = (sel, a) => document.querySelector(sel)?.getAttribute(a)?.trim() || '';

    // Name — profile pages always have an h1 with the person's name
    const fullName = $('h1.text-heading-xlarge') || $('h1');
    if (!fullName) {
      return { error: 'Could not find a profile name. Are you on a profile page (linkedin.com/in/…)?' };
    }
    const [firstName, ...rest] = fullName.split(/\s+/);
    const lastName = rest.join(' ');

    // Headline / title — the subtitle line directly under the name
    // Try most-specific selector first to avoid matching search results
    const titleEl =
      document.querySelector('.pv-text-details__left-panel .text-body-medium.break-words') ||
      document.querySelector('section.artdeco-card:first-of-type .text-body-medium.break-words') ||
      document.querySelector('.text-body-medium.break-words');
    const title = titleEl?.innerText?.trim() || '';

    // Current company from the top-card right panel
    const companyEl =
      document.querySelector('.pv-text-details__right-panel .hoverable-link-text span[aria-hidden="true"]') ||
      document.querySelector('.pv-text-details__right-panel .inline-show-more-text span[aria-hidden="true"]') ||
      document.querySelector('.pv-text-details__right-panel span[aria-hidden="true"]');
    const company = companyEl?.innerText?.trim() || '';

    // Email / phone (only visible if the contact-info panel is open)
    const email = attr('a[href^="mailto:"]', 'href').replace('mailto:', '') || $('a[href^="mailto:"]');
    const phone = attr('a[href^="tel:"]', 'href').replace('tel:', '') || $('a[href^="tel:"]');

    // Canonical profile URL (strip query params and trailing slash)
    const linkedinUrl = window.location.href.split('?')[0].replace(/\/$/, '');

    return { firstName, lastName, title, company, email, phone, linkedinUrl };
  } catch (err) {
    return { error: 'Extraction failed: ' + err.message };
  }
}

// ── Save Contact ──────────────────────────────────────────────────────────────

async function saveContact() {
  const firstName        = document.getElementById('firstName').value.trim();
  const lastName         = document.getElementById('lastName').value.trim();
  const email            = document.getElementById('email').value.trim();
  const phone            = document.getElementById('phone').value.trim();
  const title            = document.getElementById('title').value.trim();
  const companyId        = document.getElementById('company').value;
  const newCompanyName   = document.getElementById('newCompany').value.trim();
  const relationshipType = document.getElementById('relationship').value;
  const linkedinUrl      = document.getElementById('linkedinUrl').value.trim();
  const notes            = document.getElementById('notes').value.trim();

  if (!firstName)        { showStatus('First name is required.', 'error'); return; }
  if (!relationshipType) { showStatus('Please select a relationship type.', 'error'); return; }
  if (!companyId && !newCompanyName) {
    showStatus('Please select a company or enter a new company name.', 'error');
    return;
  }

  const payload = {
    first_name: firstName,
    last_name: lastName,
    email,
    phone,
    title,
    relationship_type: relationshipType,
    linkedin_url: linkedinUrl,
    notes,
  };

  if (companyId) {
    payload.company_id = companyId;
  } else {
    payload.new_company_name = newCompanyName;
  }

  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const resp = await fetch(`${API_BASE}/api/contacts`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000),
    });

    if (resp.status === 401) {
      showAuthBanner();
      showStatus('Not logged in — please log in and try again.', 'error');
      return;
    }

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || err.message || `Server error ${resp.status}`);
    }

    showStatus('Contact saved!', 'success');
    clearDraft();
    resetForm();
  } catch (err) {
    showStatus('Save failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Contact';
  }
}

function resetForm() {
  for (const id of ['firstName','lastName','email','phone','title','newCompany','linkedinUrl','notes']) {
    document.getElementById(id).value = '';
  }
  document.getElementById('company').value = '';
  document.getElementById('relationship').value = '';
  document.getElementById('companyMatch').textContent = '';
  document.getElementById('sourceBadge').innerHTML = '';
  document.getElementById('restoredBadge').style.display = 'none';
}

// ── Draft Persistence ─────────────────────────────────────────────────────────

function saveDraft() {
  const draft = {};
  for (const id of DRAFT_FIELDS) {
    draft[id] = document.getElementById(id)?.value || '';
  }
  chrome.storage.local.set({ [DRAFT_KEY]: draft });
}

function restoreDraft() {
  chrome.storage.local.get(DRAFT_KEY, ({ [DRAFT_KEY]: draft }) => {
    if (!draft) return;
    let any = false;
    for (const id of DRAFT_FIELDS) {
      const el = document.getElementById(id);
      if (el && draft[id]) { el.value = draft[id]; any = true; }
    }
    if (any) document.getElementById('restoredBadge').style.display = 'block';
  });
}

function clearDraft() {
  chrome.storage.local.remove(DRAFT_KEY);
}

// ── UI Helpers ────────────────────────────────────────────────────────────────

function showStatus(msg, type) {
  const el = document.getElementById('statusMessage');
  el.textContent = msg;
  el.className = 'status ' + (type || '');
  el.style.display = msg ? 'block' : 'none';
}
