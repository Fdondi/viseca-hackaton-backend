// Shared helpers for the lab pages.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = v => v === null || v === undefined ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v);
const WORD = {approve:"Approved", step_up:"Asks the customer", decline:"Declined"};
const LABEL = {approve:"approved", step_up:"asks you", decline:"declined", approved:"approved", declined:"declined", pending:"waiting", expired:"expired"};
const DATA_PILL = d => d === "shop's own text (untrusted)" ? "free" : d === "AP2 signatures" ? "signed" : "tx";
// Who decides a check at purchase time: one pill. `r.ai` (from the server) says how the AI takes part; null = no AI.
const WHO = r => r.ai
  ? `<span class="pill llm" title="${esc(r.ai)}">AI can decide${r.ai_used ? " · did here" : ""}</span>`
  : `<span class="pill fixed" title="${esc(r.how || "")}">no AI</span>`;

function toast(m){ let t = $("#toast"); if(!t){ t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.append(t); }
  t.textContent = m; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => t.hidden = true, 3500); }
async function api(path, body){
  const r = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : r.statusText);
  return j;
}
function debounce(fn, ms = 250){ let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }

// highlight phrases inside a text; spans = [{text, cls}]
function highlight(text, spans){
  let html = esc(text);
  spans.filter(s => s.text && s.text.trim().length > 1).sort((a, b) => b.text.length - a.text.length).forEach(s => {
    const t = esc(s.text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    html = html.replace(new RegExp(`(?![^<]*>)${t}`, "i"), m => `<mark class="${s.cls || ""}" title="${esc(s.title || "")}">${m}</mark>`);
  });
  return html;
}

// the Rule parameters, as UML attributes
function ruleAttrs(rule){
  return ["field", "operator", "value", "currency", "scope", "period_days"].filter(k => rule[k] !== undefined && rule[k] !== null)
    .map(k => `<div class="attr"><span class="k">${k}</span><span class="v">${esc(fmt(rule[k]))}</span></div>`).join("");
}

// one evaluated check: rule / input / how / result
function checkCard(r){
  const o = r.origin || {};
  const origin = o.kind === "llm" ? "«AI suggestion · confirmed»" : o.kind === "words" ? `«your words${o.phrase ? `: “${esc(o.phrase)}”` : ""}»`
    : o.kind === "permanent" ? `«permanent · from the profile${o.phrase ? `: “${esc(o.phrase)}”` : ""}»` : `«${esc(o.label || r.tier)}»`;
  return `<div class="chk ${r.status}">
    <div class="hd"><span class="st">${origin}</span><span class="pill ${r.status}">${r.status}</span></div>
    <div class="bd"><div class="rir">
      <span class="lbl">Rule</span><div>${esc(r.rule_text || r.how)}<br><code>${esc(r.rule ? `${r.rule.field} ${r.rule.operator} ${fmt(r.rule.value)}${r.rule.period_days ? ` over ${r.rule.period_days} days` : ""}` : r.field)}</code></div>
      <span class="lbl">Input</span><div class="ins">${(r.inputs || []).map(i => `<span class="in ${i.class}">${i.value === "consulted" ? `<span>${esc(i.label)}</span>` : `<span class="l">${esc(i.label)}</span>${esc(fmt(i.value))}`}${i.from ? `<span class="from">from ${esc(i.from)}</span>` : ""}</span>`).join("") || `<span class="note">none</span>`}</div>
      <span class="lbl">How</span><div><ol class="trace">${(r.trace || []).map(t => `<li>${esc(t)}</li>`).join("") || `<li>${esc(r.how)}</li>`}</ol></div>
      <span class="lbl">Result</span><div><b class="st-${r.status}">${{pass:"pass", fail:"fail", unknown:"uncertain"}[r.status]}</b> · ${esc(r.text)}</div></div>
      <span class="row" style="gap:4px">${WHO(r)}${r.text_input ? `<span class="pill free">reads the shop's own text</span>` : r.tier === "ap2" ? "" : `<span class="pill tx">transaction data only</span>`}${r.security ? `<span class="pill unknown">can only escalate</span>` : ""}</span>
    </div></div>`;
}

function nav(active){
  const pages = [["permanent", "1 · Permanent rules", 8101], ["mandate", "2 · Mandates", 8102], ["apply", "3 · Applying rules", 8103],
                 ["shop-text", "4 · Shop text", 8104], ["respond", "5 · Customer response", 8105]];
  return pages.map(([k, l, port]) => `<a class="${k === active ? "on" : ""}" href="${location.protocol}//${location.hostname}:${port}/">${l}</a>`).join("");
}
