// Netlify serverless function: Telegram bot webhook + API
// This handles Telegram updates and serves as the bot backend

const CH_API_BASE = 'https://api.companieshouse.gov.uk';

exports.handler = async (event, context) => {
  const path = event.path.replace('/.netlify/functions/api', '');
  const method = event.httpMethod;

  // CORS
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Content-Type': 'application/json',
  };

  if (method === 'OPTIONS') return { statusCode: 200, headers: cors, body: '' };

  // Health check
  if (path === '/health' || path === '/') {
    return json({ status: 'ok', service: 'UK Company Watch', version: '1.0.0' }, cors);
  }

  // Telegram webhook
  if (path === '/telegram/webhook' && method === 'POST') {
    return handleTelegram(event.body, cors);
  }

  // Company search
  if (path === '/companies/search') {
    const q = event.queryStringParameters?.q;
    const page = parseInt(event.queryStringParameters?.page || '1');
    if (!q || q.length < 2) return json({ error: 'q required (min 2 chars)' }, cors, 400);
    return searchCompanies(q, page, cors);
  }

  // Company details
  const companyMatch = path.match(/^\/companies\/([A-Z0-9]+)$/);
  if (companyMatch) return getCompany(companyMatch[1], cors);

  // Insolvency
  if (path === '/insolvency') {
    return getInsolvency(parseInt(event.queryStringParameters?.page || '1'), cors);
  }

  // Recent filings
  if (path === '/filings/recent') {
    return getRecentFilings(parseInt(event.queryStringParameters?.days || '1'), cors);
  }

  return json({ error: 'Not found' }, cors, 404);
};

async function chFetch(path) {
  try {
    const res = await fetch(`${CH_API_BASE}${path}`, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) return null;
    return await res.json();
  } catch { return null; }
}

async function searchCompanies(q, page, cors) {
  const start = (page - 1) * 20;
  const data = await chFetch(`/search/companies?q=${encodeURIComponent(q)}&items_per_page=20&start_index=${start}`);
  if (!data) return json({ error: 'CH API error' }, cors, 502);
  return json({
    data: data.items?.map(c => ({
      company_number: c.company_number,
      company_name: c.title,
      status: c.company_status,
      type: c.company_type,
      date_of_creation: c.date_of_creation,
      address: c.address_snippet,
    })) || [],
    pagination: { page, total: data.total_results || 0 },
  }, cors);
}

async function getCompany(num, cors) {
  const data = await chFetch(`/company/${num}`);
  if (!data) return json({ error: 'Not found' }, cors, 404);
  return json({ data: {
    company_number: data.company_number,
    company_name: data.company_name,
    status: data.company_status,
    type: data.type,
    date_of_creation: data.date_of_creation,
    registered_office_address: data.registered_office_address,
    sic_codes: data.sic_codes || [],
    accounts: { next_due: data.accounts?.next_due, overdue: data.accounts?.overdue || false },
    confirmation_statement: { next_due: data.confirmation_statement?.next_due, overdue: data.confirmation_statement?.overdue || false },
  }}, cors);
}

async function getInsolvency(page, cors) {
  const data = await chFetch(`/search/companies?q=&company_status=insolvency&items_per_page=20&start_index=${(page-1)*20}`);
  if (!data) return json({ error: 'CH API error' }, cors, 502);
  return json({
    data: data.items?.map(c => ({ company_number: c.company_number, company_name: c.title, status: c.company_status, date_of_creation: c.date_of_creation })) || [],
    pagination: { page, total: data.total_results || 0 },
  }, cors);
}

async function getRecentFilings(days, cors) {
  const d = new Date(); d.setDate(d.getDate() - days);
  const data = await chFetch(`/advanced-search/companies?incorporated_from=${d.toISOString().split('T')[0]}&items_per_page=20`);
  if (!data) return json({ error: 'CH API error' }, cors, 502);
  return json({
    data: data.items?.map(c => ({ company_number: c.company_number, company_name: c.title, status: c.company_status, date_of_creation: c.date_of_creation })) || [],
    pagination: { total: data.total_results || 0 },
  }, cors);
}

async function handleTelegram(bodyStr, cors) {
  let body;
  try { body = JSON.parse(bodyStr); } catch { return { statusCode: 200, headers: cors, body: 'ok' }; }
  if (!body.message) return { statusCode: 200, headers: cors, body: 'ok' };

  const chatId = body.message.chat.id;
  const text = (body.message.text || '').trim();

  if (text === '/start') {
    await tgSend(chatId,
      'Welcome to UK Company Watch! 🇬🇧\n\n' +
      'Real-time alerts for UK company:\n' +
      '• Insolvency filings\n' +
      '• Director changes\n' +
      '• Significant documents\n\n' +
      'Commands:\n' +
      '/search [name] — Search companies\n' +
      '/company [number] — Get details\n' +
      '/pricing — View plans\n\n' +
      'Free: 3 alerts/day • No signup needed'
    );
  } else if (text.startsWith('/search ')) {
    const q = text.slice(8);
    if (q.length < 2) {
      await tgSend(chatId, 'Query must be at least 2 chars. Example: /search Nike');
    } else {
      const data = await chFetch(`/search/companies?q=${encodeURIComponent(q)}&items_per_page=5`);
      if (data?.items?.length) {
        let msg = `Found ${data.total_results} companies for "${q}":\n\n`;
        data.items.slice(0, 5).forEach(c => {
          msg += `• ${c.title} (${c.company_number}) — ${c.company_status}\n`;
        });
        msg += '\nTap /company [number] for details.';
        await tgSend(chatId, msg);
      } else {
        await tgSend(chatId, `No results for "${q}".`);
      }
    }
  } else if (text.startsWith('/company ')) {
    const num = text.slice(9).trim();
    const data = await chFetch(`/company/${num}`);
    if (data) {
      let msg = `📊 ${data.company_name}\n`;
      msg += `Number: ${data.company_number}\nStatus: ${data.company_status}\n`;
      msg += `Type: ${data.type}\nFounded: ${data.date_of_creation || 'N/A'}\n`;
      msg += `SIC: ${(data.sic_codes || []).join(', ') || 'N/A'}\n`;
      if (data.accounts?.overdue) msg += '⚠️ Accounts OVERDUE\n';
      if (data.confirmation_statement?.overdue) msg += '⚠️ Confirmation statement OVERDUE\n';
      await tgSend(chatId, msg);
    } else {
      await tgSend(chatId, `Company ${num} not found.`);
    }
  } else if (text === '/pricing') {
    await tgSend(chatId,
      '📊 UK Company Watch Pricing\n\n' +
      '🆓 Free — £0/month (3 alerts/day)\n\n' +
      '⭐ Pro — £4.99/month\n' +
      '50 alerts/day • 10 companies • Email alerts\n\n' +
      '🏢 Business — £19.99/month\n' +
      'Unlimited • API • Webhooks\n\n' +
      'Upgrade via PayPal:\n' +
      'hermeswillmakesmoney@gmail.com\n' +
      'Note: "UCW Pro ' + chatId + '"'
    );
  }

  return { statusCode: 200, headers: cors, body: 'ok' };
}

async function tgSend(chatId, text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

function json(data, headers, status = 200) {
  return { statusCode: status, headers, body: JSON.stringify(data, null, 2) };
}
