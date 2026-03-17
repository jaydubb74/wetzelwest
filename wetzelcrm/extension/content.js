// Content script — runs on LinkedIn pages
// Listens for extraction requests from the popup via chrome.runtime.onMessage
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== 'EXTRACT_PROFILE') return;

  try {
    const text = (sel) => document.querySelector(sel)?.innerText?.trim() || '';
    const attr = (sel, a) => document.querySelector(sel)?.getAttribute(a)?.trim() || '';

    const fullName  = text('h1');
    const nameParts = fullName.split(/\s+/);
    const firstName = nameParts[0] || '';
    const lastName  = nameParts.slice(1).join(' ') || '';

    const title   = text('.text-body-medium');
    const company = text('.pv-text-details__right-panel span[aria-hidden="true"]') ||
                    text('.inline-show-more-text') ||
                    '';

    // Email / phone live in the contact info modal — best-effort from visible text
    const email = text('a[href^="mailto:"]') ||
                  attr('a[href^="mailto:"]', 'href').replace('mailto:', '');
    const phone = text('a[href^="tel:"]') ||
                  attr('a[href^="tel:"]', 'href').replace('tel:', '');

    sendResponse({ firstName, lastName, title, company, email, phone });
  } catch (err) {
    sendResponse({ error: err.message });
  }

  return true; // keep channel open for async
});
