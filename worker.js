const ALLOWED_ORIGINS = [
  'https://wetzelwest.com',
  'https://crm.wetzelwest.com',
];

function getCorsHeaders(origin) {
  const allowed =
    ALLOWED_ORIGINS.includes(origin) || /^chrome-extension:\/\//.test(origin)
      ? origin
      : null;
  if (!allowed) return {};
  return {
    'Access-Control-Allow-Origin': allowed,
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Methods': 'GET, POST, PUT, PATCH, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization, Cookie',
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin') || '';
    const corsHeaders = getCorsHeaders(origin);

    // Handle CORS preflight
    if (request.method === 'OPTIONS' && url.pathname.startsWith('/api/')) {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    if (url.pathname.startsWith('/api/')) {
      const targetUrl = new URL(url.pathname + url.search, 'https://crm.wetzelwest.com');
      const proxiedRequest = new Request(targetUrl, {
        method: request.method,
        headers: request.headers,
        body: request.method !== 'GET' && request.method !== 'HEAD' ? request.body : undefined,
        redirect: 'follow',
      });
      try {
        const upstreamResp = await fetch(proxiedRequest);
        const resp = new Response(upstreamResp.body, upstreamResp);
        for (const [k, v] of Object.entries(corsHeaders)) resp.headers.set(k, v);
        return resp;
      } catch {
        return new Response(JSON.stringify({ error: 'CRM server is unreachable' }), {
          status: 502,
          headers: { 'Content-Type': 'application/json', ...corsHeaders },
        });
      }
    }

    return env.ASSETS.fetch(request);
  },
};
